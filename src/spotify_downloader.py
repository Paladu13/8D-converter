"""
Module de téléchargement Spotify.

Stratégie (pensée pour les IP cloud type Render, qui se font bloquer par YouTube) :
  1. Métadonnées Spotify (artiste / titre) via l'API embed publique de Spotify.
  2. yt-dlp ytsearch sur YouTube en émulant le client Android/TV, ce qui contourne
     dans la majorité des cas le blocage anti-bot ("Sign in to confirm you're not
     a bot") que subissent les IP de datacenter. Dès qu'un blocage de ce type est
     détecté, on arrête immédiatement les tentatives YouTube (inutile d'insister).
  3. Repli spotdl avec plusieurs fournisseurs audio alternatifs (SoundCloud, Bandcamp,
     Piped, YouTube Music, YouTube — essayés un par un pour éviter qu'un crash
     spotdl sur un fournisseur n'empêche les suivants), et un matching permissif
     (--dont-filter-results).
  4. Dernier recours : yt-dlp avec combinaisons de clients alternatives, et avec
     --cookies-from-browser si disponible.
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

# ── Configuration proxy Webshare ──
PROXY_HOST = "31.59.20.176"
PROXY_PORT = 6754
PROXY_USER = "gyysupas"
PROXY_PASS = "c92q2uwdjhvz"
PROXY_URL = f"http://{PROXY_USER}:{PROXY_PASS}@{PROXY_HOST}:{PROXY_PORT}/"
PROXIES = {
    "http": PROXY_URL,
    "https": PROXY_URL,
}


def _log(job_id, msg):
    print(f"[spotify:{job_id}] {msg}", flush=True, file=sys.stdout)


YTDLP_PLAYER_CLIENTS = "android,tv,web"

SPOTDL_FALLBACK_PROVIDERS = ["soundcloud", "bandcamp", "piped"]
SPOTDL_LAST_RESORT_PROVIDERS = ["youtube-music", "youtube"]

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
    """Récupère artiste + titre via l'API embed publique de Spotify."""
    try:
        match = re.search(r'track/([A-Za-z0-9]+)', spotify_url)
        if not match:
            return None, None
        track_id = match.group(1)

        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        url = f'https://open.spotify.com/embed/track/{track_id}'
        r = requests.get(url, headers=headers, timeout=15, proxies=PROXIES)
        r.raise_for_status()

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

    # Fallback: spotdl save
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


def _download_ytdlp(query, output_dir, output_template, timeout=180, player_clients=None):
    """
    Télécharge via yt-dlp. Retourne (chemin_mp3_ou_None, sortie_texte).
    """
    clients = player_clients or YTDLP_PLAYER_CLIENTS
    cmd = [
        'yt-dlp',
        '--no-playlist',
        '--extract-audio',
        '--audio-format', 'mp3',
        '--audio-quality', '192K',
        '--extractor-args', f'youtube:player_client={clients}',
        '--output', output_template,
        '--no-warnings',
        '--proxy', PROXY_URL,
        query
    ]

    cookies_path = os.environ.get('YOUTUBE_COOKIES_PATH', '')
    if cookies_path and os.path.exists(cookies_path):
        cmd += ['--cookies', cookies_path]
    else:
        # Tentative avec cookies-from-browser (chrome/edge/brave)
        for browser in ('chrome', 'brave', 'edge', 'opera', 'chromium'):
            try:
                check = subprocess.run(
                    ['yt-dlp', '--cookies-from-browser', browser, '--version'],
                    capture_output=True, text=True, timeout=10
                )
                if check.returncode == 0:
                    cmd += ['--cookies-from-browser', browser]
                    break
            except Exception:
                continue

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        output = result.stdout + result.stderr
    except subprocess.TimeoutExpired as e:
        output = (e.stdout or '') + (e.stderr or '') + "\n[Timeout yt-dlp]"
        mp3_files = glob.glob(os.path.join(output_dir, "*.mp3"))
        if mp3_files:
            return max(mp3_files, key=os.path.getmtime), output
        return None, output

    mp3_files = glob.glob(os.path.join(output_dir, "*.mp3"))
    if mp3_files:
        return max(mp3_files, key=os.path.getmtime), output

    return None, output


def _download_spotdl_fallback(spotify_url, output_dir, timeout_per_provider=90):
    """
    Repli via spotdl, un sous-processus par fournisseur.

    Certains fournisseurs (notamment Piped) peuvent faire crasher spotdl
    avec une IndexError interne. On capture stdout+stderr et on gère les
    exceptions proprement pour ne pas bloquer les fournisseurs suivants.
    """
    all_output = []
    downloaded_file = None

    providers = SPOTDL_FALLBACK_PROVIDERS + SPOTDL_LAST_RESORT_PROVIDERS

    for provider in providers:
        all_output.append(f"--- fournisseur : {provider} ---")

        cmd = [
            'spotdl', 'download', spotify_url,
            '--output', os.path.join(output_dir, '{artist} - {title}.{ext}'),
            '--format', 'mp3', '--bitrate', '192k',
            '--overwrite', 'skip',
            '--audio', provider,
            '--dont-filter-results',
            '--log-level', 'DEBUG',
            '--proxy', PROXY_URL,
        ]

        effective_timeout = timeout_per_provider
        if provider in ("youtube", "youtube-music"):
            effective_timeout = timeout_per_provider + 60

        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, cwd=output_dir
            )
            stdout_data, stderr_data = proc.communicate(timeout=effective_timeout)
            for line in stdout_data.splitlines():
                line = line.rstrip()
                if line:
                    all_output.append(line)
            for line in stderr_data.splitlines():
                line = line.rstrip()
                if line:
                    all_output.append(f"[stderr] {line}")
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            all_output.append(f"[Timeout spotdl/{provider}]")
        except Exception as exc:
            all_output.append(f"[Exception spotdl/{provider}] {exc}")

        audio_files = []
        for pat in ('*.mp3', '*.wav', '*.flac', '*.m4a', '*.ogg', '*.aac'):
            audio_files.extend(glob.glob(os.path.join(output_dir, pat)))

        if audio_files:
            downloaded_file = max(audio_files, key=os.path.getmtime)
            break

        for f in glob.glob(os.path.join(output_dir, "*")):
            try:
                os.remove(f)
            except Exception:
                pass

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
            args=(job_id, spotify_jobs, stop_event, 10, 75, 120),
            daemon=True
        )
        progress_thread.start()

        downloaded_file = None
        last_output = ''
        artist, title = None, None

        # Étape 1 : métadonnées Spotify
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
                    try:
                        os.remove(f)
                    except Exception:
                        pass

                downloaded_file, last_output = _download_ytdlp(query, output_dir, tpl)
                _log(job_id, f"yt-dlp tentative {q_idx + 1} ({query}) :\n{last_output}")

                if downloaded_file:
                    break
                if _is_bot_check_error(last_output):
                    _log(job_id, "Blocage anti-bot YouTube détecté, abandon de yt-dlp.")
                    break

        # Étape 2 : repli spotdl
        if not downloaded_file:
            spotify_jobs[job_id]['step'] = 'spotdl-fallback'
            for f in glob.glob(os.path.join(output_dir, "*")):
                try:
                    os.remove(f)
                except Exception:
                    pass

            downloaded_file, last_output = _download_spotdl_fallback(spotify_url, output_dir)
            _log(job_id, f"spotdl fallback :\n{last_output}")

        # Étape 3 : dernier recours yt-dlp avec clients alternatifs
        if not downloaded_file and artist and title:
            spotify_jobs[job_id]['step'] = 'ytdlp-last-resort'
            _log(job_id, "Spotdl a échoué, tentative yt-dlp avec clients alternatifs...")

            safe_name = sanitize_filename(f"{artist} - {title}")
            tpl = os.path.join(output_dir, f"{safe_name}.%(ext)s")

            alternate_client_configs = [
                ("web",),
                ("android",),
                ("tv",),
                ("android,web",),
                ("tv,web",),
                ("web,tv,android",),
            ]

            for clients_tuple in alternate_client_configs:
                for f in glob.glob(os.path.join(output_dir, "*")):
                    try:
                        os.remove(f)
                    except Exception:
                        pass

                clients_str = ",".join(clients_tuple)
                spotify_jobs[job_id]['step'] = f'ytdlp-alt-{clients_str}'

                query = f"ytsearch1:{artist} - {title}"
                downloaded_file, alt_output = _download_ytdlp(
                    query, output_dir, tpl, timeout=120,
                    player_clients=clients_str
                )

                _log(job_id, f"yt-dlp {clients_str} :\n{alt_output}")
                if downloaded_file:
                    last_output = alt_output
                    break

                if _is_bot_check_error(alt_output):
                    _log(job_id, f"Blocage anti-bot avec {clients_str}, essai suivant...")
                    continue

            _log(job_id, f"Résultat yt-dlp dernier recours : {'réussi' if downloaded_file else 'échoué'}")

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

        # Nommage
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
            try:
                os.remove(f)
            except Exception:
                pass
        try:
            os.rmdir(output_dir)
        except Exception:
            pass

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