"""
Module de téléchargement Spotify.
Contient la logique de téléchargement via spotdl.
"""
import os
import re
import glob
import shutil
import subprocess
import threading
import time

from .audio_processor import UPLOAD_FOLDER


def sanitize_filename(name):
    """Supprime les caractères invalides pour un nom de fichier Windows."""
    return re.sub(r'[<>:"/\\|?*]', '', name).strip()


def _check_dependencies():
    """Vérifie que spotdl et ffmpeg sont disponibles."""
    errors = []
    if not shutil.which("spotdl"):
        errors.append("spotdl n'est pas installé (pip install spotdl)")
    if not shutil.which("ffmpeg"):
        errors.append("ffmpeg n'est pas installé")
    return errors


def _simulate_progress(job_id, spotify_jobs, stop_event, start_pct=10, end_pct=75, duration=60):
    """
    Monte la progression de start_pct à end_pct sur ~duration secondes.
    S'arrête dès que stop_event est déclenché.
    """
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
        # Ne jamais dépasser end_pct ni écraser un statut d'erreur
        if spotify_jobs.get(job_id, {}).get('status') == 'downloading':
            spotify_jobs[job_id]['progress'] = min(new_pct, end_pct)


def process_spotify_download(job_id, spotify_url, spotify_jobs):
    """Télécharge une musique depuis un lien Spotify en utilisant spotdl."""
    process = None
    stop_event = threading.Event()

    try:
        spotify_jobs[job_id] = {
            'status': 'downloading',
            'progress': 5,
            'error': None,
            'file_path': None
        }

        # ── Vérification des dépendances ──
        dep_errors = _check_dependencies()
        if dep_errors:
            raise RuntimeError("Dépendances manquantes : " + " | ".join(dep_errors))

        output_dir = os.path.join(UPLOAD_FOLDER, f"spotify_{job_id}")
        os.makedirs(output_dir, exist_ok=True)

        spotify_jobs[job_id]['progress'] = 10

        # ── Lancement de la progression simulée en parallèle ──
        progress_thread = threading.Thread(
            target=_simulate_progress,
            args=(job_id, spotify_jobs, stop_event, 10, 75, 90),
            daemon=True
        )
        progress_thread.start()

        cmd = [
            'spotdl', 'download', spotify_url,
            '--output', os.path.join(output_dir, '{artist} - {title}.{ext}'),
            '--format', 'mp3',
            '--bitrate', '192k',
            '--overwrite', 'skip'
        ]

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,   # merge stderr dans stdout pour tout capturer
            text=True,
            cwd=output_dir
        )

        # ── Lecture ligne par ligne pour détecter les erreurs tôt ──
        output_lines = []
        for line in process.stdout:
            line = line.rstrip()
            if line:
                output_lines.append(line)
                # Détection précoce d'erreurs connues
                line_lower = line.lower()
                if 'error' in line_lower or 'failed' in line_lower or 'exception' in line_lower:
                    # On note mais on continue — spotdl peut logger des warnings non fatals
                    pass

        process.wait(timeout=180)

        # ── Arrêt de la progression simulée ──
        stop_event.set()

        if process.returncode != 0:
            full_output = "\n".join(output_lines[-20:])  # dernières 20 lignes
            # Messages d'erreur courants et plus clairs
            if 'no results' in full_output.lower() or 'not found' in full_output.lower():
                raise RuntimeError("Musique introuvable sur YouTube Music. Essaie un autre lien Spotify.")
            if 'ffmpeg' in full_output.lower():
                raise RuntimeError("Erreur ffmpeg lors de la conversion. Vérifie l'installation de ffmpeg.")
            if 'premium' in full_output.lower():
                raise RuntimeError("Ce contenu nécessite un compte Spotify Premium ou n'est pas disponible.")
            raise RuntimeError(
                f"spotdl a échoué (code {process.returncode}).\n"
                f"Détails : {full_output[-400:] if full_output else 'Aucune sortie capturée.'}"
            )

        spotify_jobs[job_id]['progress'] = 80

        # ── Recherche du fichier MP3 généré ──
        mp3_files = glob.glob(os.path.join(output_dir, "*.mp3"))
        if not mp3_files:
            # Cherche aussi d'autres formats au cas où la conversion a partiellement réussi
            any_files = glob.glob(os.path.join(output_dir, "*"))
            detail = f"Fichiers présents : {[os.path.basename(f) for f in any_files]}" if any_files else "Dossier vide."
            raise RuntimeError(f"Aucun fichier MP3 trouvé après téléchargement. {detail}")

        downloaded_file = max(mp3_files, key=os.path.getmtime)

        # Nom brut du fichier : "Artist - Title" (template spotdl)
        raw_name = os.path.splitext(os.path.basename(downloaded_file))[0]
        track_name = raw_name  # exposé tel quel au frontend pour l'affichage

        # Nom du fichier de téléchargement : "Title - Artist.mp3"
        dash_idx = raw_name.find(' - ')
        if dash_idx != -1:
            artist = raw_name[:dash_idx]
            title  = raw_name[dash_idx + 3:]
            download_name = f"{title} - {artist}.mp3"
        else:
            download_name = f"{raw_name}.mp3"
        download_name = sanitize_filename(download_name)

        final_path = os.path.join(UPLOAD_FOLDER, f"spotify_{job_id}_final.mp3")
        if os.path.exists(final_path):
            os.remove(final_path)
        os.rename(downloaded_file, final_path)

        # ── Nettoyage du dossier temporaire ──
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
        if process:
            process.kill()
        spotify_jobs[job_id] = {
            'status': 'error',
            'progress': 0,
            'error': 'Le téléchargement a pris trop de temps (limite 180s). Réessaie ou vérifie ta connexion.',
            'file_path': None
        }
    except Exception as e:
        stop_event.set()
        if process and process.poll() is None:
            process.kill()
        spotify_jobs[job_id] = {
            'status': 'error',
            'progress': 0,
            'error': str(e),
            'file_path': None
        }