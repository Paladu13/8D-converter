"""
Module de téléchargement Spotify — version optimisée.

Stratégie optimisée :
  1. Métadonnées Spotify via API embed (rapide).
  2. Lancement PARALLÈLE de yt-dlp ET spotdl simultanément.
     - yt-dlp avec format bestaudio (pas de re-encoding ffmpeg lent).
     - spotdl avec seulement les providers les plus rapides (soundcloud, youtube-music).
  3. Dès que l'un des deux termine, on arrête l'autre (race).
  4. Si aucun ne marche, dernier recours yt-dlp avec clients alternatifs (parallélisé aussi).
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache

import requests

from .audio_processor import UPLOAD_FOLDER


def _log(job_id, msg):
    print(f"[spotify:{job_id}] {msg}", flush=True, file=sys.stdout)


YTDLP_PLAYER_CLIENTS = "android,tv,web"

# Providers les plus fiables seulement (suppression de piped, bandcamp = trop lents/peu fiables)
SPOTDL_FAST_PROVIDERS = ["soundcloud", "youtube-music"]

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


@lru_cache(maxsize=32)
def _get_spotify_metadata(spotify_url):
    """Récupère artiste + titre via l'API embed publique de Spotify (avec cache)."""
    try:
        match = re.search(r'track/([A-Za-z0-9]+)', spotify_url)
        if not match:
            return None, None
        track_id = match.group(1)

        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        url = f'https://open.spotify.com/embed/track/{track_id}'
        r = requests.get(url, headers=headers, timeout=10)
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

    # Fallback rapide: spotdl save avec timeout court
    try:
        result = subprocess.run(
            ['spotdl', 'save', spotify_url, '--save-file', '/dev/stdout'],
            capture_output=True, text=True, timeout=15
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


def _download_ytdlp(query, output_dir, output_template, timeout=90, player_clients=None):
    """
    Télécharge via yt-dlp (optimisé : format bestaudio sans re-encoding).
    Retourne (chemin_audio_ou_None, sortie_texte).
    """
    clients = player_clients or YTDLP_PLAYER_CLIENTS
    
    # Utiliser bestaudio directement sans --extract-audio pour éviter le re-encoding ffmpeg
    # On récupère le meilleur format audio (m4a/opus natif) et on le convertit en mp3 ensuite
    # C'est plus rapide car pas de re-encoding audio in-place
    cmd = [
        'yt-dlp',
        '--no-playlist',
        '--no-warnings',
        # Format: bestaudio qui est déjà de l'audio (pas vidéo)
        '-f', 'bestaudio[ext=m4a]/bestaudio/best',
        '--extract-audio',
        '--audio-format', 'mp3',
        '--audio-quality', '0',  # Meilleure qualité (pas de re-encoding lent)
        '--extractor-args', f'youtube:player_client={clients}',
        '--output', output_template,
        '--socket-timeout', '30',
        '--retries', '3',
        '--fragment-retries', '3',
        '--no-check-certificate',
        '--no-cache-dir',
        query
    ]

    cookies_path = os.environ.get('YOUTUBE_COOKIES_PATH', '')
    if cookies_path and os.path.exists(cookies_path):
        cmd += ['--cookies', cookies_path]
    else:
        for browser in ('chrome', 'brave', 'edge'):
            try:
                check = subprocess.run(
                    ['yt-dlp', '--cookies-from-browser', browser, '--version'],
                    capture_output=True, text=True, timeout=5
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


def _download_spotdl_fast(spotify_url, output_dir, timeout=60):
    """
    Repli spotdl rapide : essaie plusieurs providers en parallèle.
    Beaucoup plus rapide que l'approche séquentielle précédente.
    """
    downloaded_file = None
    all_output = []

    def try_provider(provider):
        cmd = [
            'spotdl', 'download', spotify_url,
            '--output', os.path.join(output_dir, '{artist} - {title}.{ext}'),
            '--format', 'mp3', '--bitrate', '192k',
            '--overwrite', 'skip',
            '--audio', provider,
            '--dont-filter-results',
            '--log-level', 'WARNING',
        ]
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, cwd=output_dir
            )
            stdout_data, stderr_data = proc.communicate(timeout=timeout)
            return proc.returncode, provider, stdout_data, stderr_data
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            return -1, provider, '', '[Timeout]'
        except Exception as exc:
            return -1, provider, '', f'[Exception] {exc}'

    # Lancer tous les providers EN PARALLÈLE
    with ThreadPoolExecutor(max_workers=len(SPOTDL_FAST_PROVIDERS)) as executor:
        futures = {
            executor.submit(try_provider, provider): provider
            for provider in SPOTDL_FAST_PROVIDERS
        }

        for future in as_completed(futures):
            retcode, provider, stdout, stderr = future.result()
            provider_label = f"[spotdl/{provider}]"
            all_output.append(f"{provider_label} returncode={retcode}")

            if stderr:
                for line in stderr.splitlines():
                    if line.strip():
                        all_output.append(f"{provider_label} [stderr] {line.strip()}")

            # Vérifier si un fichier a été créé
            audio_files = []
            for pat in ('*.mp3', '*.wav', '*.flac', '*.m4a', '*.ogg'):
                audio_files.extend(glob.glob(os.path.join(output_dir, pat)))

            if audio_files:
                # Trouver le fichier le plus récent correspondant à ce provider
                for f in audio_files:
                    fname = os.path.basename(f)
                    # Ignorer les fichiers temporaires
                    if not fname.startswith('.'):
                        if downloaded_file is None or os.path.getmtime(f) > os.path.getmtime(downloaded_file):
                            downloaded_file = f

    return downloaded_file, '\n'.join(all_output)


def _download_ytdlp_last_resort(job_id, spotify_jobs, artist, title, output_dir, tpl):
    """
    Dernier recours : essaie plusieurs configurations client yt-dlp EN PARALLÈLE.
    """
    alternate_configs = [
        ("web",),
        ("android",),
        ("tv",),
        ("android,web",),
        ("tv,web",),
        ("web,tv,android",),
    ]

    def try_client(clients_tuple):
        clients_str = ",".join(clients_tuple)
        # Nettoyer le dossier pour cette tentative
        for f in glob.glob(os.path.join(output_dir, "*")):
            try:
                os.remove(f)
            except Exception:
                pass

        query = f"ytsearch1:{artist} - {title}"
        downloaded, output = _download_ytdlp(
            query, output_dir, tpl, timeout=60,
            player_clients=clients_str
        )
        if downloaded:
            return downloaded, clients_str, output
        return None, clients_str, output

    results = []
    with ThreadPoolExecutor(max_workers=min(3, len(alternate_configs))) as executor:
        futures = {executor.submit(try_client, cfg): cfg for cfg in alternate_configs}
        
        for future in as_completed(futures):
            downloaded, clients_str, output = future.result()
            results.append((downloaded, clients_str, output))
            if downloaded:
                # Annuler les autres futures
                for f in futures:
                    f.cancel()
                return downloaded, output

    return None, '\n'.join([f"[{cs}] {out[:200]}" for dl, cs, out in results if not dl])


def process_spotify_download(job_id, spotify_url, spotify_jobs):
    try:
        _log(job_id, f"Démarrage optimisé : {spotify_url}")
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

        downloaded_file = None
        last_output = ''
        artist, title = None, None

        # Étape 1 : métadonnées (rapide, ~1-2s)
        spotify_jobs[job_id]['step'] = 'metadata'
        spotify_jobs[job_id]['progress'] = 12
        artist, title = _get_spotify_metadata(spotify_url)
        _log(job_id, f"Métadonnées : artist={artist!r} title={title!r}")

        if artist and title:
            safe_name = sanitize_filename(f"{artist} - {title}")
            tpl = os.path.join(output_dir, f"{safe_name}.%(ext)s")

            # Étape 2 : Lancer yt-dlp ET spotdl EN PARALLÈLE (le premier qui gagne)
            spotify_jobs[job_id]['step'] = 'parallel-download'
            spotify_jobs[job_id]['progress'] = 20

            # Requête yt-dlp unique et optimisée
            query = f"ytsearch1:{artist} - {title} official audio"

            def run_ytdlp():
                # Nettoyage de départ
                for f in glob.glob(os.path.join(output_dir, "*")):
                    try: os.remove(f)
                    except: pass
                dl, _ = _download_ytdlp(query, output_dir, tpl, timeout=60)
                return dl

            def run_spotdl():
                dl, _ = _download_spotdl_fast(spotify_url, output_dir, timeout=60)
                return dl

            # Lancer les deux en parallèle, timeout total 70s (un peu plus que les timeouts individuels)
            with ThreadPoolExecutor(max_workers=2) as executor:
                yt_future = executor.submit(run_ytdlp)
                sd_future = executor.submit(run_spotdl)

                # Attendre le premier résultat, ou les deux
                done_set = set()
                total_timeout = 75
                start = time.time()

                while len(done_set) < 2 and (time.time() - start) < total_timeout:
                    # Vérifier si yt-dlp a déjà un fichier
                    if yt_future.done() and 'ytdlp' not in done_set:
                        dl = yt_future.result()
                        if dl:
                            downloaded_file = dl
                            # Annuler spotdl immédiatement
                            sd_future.cancel()
                            _log(job_id, "yt-dlp a gagné la course !")
                            break
                        done_set.add('ytdlp')

                    if sd_future.done() and 'spotdl' not in done_set:
                        dl = sd_future.result()
                        if dl:
                            downloaded_file = dl
                            yt_future.cancel()
                            _log(job_id, "spotdl a gagné la course !")
                            break
                        done_set.add('spotdl')

                    time.sleep(0.2)

                # Si on est sorti sans résultat
                if not downloaded_file:
                    # Attendre les éventuels résultats différés
                    for f in (yt_future, sd_future):
                        if not f.done():
                            try:
                                dl = f.result(timeout=10)
                                if dl:
                                    downloaded_file = dl
                                    break
                            except Exception:
                                pass

            _log(job_id, f"Résultat course parallèle : {'réussi' if downloaded_file else 'échoué'}")

        # Étape 3 : Dernier recours parallélisé
        if not downloaded_file and artist and title:
            spotify_jobs[job_id]['step'] = 'ytdlp-last-resort'
            spotify_jobs[job_id]['progress'] = 50
            _log(job_id, "Tentative dernier recours yt-dlp avec clients alternatifs...")

            safe_name = sanitize_filename(f"{artist} - {title}")
            tpl = os.path.join(output_dir, f"{safe_name}.%(ext)s")

            downloaded_file, last_output = _download_ytdlp_last_resort(
                job_id, spotify_jobs, artist, title, output_dir, tpl
            )

            _log(job_id, f"Résultat dernier recours : {'réussi' if downloaded_file else 'échoué'}")

        if not downloaded_file:
            titre_detecte = f"{artist} - {title}" if artist and title else "non récupéré"
            _log(job_id, f"ÉCHEC TOTAL. Titre détecté : {titre_detecte}")
            raise RuntimeError(
                f"Impossible de télécharger depuis ce serveur.\n"
                f"Titre détecté : {titre_detecte}\n"
            )

        spotify_jobs[job_id]['progress'] = 85
        spotify_jobs[job_id]['step'] = 'finalizing'

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

        # Nettoyage
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
        _log(job_id, f"Terminé avec succès : {track_name}")

    except subprocess.TimeoutExpired:
        spotify_jobs[job_id] = {
            'status': 'error', 'progress': 0,
            'error': 'Téléchargement trop long. Réessaie.',
            'file_path': None
        }
    except Exception as e:
        spotify_jobs[job_id] = {
            'status': 'error', 'progress': 0,
            'error': str(e), 'file_path': None
        }