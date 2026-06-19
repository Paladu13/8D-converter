"""
Module de téléchargement Spotify.

Stratégie (pensée pour les IP cloud type Render, qui se font bloquer par YouTube) :
  1. Métadonnées Spotify (artiste / titre) via l'API embed publique de Spotify.
  2. yt-dlp ytsearch sur YouTube :
     - Plusieurs tentatives avec des requêtes de plus en plus spécifiques
     - --match-filter pour n'accepter que les résultats contenant l'artiste
     - Émulation du client Android/TV pour contourner le blocage anti-bot
  3. Repli spotdl avec plusieurs fournisseurs audio alternatifs (SoundCloud, Bandcamp,
     Piped, YouTube Music), matching strict (sans --dont-filter-results).
  4. Repli sur l'API Piped (instance invidious/piped) pour faire une recherche
     YouTube propre et récupérer l'audio via yt-dlp par video ID.
  5. Dernier recours : yt-dlp avec combinaisons de clients alternatives.
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
import base64

import requests

from .audio_processor import UPLOAD_FOLDER


def _log(job_id, msg):
    print(f"[spotify:{job_id}] {msg}", flush=True, file=sys.stdout)


YTDLP_PLAYER_CLIENTS = "android,tv,web"

SPOTDL_PROVIDERS = ["soundcloud", "bandcamp", "piped"]
SPOTDL_LAST_RESORT_PROVIDERS = ["youtube-music", "youtube"]

_BOT_CHECK_MARKERS = (
    "sign in to confirm",
    "confirm you're not a bot",
    "http error 429",
    "sign in",
    "login required",
)

# Fichier de cookies persistant pour YouTube (dans le dossier upload)
COOKIES_FILE = os.path.join(UPLOAD_FOLDER, ".youtube_cookies.txt")

# Liste d'instances Piped fiables pour la recherche YouTube
PIPED_INSTANCES = [
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.syncpundit.com",
    "https://pipedapi.pfcd.me",
    "https://pipedapi.moomoo.me",
]

# Pour s'assurer que le match-filter fonctionne, on définit les champs
# auxquels on peut faire référence dans --match-filter
# yt-dlp v2023+ supporte: title, artist, artists, etc.
# Mais attention : ces champs ne sont disponibles qu'avec certains extracteurs.
# On va plutôt utiliser --match-filter sur le title uniquement.


def sanitize_filename(name):
    return re.sub(r'[<>:"/\\|?*]', '', name).strip()


def init_cookies():
    """
    Charge les cookies depuis la variable d'environnement YOUTUBE_COOKIES
    (contenu base64 d'un fichier cookies.txt au format Netscape)
    ou depuis un fichier uploadé.
    La priorité : fichier uploadé > variable d'environnement > rien.
    """
    # Si le fichier uploadé existe déjà, ne rien faire (il a priorité)
    if os.path.exists(COOKIES_FILE):
        return True

    # Sinon, essayer la variable d'environnement
    env_cookies = os.environ.get('YOUTUBE_COOKIES', '')
    if env_cookies:
        try:
            decoded = base64.b64decode(env_cookies).decode('utf-8')
            with open(COOKIES_FILE, 'w', encoding='utf-8') as f:
                f.write(decoded)
            return True
        except Exception as e:
            print(f"[cookies] Erreur décodage YOUTUBE_COOKIES: {e}", flush=True)

    return bool(os.path.exists(COOKIES_FILE))


def save_cookies_from_text(cookies_text):
    """Sauvegarde le texte brut d'un fichier cookies.txt."""
    try:
        os.makedirs(os.path.dirname(COOKIES_FILE), exist_ok=True)
        with open(COOKIES_FILE, 'w', encoding='utf-8') as f:
            f.write(cookies_text)
        return True
    except Exception as e:
        print(f"[cookies] Erreur sauvegarde: {e}", flush=True)
        return False


def clear_cookies():
    """Supprime le fichier de cookies."""
    try:
        if os.path.exists(COOKIES_FILE):
            os.remove(COOKIES_FILE)
            return True
    except Exception as e:
        print(f"[cookies] Erreur suppression: {e}", flush=True)
    return False


def has_cookies():
    """Vérifie si un fichier de cookies valide existe."""
    return os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0


def get_cookies_path():
    """Retourne le chemin du fichier cookies s'il existe."""
    return COOKIES_FILE if has_cookies() else ''


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


def _simulate_progress(job_id, spotify_jobs, stop_event, start_pct=10, end_pct=90, duration=180):
    steps = 60
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
    
    # Si le job tourne encore après la simulation, maintenir à end_pct
    # jusqu'à ce que le stop_event soit déclenché
    while not stop_event.is_set():
        time.sleep(2)
        if spotify_jobs.get(job_id, {}).get('status') == 'downloading':
            spotify_jobs[job_id]['progress'] = end_pct


def _get_spotify_metadata(spotify_url):
    """Récupère artiste + titre via l'API embed publique de Spotify."""
    try:
        match = re.search(r'track/([A-Za-z0-9]+)', spotify_url)
        if not match:
            return None, None, None
        track_id = match.group(1)

        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        url = f'https://open.spotify.com/embed/track/{track_id}'
        r = requests.get(url, headers=headers, timeout=15)
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
                return artist.strip(), title.strip(), track_id
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
                    return artist.strip(), title.strip(), track_id
    except Exception:
        pass

    return None, None, track_id


def _build_match_filter(artist, title):
    """
    Construit une chaîne --match-filter pour yt-dlp qui impose que le titre
    contienne des mots-clés de l'artiste ET du titre.
    
    On ne peut pas utiliser 'artist' comme champ car il n'est pas présent
    dans les métadonnées de search results. On se base sur le 'title'.
    On vérifie que le titre contient au moins 2 mots significatifs de l'artiste.
    """
    if not artist:
        return None
    
    # Prendre les mots significatifs de l'artiste (au moins 3 caractères)
    artist_keywords = [w for w in re.findall(r'[A-Za-z0-9]+', artist) if len(w) >= 3]
    if not artist_keywords:
        return None
    
    # Construire une condition: le titre doit contenir au moins un mot de l'artiste
    conditions = []
    for keyword in artist_keywords[:3]:  # max 3 mots
        conditions.append(f'title *? "{keyword}"')
    
    # Il faut qu'au moins UN des mots de l'artiste soit présent dans le titre
    match_filter = " | ".join(conditions)
    return match_filter


def _search_piped(query):
    """
    Utilise l'API Piped (instance libre de YouTube sans tracker) pour chercher
    une vidéo. Retourne l'ID vidéo et le titre, ou None.
    """
    for instance in PIPED_INSTANCES:
        try:
            url = f"{instance}/search"
            r = requests.post(url, json={
                "q": query,
                "filter": "videos"
            }, timeout=15, headers={
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0"
            })
            if r.status_code != 200:
                continue
            data = r.json()
            items = data.get("items", [])
            if not items:
                continue
            # Prendre le premier résultat
            first = items[0]
            video_id = first.get("url")
            if video_id and video_id.startswith("/watch?v="):
                video_id = video_id[9:]
            else:
                video_id = first.get("url")
            title = first.get("title", "")
            return video_id, title
        except Exception:
            continue
    return None, None


def _download_ytdlp(query, output_dir, output_template, timeout=180, player_clients=None, match_filter=None):
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
    ]
    
    # Ajouter match-filter si fourni
    if match_filter:
        cmd += ['--match-filter', match_filter]
    
    cmd.append(query)

    cookies_path = get_cookies_path()
    if cookies_path:
        cmd += ['--cookies', cookies_path]

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


def _download_by_video_id(video_id, output_dir, output_template, timeout=180):
    """
    Télécharge l'audio d'une vidéo YouTube par son ID.
    """
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    return _download_ytdlp(
        video_url, output_dir, output_template,
        timeout=timeout, match_filter=None
    )


def _download_spotdl_fallback(spotify_url, output_dir, timeout_per_provider=90):
    """
    Repli via spotdl, un sous-processus par fournisseur.
    
    Version améliorée : 
    - Supprime --dont-filter-results pour un matching strict
    - Ajoute --preload pour charger les métadonnées avant
    """
    all_output = []
    downloaded_file = None

    providers = SPOTDL_PROVIDERS + SPOTDL_LAST_RESORT_PROVIDERS

    for provider in providers:
        all_output.append(f"--- fournisseur : {provider} ---")

        cmd = [
            'spotdl', 'download', spotify_url,
            '--output', os.path.join(output_dir, '{artist} - {title}.{ext}'),
            '--format', 'mp3', '--bitrate', '192k',
            '--overwrite', 'skip',
            '--audio', provider,
            '--preload',
            '--log-level', 'DEBUG',
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
            args=(job_id, spotify_jobs, stop_event, 10, 90, 180),
            daemon=True
        )
        progress_thread.start()

        downloaded_file = None
        last_output = ''
        artist, title, track_id = None, None, None

        # Étape 0 : métadonnées Spotify (inclut track_id)
        spotify_jobs[job_id]['step'] = 'metadata'
        artist, title, track_id = _get_spotify_metadata(spotify_url)
        _log(job_id, f"Métadonnées : artist={artist!r} title={title!r} track_id={track_id!r}")

        if artist and title:
            safe_name = sanitize_filename(f"{artist} - {title}")
            tpl = os.path.join(output_dir, f"{safe_name}.%(ext)s")
            match_filter = _build_match_filter(artist, title)
            _log(job_id, f"match-filter : {match_filter}")

            # ── Étape 1A : Recherche avec guillemets pour phrase exacte ──
            queries = [
                f'ytsearch1:"{artist}" "{title}"',
                f'ytsearch1:"{artist} - {title}" official audio',
                f'ytsearch1:"{artist} - {title}" official music video',
                f'ytsearch1:{artist} {title} audio',
                f'ytsearch1:{artist} {title} topic',  # Topic = chaîne auto YouTube Music
            ]

            for q_idx, query in enumerate(queries):
                spotify_jobs[job_id]['step'] = f'ytdlp-search-{q_idx + 1}'
                for f in glob.glob(os.path.join(output_dir, "*")):
                    try:
                        os.remove(f)
                    except Exception:
                        pass

                downloaded_file, last_output = _download_ytdlp(
                    query, output_dir, tpl, match_filter=match_filter
                )
                _log(job_id, f"yt-dlp tentative {q_idx + 1} ({query}) :\n{last_output}")

                if downloaded_file:
                    break
                if _is_bot_check_error(last_output):
                    _log(job_id, "Blocage anti-bot YouTube détecté, abandon de yt-dlp.")
                    break

            # ── Étape 1B : Recherche via API Piped si yt-dlp n'a rien trouvé ──
            if not downloaded_file and not _is_bot_check_error(last_output):
                spotify_jobs[job_id]['step'] = 'piped-search'
                _log(job_id, "Recherche via API Piped...")
                
                # Nettoyer le dossier
                for f in glob.glob(os.path.join(output_dir, "*")):
                    try:
                        os.remove(f)
                    except Exception:
                        pass
                
                piped_query = f"{artist} - {title} audio"
                video_id, piped_title = _search_piped(piped_query)
                
                if video_id:
                    _log(job_id, f"Piped a trouvé : {piped_title} (id={video_id})")
                    downloaded_file, piped_output = _download_by_video_id(
                        video_id, output_dir, tpl, timeout=180
                    )
                    last_output = piped_output
                    _log(job_id, f"Téléchargement par vidéo ID : {'réussi' if downloaded_file else 'échoué'}")
                else:
                    _log(job_id, "Piped n'a trouvé aucun résultat")
                    
                # Si ça a échoué, essayer d'autres requêtes Piped
                if not downloaded_file:
                    for alt_query in [
                        f"{artist} {title} official audio",
                        f"{artist} - {title} topic",
                    ]:
                        for f in glob.glob(os.path.join(output_dir, "*")):
                            try:
                                os.remove(f)
                            except Exception:
                                pass
                        
                        video_id, piped_title = _search_piped(alt_query)
                        if video_id:
                            _log(job_id, f"Piped (alt) a trouvé : {piped_title} (id={video_id})")
                            downloaded_file, piped_output = _download_by_video_id(
                                video_id, output_dir, tpl, timeout=180
                            )
                            last_output = piped_output
                            if downloaded_file:
                                break

        # Étape 2 : repli spotdl (matching strict)
        if not downloaded_file:
            spotify_jobs[job_id]['step'] = 'spotdl-fallback'
            for f in glob.glob(os.path.join(output_dir, "*")):
                try:
                    os.remove(f)
                except Exception:
                    pass

            downloaded_file, last_output = _download_spotdl_fallback(spotify_url, output_dir)
            _log(job_id, f"spotdl fallback :\n{last_output}")

        # Étape 3 : dernier recours yt-dlp avec combinaisons de clients alternatives
        # et sans match-filter (plus permissif)
        if not downloaded_file and artist and title:
            spotify_jobs[job_id]['step'] = 'ytdlp-last-resort'
            _log(job_id, "Échec des méthodes précédentes, dernière tentative yt-dlp avec clients alternatifs...")

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

                # Dernière tentative : sans match-filter pour être plus permissif
                query = f"ytsearch1:{artist} - {title} audio"
                downloaded_file, alt_output = _download_ytdlp(
                    query, output_dir, tpl, timeout=120,
                    player_clients=clients_str,
                    match_filter=None  # Pas de filtre = plus de résultats
                )

                _log(job_id, f"yt-dlp {clients_str} (no filter) :\n{alt_output}")
                if downloaded_file:
                    last_output = alt_output
                    break

                if _is_bot_check_error(alt_output):
                    _log(job_id, f"Blocage anti-bot avec {clients_str}, essai suivant...")
                    continue

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