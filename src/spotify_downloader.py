"""Module de téléchargement depuis Spotify uniquement."""
import os
import re
import json
import glob
import shutil
import subprocess
import sys

import requests
from .audio_processor import UPLOAD_FOLDER


def _log(job_id, msg):
    print(f"[spotify:{job_id}] {msg}", flush=True, file=sys.stdout)


def _check_deps():
    """Vérifie les dépendances."""
    if not shutil.which("librespot"):
        raise RuntimeError("librespot n'est pas installé (voir instructions)")


def _get_track_id(spotify_url):
    """Extrait l'ID de piste."""
    match = re.search(r'track/([A-Za-z0-9]+)', spotify_url)
    return match.group(1) if match else None


def _download_with_librespot(track_id, output_path, job_id):
    """Télécharge directement depuis Spotify avec librespot."""
    cmd = [
        'librespot',
        '--username', os.environ.get('SPOTIFY_USERNAME', ''),
        '--password', os.environ.get('SPOTIFY_PASSWORD', ''),
        '--track-id-only', track_id,
        '--output', output_path,
        '--bitrate', '320',
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            return output_path if os.path.exists(output_path) else None
        else:
            _log(job_id, f"Erreur librespot: {result.stderr}")
            return None
    except subprocess.TimeoutExpired:
        raise TimeoutError("Téléchargement trop long")
    except Exception as e:
        _log(job_id, f"Exception: {e}")
        return None


def _get_metadata(spotify_url):
    """Récupère artiste + titre."""
    try:
        track_id = _get_track_id(spotify_url)
        headers = {'User-Agent': 'Mozilla/5.0'}
        r = requests.get(
            f'https://open.spotify.com/embed/track/{track_id}',
            headers=headers, timeout=10
        )
        match = re.search(r'"name":"([^"]+)".*?"artists":\[\{"name":"([^"]+)"', r.text)
        if match:
            return match.group(2).strip(), match.group(1).strip()
    except Exception:
        pass
    return None, None


def _sanitize(name):
    """Nettoie le nom."""
    return re.sub(r'[<>:"/\\|?*]', '', name).strip()


def process_spotify_download(job_id, spotify_url, spotify_jobs):
    """Télécharge depuis Spotify uniquement."""
    try:
        _log(job_id, f"Démarrage : {spotify_url}")
        spotify_jobs[job_id] = {
            'status': 'downloading', 'progress': 10,
            'error': None, 'file_path': None
        }
        
        _check_deps()
        
        track_id = _get_track_id(spotify_url)
        if not track_id:
            raise ValueError("URL Spotify invalide")
        
        output_dir = os.path.join(UPLOAD_FOLDER, f"spotify_{job_id}")
        os.makedirs(output_dir, exist_ok=True)
        
        spotify_jobs[job_id]['progress'] = 30
        artist, title = _get_metadata(spotify_url)
        _log(job_id, f"Piste : {artist} - {title}")
        
        spotify_jobs[job_id]['progress'] = 50
        output_path = os.path.join(output_dir, f"track.ogg")
        
        downloaded = _download_with_librespot(track_id, output_path, job_id)
        spotify_jobs[job_id]['progress'] = 85
        
        if not downloaded:
            raise RuntimeError("Téléchargement Spotify échoué")
        
        # Conversion OGG → MP3
        if downloaded.endswith('.ogg'):
            mp3_path = os.path.join(output_dir, "track.mp3")
            cmd = ['ffmpeg', '-i', downloaded, '-q:a', '5', mp3_path]
            subprocess.run(cmd, capture_output=True, timeout=60)
            if os.path.exists(mp3_path):
                os.remove(downloaded)
                downloaded = mp3_path
        
        track_name = f"{artist} - {title}" if artist and title else "track"
        safe_name = _sanitize(f"{title} - {artist}.mp3") if artist else "track.mp3"
        
        ext = os.path.splitext(downloaded)[1].lower()
        final = os.path.join(UPLOAD_FOLDER, f"spotify_{job_id}_final{ext}")
        os.rename(downloaded, final)
        
        shutil.rmtree(output_dir, ignore_errors=True)
        
        spotify_jobs[job_id].update({
            'status': 'done', 'progress': 100,
            'file_path': final,
            'track_name': track_name,
            'download_name': safe_name
        })
        _log(job_id, "✓ Succès depuis Spotify")
        
    except Exception as e:
        shutil.rmtree(os.path.join(UPLOAD_FOLDER, f"spotify_{job_id}"), ignore_errors=True)
        spotify_jobs[job_id] = {
            'status': 'error', 'progress': 0,
            'error': str(e), 'file_path': None
        }
        _log(job_id, f"✗ {e}")
