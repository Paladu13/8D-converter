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


def _simulate_progress(job_id, spotify_jobs, stop_event, start_pct=10, end_pct=75, duration=90):
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
        if spotify_jobs.get(job_id, {}).get('status') == 'downloading':
            spotify_jobs[job_id]['progress'] = min(new_pct, end_pct)


def _build_spotdl_cmd(spotify_url, output_dir, provider='youtube-music'):
    """Construit la commande spotdl selon le provider."""
    cmd = [
        'spotdl', 'download', spotify_url,
        '--output', os.path.join(output_dir, '{artist} - {title}.{ext}'),
        '--format', 'mp3',
        '--bitrate', '192k',
        '--overwrite', 'skip',
        '--audio', provider,
    ]

    # Si des cookies YouTube sont disponibles (fichier monté sur Render via env ou secret)
    cookies_path = os.environ.get('YOUTUBE_COOKIES_PATH', '')
    if cookies_path and os.path.exists(cookies_path):
        cmd += ['--cookie-file', cookies_path]

    return cmd


def _run_spotdl(cmd, output_dir, timeout=300):
    """Lance spotdl et retourne (returncode, output_lines)."""
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=output_dir
    )

    output_lines = []
    for line in process.stdout:
        line = line.rstrip()
        if line:
            output_lines.append(line)

    process.wait(timeout=timeout)
    return process.returncode, output_lines, process


def _find_audio_files(output_dir):
    """Retourne tous les fichiers audio dans le dossier."""
    extensions = ('*.mp3', '*.wav', '*.flac', '*.m4a', '*.ogg', '*.aac', '*.wma')
    audio_files = []
    for pattern in extensions:
        audio_files.extend(glob.glob(os.path.join(output_dir, pattern)))
    return audio_files


def _is_youtube_blocked(output_lines):
    """Détecte si YouTube Music a bloqué le téléchargement."""
    full = '\n'.join(output_lines).lower()
    blocked_signals = [
        'ytdlp download error',
        'yt-dlp download error',
        'http error 403',
        'http error 429',
        'sign in to confirm',
        'video unavailable',
        'blocked',
        'audioProviderError'.lower(),
        'audioprovidererror',
    ]
    return any(sig in full for sig in blocked_signals)


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

        # ── Progression simulée en parallèle ──
        progress_thread = threading.Thread(
            target=_simulate_progress,
            args=(job_id, spotify_jobs, stop_event, 10, 75, 90),
            daemon=True
        )
        progress_thread.start()

        # ── Tentative 1 : YouTube Music (provider par défaut) ──
        spotify_jobs[job_id]['provider_attempt'] = 'youtube-music'
        cmd = _build_spotdl_cmd(spotify_url, output_dir, provider='youtube-music')
        returncode, output_lines, process = _run_spotdl(cmd, output_dir, timeout=300)

        audio_files = _find_audio_files(output_dir)

        # ── Tentative 2 : SoundCloud si YouTube est bloqué ou a échoué sans fichier ──
        if (returncode != 0 or not audio_files) and _is_youtube_blocked(output_lines):
            spotify_jobs[job_id]['provider_attempt'] = 'soundcloud'

            # Nettoyage avant retry
            for f in glob.glob(os.path.join(output_dir, "*")):
                try:
                    os.remove(f)
                except Exception:
                    pass

            cmd2 = _build_spotdl_cmd(spotify_url, output_dir, provider='soundcloud')
            returncode, output_lines, process = _run_spotdl(cmd2, output_dir, timeout=300)
            audio_files = _find_audio_files(output_dir)

        # ── Arrêt de la progression simulée ──
        stop_event.set()

        full_output = "\n".join(output_lines[-30:])

        # ── Vérification du résultat final ──
        if returncode != 0 and not audio_files:
            full_lower = full_output.lower()
            if 'no results' in full_lower or 'not found' in full_lower:
                raise RuntimeError(
                    "Musique introuvable (ni sur YouTube Music, ni sur SoundCloud). "
                    "Essaie un autre lien Spotify."
                )
            if 'ffmpeg' in full_lower:
                raise RuntimeError("Erreur ffmpeg lors de la conversion. Vérifie l'installation.")
            if 'premium' in full_lower:
                raise RuntimeError("Ce contenu nécessite Spotify Premium ou n'est pas disponible.")
            if _is_youtube_blocked(output_lines):
                raise RuntimeError(
                    "YouTube Music et SoundCloud ont tous les deux refusé le téléchargement "
                    "depuis ce serveur. Configure la variable d'environnement "
                    "YOUTUBE_COOKIES_PATH pointant vers un fichier cookies.txt Netscape valide."
                )
            raise RuntimeError(
                f"spotdl a échoué (code {returncode}).\n"
                f"Détails : {full_output[-500:] if full_output else 'Aucune sortie capturée.'}"
            )

        if not audio_files:
            all_files = glob.glob(os.path.join(output_dir, "*"))
            debug = (
                f"Fichiers présents : {[os.path.basename(f) for f in all_files]}\n"
                f"Output spotdl :\n{full_output[-500:]}"
            )
            raise RuntimeError(f"Aucun fichier audio trouvé après téléchargement.\n{debug}")

        spotify_jobs[job_id]['progress'] = 80

        downloaded_file = max(audio_files, key=os.path.getmtime)

        # "Artist - Title" → on inverse en "Title - Artist"
        raw_name = os.path.splitext(os.path.basename(downloaded_file))[0]
        track_name = raw_name

        dash_idx = raw_name.find(' - ')
        if dash_idx != -1:
            artist = raw_name[:dash_idx]
            title  = raw_name[dash_idx + 3:]
            download_name = f"{title} - {artist}.mp3"
        else:
            download_name = f"{raw_name}.mp3"
        download_name = sanitize_filename(download_name)

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
        if process:
            process.kill()
        spotify_jobs[job_id] = {
            'status': 'error',
            'progress': 0,
            'error': 'Le téléchargement a pris trop de temps (limite 300s). Réessaie ou vérifie ta connexion.',
            'file_path': None
        }
    except Exception as e:
        stop_event.set()
        if process and process.poll() is None:
            try:
                process.kill()
            except Exception:
                pass
        spotify_jobs[job_id] = {
            'status': 'error',
            'progress': 0,
            'error': str(e),
            'file_path': None
        }