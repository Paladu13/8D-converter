"""Module de téléchargement Spotify direct."""
import os
import re
import json
import glob
import shutil
import subprocess
import threading
import sys

import requests
from .audio_processor import UPLOAD_FOLDER


def _log(job_id, msg):
    print(f"[spotify:{job_id}] {msg}", flush=True, file=sys.stdout)


def _check_deps():
    """Vérifie les dépendances requises."""
    missing = []
    for tool in ["ffmpeg", "spotdl"]:
        if not shutil.which(tool):
            missing.append(tool)
    if missing:
        raise RuntimeError(f"Outils manquants : {', '.join(missing)}")


def _get_metadata(spotify_url):
    """Récupère artiste + titre depuis l'API Spotify publique."""
    try:
        track_id = re.search(r'track/([A-Za-z0-9]+)', spotify_url)
        if not track_id:
            return None, None
        
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        r = requests.get(
            f'https://open.spotify.com/embed/track/{track_id.group(1)}',
            headers=headers, timeout=15
        )
        
        match = re.search(r'"entity":\{[^}]*"name":"([^"]+)"[^}]*"artists":\[\{[^}]*"name":"([^"]+)"', r.text)
        if match:
            return match.group(2).strip(), match.group(1).strip()
    except Exception:
        pass
    return None, None


def _download_spotify(spotify_url, output_dir, job_id):
    """Télécharge directement depuis Spotify avec les meilleurs fournisseurs."""
    # Essai des fournisseurs dans cet ordre (plus proches de Spotify)
    providers = ['youtube-music', 'bandcamp', 'soundcloud', 'piped', 'youtube']
    
    for provider in providers:
        try:
            _log(job_id, f"Tentative avec {provider}...")
            cmd = [
                'spotdl', 'download', spotify_url,
                '--output', os.path.join(output_dir, '{artist} - {title}.{ext}'),
                '--format', 'mp3',
                '--bitrate', '192k',
                '--audio', provider,
                '--dont-filter-results',
                '--log-level', 'INFO',
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=180, cwd=output_dir)
            output = result.stdout + result.stderr
            
            # Cherche le fichier MP3 téléchargé
            for f in glob.glob(os.path.join(output_dir, "*.mp3")):
                _log(job_id, f"✓ Succès avec {provider}")
                return f, output
            
            _log(job_id, f"✗ Échec avec {provider}")
            
        except subprocess.TimeoutExpired:
            _log(job_id, f"Timeout avec {provider}")
            continue
        except Exception as e:
            _log(job_id, f"Erreur avec {provider}: {e}")
            continue
    
    return None, "Tous les fournisseurs ont échoué"


def _sanitize(name):
    """Nettoie le nom de fichier."""
    return re.sub(r'[<>:"/\\|?*]', '', name).strip()


def process_spotify_download(job_id, spotify_url, spotify_jobs):
    """Télécharge une piste Spotify."""
    try:
        _log(job_id, f"Démarrage : {spotify_url}")
        spotify_jobs[job_id] = {
            'status': 'downloading',
            'progress': 10,
            'error': None,
            'file_path': None
        }
        
        _check_deps()
        
        output_dir = os.path.join(UPLOAD_FOLDER, f"spotify_{job_id}")
        os.makedirs(output_dir, exist_ok=True)
        
        # Métadonnées
        artist, title = _get_metadata(spotify_url)
        _log(job_id, f"Métadonnées : {artist} - {title}")
        spotify_jobs[job_id]['progress'] = 30
        
        # Téléchargement direct Spotify
        spotify_jobs[job_id]['step'] = 'downloading'
        downloaded_file, output = _download_spotify(spotify_url, output_dir, job_id)
        spotify_jobs[job_id]['progress'] = 85
        
        if not downloaded_file:
            raise RuntimeError(f"Téléchargement échoué.\n{output[-500:]}")
        
        # Renommage
        track_name = f"{artist} - {title}" if artist and title else os.path.splitext(os.path.basename(downloaded_file))[0]
        safe_name = _sanitize(f"{title} - {artist}.mp3") if artist and title else "track.mp3"
        
        ext = os.path.splitext(downloaded_file)[1].lower()
        final_path = os.path.join(UPLOAD_FOLDER, f"spotify_{job_id}_final{ext}")
        os.rename(downloaded_file, final_path)
        
        # Nettoyage
        shutil.rmtree(output_dir, ignore_errors=True)
        
        spotify_jobs[job_id].update({
            'status': 'done',
            'progress': 100,
            'file_path': final_path,
            'track_name': track_name,
            'download_name': safe_name
        })
        _log(job_id, "✓ Succès")
        
    except TimeoutError as e:
        spotify_jobs[job_id] = {'status': 'error', 'progress': 0, 'error': str(e), 'file_path': None}
        _log(job_id, f"✗ {e}")
    except Exception as e:
        spotify_jobs[job_id] = {'status': 'error', 'progress': 0, 'error': str(e), 'file_path': None}
        _log(job_id, f"✗ {e}")
