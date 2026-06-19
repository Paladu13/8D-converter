"""
Module de téléchargement Spotify.

Stratégie (pensée pour les IP cloud type Render, qui se font bloquer par YouTube) :
  1. Métadonnées Spotify (artiste / titre) via la page embed publique.
  2. yt-dlp ytsearch sur YouTube en émulant le client Android/TV, ce qui contourne
     dans la majorité des cas le blocage anti-bot ("Sign in to confirm you're not
     a bot") que subissent les IP de datacenter. Dès qu'un blocage de ce type est
     détecté, on arrête immédiatement les tentatives YouTube (inutile d'insister).
  3. Repli spotdl avec plusieurs fournisseurs audio alternatifs (Piped, SoundCloud,
     Bandcamp — on évite YouTube/YouTube Music, blacklistés sur les IP cloud), et
     un matching permissif (--dont-filter-results) pour éviter les faux négatifs
     du type "LookupError: No results found" quand un résultat correct existe
     mais ne passe pas le scoring strict de spotdl par défaut.
"""
import os
import re
import json
import glob
import shutil
import subprocess
import threading
import time
import sys

import requests

from .audio_processor import UPLOAD_FOLDER


def _log(job_id, msg):
    """
    Écrit dans stdout (donc visible dans les logs Render), avec le job_id
    pour pouvoir suivre une requête précise. Le message renvoyé au client
    est tronqué à 600 caractères, mais ICI on garde tout.
    """
    print(f"[spotify:{job_id}] {msg}", flush=True, file=sys.stdout)

# Émuler ces clients yt-dlp (dans cet ordre) contourne le blocage anti-bot
# YouTube rencontré depuis les IP de datacenter, sans nécessiter de cookies.
YTDLP_PLAYER_CLIENTS = "android,tv,web"

# Fournisseurs audio de repli pour spotdl, en excluant YouTube / YouTube Music
# (bloqués sur les IP cloud). Piped est un proxy YouTube alternatif qui passe
# souvent là où l'accès direct à YouTube est bloqué.
SPOTDL_FALLBACK_PROVIDERS = ["piped", "soundcloud", "bandcamp"]

_BOT_CHECK_MARKERS = (
    "sign in to confirm",
    "confirm you're not a bot",
    "http error 429",
)


def sanitize_filename(name):
    return re.sub(r'[<>:"/\\|?*]', '', name).strip()


def _check_dependencies():
    errors = []
    if not shutil.which("ffmpeg"):
        errors.append("ffmpeg n'est pas installé")
    if not shutil.which("yt-dlp"):
        errors.append("yt-dlp n'est pas installé (pip install yt-dlp)")
    if not shutil.which("spotdl"):
        errors.append("spotdl n'est pas installé (pip install spotdl)")
    return errors


def _is_bot_check_error(output):
    low = (output or '').lower()
    return any(marker in low for marker in _BOT_CHECK_MARKERS)


def _simulate_progress(job_id, spotify_jobs, stop_event, start_pct=10, end_pct=75, duration=90):
    steps = 40
    interval = duration / steps
    increment = (end_pct - start_pct) / steps
    for i in range(steps):
        if stop_event.is_set():
            return
        time.sleep(interval)
        if stop_event.is_set():
            return
        new_pct = int(start_pct + increment * (i + 1))
        if spotify_jobs.get(job_id, {}).get('status') == 'downloading':
            spotify_jobs[job_id]['progress'] = min(new_pct, end_pct)


def _get_spotify_metadata(spotify_url):
    """
    Récupère artiste + titre via l'API embed publique de Spotify.
    Parse l'URL pour extraire le track_id, puis appelle la page embed.
    Fonctionne sans clé API et depuis les IPs cloud (Render).
    """
    try:
        match = re.search(r'track/([A-Za-z0-9]+)', spotify_url)
        if not match:
            return None, None
        track_id = match.group(1)

        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        url = f'https://open.spotify.com/embed/track/{track_id}'
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()

        # Extraire les données JSON structurées __NEXT_DATA__
        for m in re.finditer(
            r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
            r.text, re.DOTALL
        ):
            data = json.loads(m.group(1))
            entity = (
                data.get('props', {})
                    .get('pageProps', {})
                    .get('state', {})
                    .get('data', {})
                    .get('entity', {})
            )
            if not entity:
                continue
            artist = entity.get('artists', [{}])[0].get('name', '')
            title = entity.get('title', '') or entity.get('name', '')
            if artist and title:
                return artist.strip(), title.strip()

    except Exception:
        pass

    # Fallback : spotdl save (peut marcher localement)
    try:
        result = subprocess.run(
            ['spotdl', 'save', spotify_url, '--save-file', '/dev/stdout'],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            if data and isinstance(data, list):
                song = data[0]
                artist = song.get('artist') or (song.get('artists', [''])[0] if song.get('artists') else '')
                title = song.get('name') or song.get('title') or ''
                if artist and title:
                    return artist.strip(), title.strip()
    except Exception:
        pass

    return None, None


def _download_ytdlp(query, output_dir, output_template, timeout=180):
    """
    Télécharge via yt-dlp. query peut être une URL YouTube ou un ytsearch:.
    Retourne (chemin_mp3_ou_None, sortie_texte).
    """
    cmd = [
        'yt-dlp',
        '--no-playlist',
        '--extract-audio',
        '--audio-format', 'mp3',
        '--audio-quality', '192K',
        # Contourne le blocage anti-bot YouTube rencontré depuis les IP cloud
        # (les clients android/tv ne demandent pas de PoToken contrairement au
        # client web par défaut).
        '--extractor-args', f'youtube:player_client={YTDLP_PLAYER_CLIENTS}',
        '--output', output_template,
        '--no-warnings',
        # Pas de --quiet pour voir les erreurs dans les logs
        query
    ]

    cookies_path = os.environ.get('YOUTUBE_COOKIES_PATH', '')
    if cookies_path and os.path.exists(cookies_path):
        cmd += ['--cookies', cookies_path]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        output = result.stdout + result.stderr
    except subprocess.TimeoutExpired as e:
        output = (e.stdout or '') + (e.stderr or '')
        mp3_files = glob.glob(os.path.join(output_dir, "*.mp3"))
        if mp3_files:
            return max(mp3_files, key=os.path.getmtime), output
        return None, output + "\n[Timeout yt-dlp]"

    mp3_files = glob.glob(os.path.join(output_dir, "*.mp3"))
    if mp3_files:
        return max(mp3_files, key=os.path.getmtime), output

    return None, output


def _download_spotdl_fallback(spotify_url, output_dir, timeout_per_provider=90):
    """
    Repli via spotdl, UN SOUS-PROCESSUS PAR FOURNISSEUR.

    Important : spotdl, quand on lui passe plusieurs fournisseurs avec
    `--audio a b c`, les essaie en interne dans une boucle SANS try/except
    entre eux. Si un fournisseur lève une exception non gérée (ex: Piped,
    qui tape en dur sur une seule instance piped.video souvent injoignable
    depuis les IP cloud -> JSONDecodeError), toute la recherche s'arrête et
    les fournisseurs suivants ne sont jamais tentés.

    En lançant nous-mêmes un sous-processus spotdl distinct par fournisseur,
    un crash sur l'un n'empêche pas d'essayer les suivants.
    """
    all_output = []
    downloaded_file = None

    for provider in SPOTDL_FALLBACK_PROVIDERS:
        all_output.append(f"--- fournisseur : {provider} ---")

        cmd = [
            'spotdl', 'download', spotify_url,
            '--output', os.path.join(output_dir, '{artist} - {title}.{ext}'),
            '--format', 'mp3', '--bitrate', '192k',
            '--overwrite', 'skip',
            '--audio', provider,
            '--dont-filter-results',
            # DEBUG : spotdl avale la vraie exception sous-jacente et n'affiche
            # qu'un message générique ("YT-DLP download error") sauf en debug.
            # On en a besoin pour voir la cause réelle, pas le symptôme.
            '--log-level', 'DEBUG',
        ]
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, cwd=output_dir
        )
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                all_output.append(line)

        try:
            proc.wait(timeout=timeout_per_provider)
        except subprocess.TimeoutExpired:
            proc.kill()
            all_output.append(f"[Timeout spotdl/{provider}]")

        audio_files = []
        for pat in ('*.mp3', '*.wav', '*.flac', '*.m4a', '*.ogg', '*.aac'):
            audio_files.extend(glob.glob(os.path.join(output_dir, pat)))

        if audio_files:
            downloaded_file = max(audio_files, key=os.path.getmtime)
            break  # ce fournisseur a réussi, inutile d'essayer les suivants

        # Rien de valide trouvé avec ce fournisseur : on nettoie avant le suivant
        for f in glob.glob(os.path.join(output_dir, "*")):
            try: os.remove(f)
            except Exception: pass

    return downloaded_file, '\n'.join(all_output)


def process_spotify_download(job_id, spotify_url, spotify_jobs):
    stop_event = threading.Event()

    try:
        _log(job_id, f"Démarrage : {spotify_url}")
        spotify_jobs[job_id] = {
            'status': 'downloading', 'progress': 5,
            'error': None, 'file_path': None
        }

        dep_errors = _check_dependencies()
        if dep_errors:
            raise RuntimeError("Dépendances manquantes : " + " | ".join(dep_errors))

        output_dir = os.path.join(UPLOAD_FOLDER, f"spotify_{job_id}")
        os.makedirs(output_dir, exist_ok=True)

        spotify_jobs[job_id]['progress'] = 10

        progress_thread = threading.Thread(
            target=_simulate_progress,
            args=(job_id, spotify_jobs, stop_event, 10, 75, 90),
            daemon=True
        )
        progress_thread.start()

        downloaded_file = None
        last_output = ''
        artist, title = None, None

        # ── Étape 1 : métadonnées Spotify ──
        spotify_jobs[job_id]['step'] = 'metadata'
        artist, title = _get_spotify_metadata(spotify_url)
        _log(job_id, f"Métadonnées : artist={artist!r} title={title!r}")

        if artist and title:
            safe_name = sanitize_filename(f"{artist} - {title}")
            tpl = os.path.join(output_dir, f"{safe_name}.%(ext)s")

            queries = [
                f"ytsearch1:{artist} - {title}",
                f"ytsearch1:{artist} {title} official audio",
                f"ytsearch1:{title} {artist} lyrics",
            ]

            for q_idx, query in enumerate(queries):
                spotify_jobs[job_id]['step'] = f'ytdlp-search-{q_idx + 1}'
                for f in glob.glob(os.path.join(output_dir, "*")):
                    try: os.remove(f)
                    except Exception: pass

                downloaded_file, last_output = _download_ytdlp(query, output_dir, tpl)
                _log(job_id, f"yt-dlp tentative {q_idx + 1} ({query}) :\n{last_output}")

                if downloaded_file:
                    break
                if _is_bot_check_error(last_output):
                    # YouTube bloque cette IP : inutile d'insister avec d'autres
                    # requêtes, on bascule directement sur le repli spotdl.
                    _log(job_id, "Blocage anti-bot YouTube détecté, abandon de yt-dlp.")
                    break

        # ── Étape 3 : repli spotdl (Piped / SoundCloud / Bandcamp) ──
        if not downloaded_file:
            spotify_jobs[job_id]['step'] = 'spotdl-fallback'
            for f in glob.glob(os.path.join(output_dir, "*")):
                try: os.remove(f)
                except Exception: pass

            downloaded_file, last_output = _download_spotdl_fallback(spotify_url, output_dir)
            _log(job_id, f"spotdl fallback :\n{last_output}")

        stop_event.set()

        if not downloaded_file:
            titre_detecte = f"{artist} - {title}" if artist and title else "non récupéré"
            _log(job_id, f"ÉCHEC TOTAL. Titre détecté : {titre_detecte}\nDernière sortie complète :\n{last_output}")
            raise RuntimeError(
                f"Impossible de télécharger depuis ce serveur.\n"
                f"Titre détecté : {titre_detecte}\n"
                f"Dernière sortie :\n{last_output[-600:]}"
            )

        spotify_jobs[job_id]['progress'] = 85

        # ── Nommage ──
        raw_name = os.path.splitext(os.path.basename(downloaded_file))[0]
        if artist and title:
            track_name = f"{artist} - {title}"
            download_name = sanitize_filename(f"{title} - {artist}.mp3")
        else:
            track_name = raw_name
            dash_idx = raw_name.find(' - ')
            if dash_idx != -1:
                art = raw_name[:dash_idx]
                tit = raw_name[dash_idx + 3:]
                download_name = sanitize_filename(f"{tit} - {art}.mp3")
            else:
                download_name = sanitize_filename(f"{raw_name}.mp3")

        actual_ext = os.path.splitext(downloaded_file)[1].lower()
        final_path = os.path.join(UPLOAD_FOLDER, f"spotify_{job_id}_final{actual_ext}")
        if os.path.exists(final_path):
            os.remove(final_path)
        os.rename(downloaded_file, final_path)

        for f in glob.glob(os.path.join(output_dir, "*")):
            try: os.remove(f)
            except: pass
        try: os.rmdir(output_dir)
        except: pass

        spotify_jobs[job_id].update({
            'status': 'done', 'progress': 100,
            'file_path': final_path,
            'track_name': track_name,
            'download_name': download_name
        })

    except subprocess.TimeoutExpired:
        stop_event.set()
        spotify_jobs[job_id] = {
            'status': 'error', 'progress': 0,
            'error': 'Téléchargement trop long (300s). Réessaie.',
            'file_path': None
        }
    except Exception as e:
        stop_event.set()
        spotify_jobs[job_id] = {
            'status': 'error', 'progress': 0,
            'error': str(e), 'file_path': None
        }