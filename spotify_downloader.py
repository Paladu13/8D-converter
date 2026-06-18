"""
Module de téléchargement Spotify.
Stratégie : spotdl metadata → yt-dlp recherche YouTube directe (pas YouTube Music).
YouTube Music est blacklisté sur Render, yt-dlp + YouTube search fonctionne.
"""
import os
import re
import json
import glob
import shutil
import subprocess
import threading
import time

from .audio_processor import UPLOAD_FOLDER


def sanitize_filename(name):
    """Supprime les caractères invalides pour un nom de fichier."""
    return re.sub(r'[<>:"/\\|?*]', '', name).strip()


def _check_dependencies():
    errors = []
    if not shutil.which("spotdl"):
        errors.append("spotdl n'est pas installé (pip install spotdl)")
    if not shutil.which("ffmpeg"):
        errors.append("ffmpeg n'est pas installé")
    if not shutil.which("yt-dlp"):
        errors.append("yt-dlp n'est pas installé (pip install yt-dlp)")
    return errors


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
    Utilise spotdl --save pour récupérer les métadonnées (titre, artiste)
    sans télécharger. Retourne (artist, title) ou (None, None) si échec.
    """
    try:
        result = subprocess.run(
            ['spotdl', 'save', spotify_url, '--save-file', '/dev/stdout'],
            capture_output=True, text=True, timeout=30
        )
        # spotdl save retourne un JSON avec la liste des chansons
        data = json.loads(result.stdout)
        if data and isinstance(data, list) and len(data) > 0:
            song = data[0]
            artist = song.get('artist') or (song.get('artists', [''])[0] if song.get('artists') else '')
            title = song.get('name') or song.get('title') or ''
            return artist.strip(), title.strip()
    except Exception:
        pass
    return None, None


def _download_via_ytdlp(artist, title, output_dir):
    """
    Télécharge via yt-dlp avec une recherche YouTube standard (ytsearch).
    Beaucoup moins restrictif que YouTube Music sur les serveurs cloud.
    Retourne le chemin du fichier téléchargé ou None.
    """
    search_query = f"ytsearch1:{artist} - {title} audio"
    output_template = os.path.join(output_dir, f"{artist} - {title}.%(ext)s")

    cmd = [
        'yt-dlp',
        '--no-playlist',
        '--extract-audio',
        '--audio-format', 'mp3',
        '--audio-quality', '192K',
        '--output', output_template,
        '--no-warnings',
        '--quiet',
        search_query
    ]

    # Cookies YouTube optionnels (montés sur Render via secret file)
    cookies_path = os.environ.get('YOUTUBE_COOKIES_PATH', '')
    if cookies_path and os.path.exists(cookies_path):
        cmd += ['--cookies', cookies_path]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

    mp3_files = glob.glob(os.path.join(output_dir, "*.mp3"))
    if mp3_files:
        return max(mp3_files, key=os.path.getmtime)

    return None


def _download_via_spotdl_soundcloud(spotify_url, output_dir):
    """
    Fallback : spotdl avec SoundCloud comme provider.
    """
    cmd = [
        'spotdl', 'download', spotify_url,
        '--output', os.path.join(output_dir, '{artist} - {title}.{ext}'),
        '--format', 'mp3',
        '--bitrate', '192k',
        '--overwrite', 'skip',
        '--audio', 'soundcloud',
    ]

    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, cwd=output_dir
    )
    output_lines = []
    for line in process.stdout:
        line = line.rstrip()
        if line:
            output_lines.append(line)
    process.wait(timeout=300)

    audio_files = []
    for pattern in ('*.mp3', '*.wav', '*.flac', '*.m4a', '*.ogg', '*.aac'):
        audio_files.extend(glob.glob(os.path.join(output_dir, pattern)))

    if audio_files:
        return max(audio_files, key=os.path.getmtime), output_lines

    return None, output_lines


def process_spotify_download(job_id, spotify_url, spotify_jobs):
    """Télécharge une musique depuis un lien Spotify."""
    stop_event = threading.Event()

    try:
        spotify_jobs[job_id] = {
            'status': 'downloading',
            'progress': 5,
            'error': None,
            'file_path': None
        }

        dep_errors = _check_dependencies()
        if dep_errors:
            raise RuntimeError("Dépendances manquantes : " + " | ".join(dep_errors))

        output_dir = os.path.join(UPLOAD_FOLDER, f"spotify_{job_id}")
        os.makedirs(output_dir, exist_ok=True)

        spotify_jobs[job_id]['progress'] = 10

        # ── Progression simulée ──
        progress_thread = threading.Thread(
            target=_simulate_progress,
            args=(job_id, spotify_jobs, stop_event, 10, 75, 90),
            daemon=True
        )
        progress_thread.start()

        downloaded_file = None
        artist, title = None, None
        last_error_lines = []

        # ── Étape 1 : récupérer les métadonnées Spotify ──
        spotify_jobs[job_id]['step'] = 'metadata'
        artist, title = _get_spotify_metadata(spotify_url)

        # ── Étape 2 : yt-dlp recherche YouTube standard (marche sur Render) ──
        if artist and title:
            spotify_jobs[job_id]['step'] = 'ytdlp'
            try:
                downloaded_file = _download_via_ytdlp(artist, title, output_dir)
            except Exception as e:
                last_error_lines = [str(e)]

        # ── Étape 3 : fallback spotdl + SoundCloud ──
        if not downloaded_file:
            spotify_jobs[job_id]['step'] = 'soundcloud'
            # Nettoyer avant retry
            for f in glob.glob(os.path.join(output_dir, "*")):
                try:
                    os.remove(f)
                except Exception:
                    pass
            downloaded_file, last_error_lines = _download_via_spotdl_soundcloud(
                spotify_url, output_dir
            )

        # ── Étape 4 : spotdl YouTube Music (dernière chance, peut échouer sur Render) ──
        if not downloaded_file:
            spotify_jobs[job_id]['step'] = 'spotdl-yt'
            for f in glob.glob(os.path.join(output_dir, "*")):
                try:
                    os.remove(f)
                except Exception:
                    pass
            cmd_yt = [
                'spotdl', 'download', spotify_url,
                '--output', os.path.join(output_dir, '{artist} - {title}.{ext}'),
                '--format', 'mp3', '--bitrate', '192k', '--overwrite', 'skip',
                '--audio', 'youtube-music',
            ]
            process_yt = subprocess.Popen(
                cmd_yt, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, cwd=output_dir
            )
            last_error_lines = []
            for line in process_yt.stdout:
                line = line.rstrip()
                if line:
                    last_error_lines.append(line)
            process_yt.wait(timeout=300)

            audio_files = []
            for pattern in ('*.mp3', '*.wav', '*.flac', '*.m4a', '*.ogg', '*.aac'):
                audio_files.extend(glob.glob(os.path.join(output_dir, pattern)))
            if audio_files:
                downloaded_file = max(audio_files, key=os.path.getmtime)

        stop_event.set()

        if not downloaded_file:
            full_output = "\n".join(last_error_lines[-20:])
            raise RuntimeError(
                f"Impossible de télécharger cette musique depuis ce serveur.\n"
                f"Titre détecté : {f'{artist} - {title}' if artist and title else 'non récupéré'}\n"
                f"Détails : {full_output[-400:] if full_output else 'Aucune sortie.'}"
            )

        spotify_jobs[job_id]['progress'] = 85

        # ── Nommage du fichier de sortie ──
        raw_name = os.path.splitext(os.path.basename(downloaded_file))[0]

        # Préférer les métadonnées Spotify pour le nom propre
        if artist and title:
            download_name = sanitize_filename(f"{title} - {artist}.mp3")
            track_name = f"{artist} - {title}"
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

        # ── Nettoyage ──
        for f in glob.glob(os.path.join(output_dir, "*")):
            try:
                os.remove(f)
            except Exception:
                pass
        try:
            os.rmdir(output_dir)
        except Exception:
            pass

        spotify_jobs[job_id]['status'] = 'done'
        spotify_jobs[job_id]['progress'] = 100
        spotify_jobs[job_id]['file_path'] = final_path
        spotify_jobs[job_id]['track_name'] = track_name
        spotify_jobs[job_id]['download_name'] = download_name

    except subprocess.TimeoutExpired:
        stop_event.set()
        spotify_jobs[job_id] = {
            'status': 'error', 'progress': 0,
            'error': 'Le téléchargement a pris trop de temps (300s). Réessaie.',
            'file_path': None
        }
    except Exception as e:
        stop_event.set()
        spotify_jobs[job_id] = {
            'status': 'error', 'progress': 0,
            'error': str(e), 'file_path': None
        }