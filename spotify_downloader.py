"""
Module de téléchargement Spotify.
Stratégie sur Render : spotdl metadata → yt-dlp ytsearch YouTube direct.
YouTube Music est blacklisté sur les IPs cloud, on évite complètement spotdl download.
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
    return re.sub(r'[<>:"/\\|?*]', '', name).strip()


def _check_dependencies():
    errors = []
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
    Récupère artiste + titre via l'API publique Spotify (pas besoin de clé).
    Parse l'URL pour extraire le track_id, puis appelle l'embed API.
    """
    try:
        # Extraire le track ID de l'URL
        match = re.search(r'track/([A-Za-z0-9]+)', spotify_url)
        if not match:
            return None, None
        track_id = match.group(1)

        # Appel à l'API embed Spotify (publique, sans auth)
        cmd = [
            'yt-dlp',
            '--no-playlist',
            '--skip-download',
            '--print', '%(artist)s|||%(title)s',
            f'https://open.spotify.com/track/{track_id}'
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if result.returncode == 0 and '|||' in result.stdout:
            parts = result.stdout.strip().split('|||')
            if len(parts) == 2 and parts[0] and parts[1]:
                return parts[0].strip(), parts[1].strip()
    except Exception:
        pass

    # Fallback : spotdl save
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


def _download_ytdlp(query, output_dir, output_template):
    """
    Télécharge via yt-dlp. query peut être une URL YouTube ou un ytsearch:.
    Retourne le chemin mp3 ou None.
    """
    cmd = [
        'yt-dlp',
        '--no-playlist',
        '--extract-audio',
        '--audio-format', 'mp3',
        '--audio-quality', '192K',
        '--output', output_template,
        '--no-warnings',
        # Pas de --quiet pour voir les erreurs dans les logs
        query
    ]

    cookies_path = os.environ.get('YOUTUBE_COOKIES_PATH', '')
    if cookies_path and os.path.exists(cookies_path):
        cmd += ['--cookies', cookies_path]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

    mp3_files = glob.glob(os.path.join(output_dir, "*.mp3"))
    if mp3_files:
        return max(mp3_files, key=os.path.getmtime), result.stdout + result.stderr

    return None, result.stdout + result.stderr


def process_spotify_download(job_id, spotify_url, spotify_jobs):
    stop_event = threading.Event()

    try:
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

        if artist and title:
            safe_name = sanitize_filename(f"{artist} - {title}")
            tpl = os.path.join(output_dir, f"{safe_name}.%(ext)s")

            # ── Étape 2a : yt-dlp ytsearch YouTube (marche sur Render) ──
            spotify_jobs[job_id]['step'] = 'ytdlp-search'
            query = f"ytsearch1:{artist} - {title}"
            downloaded_file, last_output = _download_ytdlp(query, output_dir, tpl)

            # ── Étape 2b : yt-dlp ytsearch avec "official audio" ──
            if not downloaded_file:
                for f in glob.glob(os.path.join(output_dir, "*")):
                    try: os.remove(f)
                    except: pass
                query2 = f"ytsearch1:{artist} {title} official audio"
                downloaded_file, last_output = _download_ytdlp(query2, output_dir, tpl)

            # ── Étape 2c : yt-dlp ytsearch lyrics/topic ──
            if not downloaded_file:
                for f in glob.glob(os.path.join(output_dir, "*")):
                    try: os.remove(f)
                    except: pass
                query3 = f"ytsearch1:{title} {artist} lyrics"
                downloaded_file, last_output = _download_ytdlp(query3, output_dir, tpl)

        # ── Étape 3 : spotdl SoundCloud (sans YouTube Music) ──
        if not downloaded_file:
            spotify_jobs[job_id]['step'] = 'soundcloud'
            for f in glob.glob(os.path.join(output_dir, "*")):
                try: os.remove(f)
                except: pass

            cmd_sc = [
                'spotdl', 'download', spotify_url,
                '--output', os.path.join(output_dir, '{artist} - {title}.{ext}'),
                '--format', 'mp3', '--bitrate', '192k',
                '--overwrite', 'skip',
                '--audio', 'soundcloud',
            ]
            proc = subprocess.Popen(
                cmd_sc, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, cwd=output_dir
            )
            lines = []
            for line in proc.stdout:
                line = line.rstrip()
                if line: lines.append(line)
            proc.wait(timeout=300)
            last_output = '\n'.join(lines)

            audio_files = []
            for pat in ('*.mp3', '*.wav', '*.flac', '*.m4a', '*.ogg', '*.aac'):
                audio_files.extend(glob.glob(os.path.join(output_dir, pat)))
            if audio_files:
                downloaded_file = max(audio_files, key=os.path.getmtime)

        stop_event.set()

        if not downloaded_file:
            titre_detecte = f"{artist} - {title}" if artist and title else "non récupéré"
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