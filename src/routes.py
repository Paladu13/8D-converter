"""
Routes Flask pour l'application 8D Audio Studio.
"""
import os
import uuid
import io
import zipfile
import threading
import tempfile
import time

from flask import request, jsonify, send_file, render_template, abort
from pydub.utils import which

from .audio_processor import process_8d, process_batch, cleanup_old_files, UPLOAD_FOLDER
from .spotify_downloader import (
    process_spotify_download,
)

# Dictionnaires de progression partagés
jobs = {}
batches = {}
spotify_jobs = {}

ALLOWED_EXTENSIONS = {'mp3', 'wav', 'mp4', 'mkv', 'flac', 'm4a', 'aac', 'ogg'}

# Variable de maintenance Spotify (depuis .env)
SPOTIFY_ENABLED = os.environ.get('SPOTIFY_DOWNLOAD_ENABLED', 'on').strip().lower() == 'on'

# Durée de validité des fichiers Spotify : 1 heure
SPOTIFY_FILE_TTL = 3600


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def cleanup_expired_spotify_jobs():
    """Supprime les jobs Spotify expirés et leurs fichiers associés."""
    now = time.time()
    expired_ids = []
    for jid, job in list(spotify_jobs.items()):
        created = job.get('created_at')
        if created and (now - created) > SPOTIFY_FILE_TTL:
            expired_ids.append(jid)
            # Supprimer le fichier zip
            file_path = job.get('file_path')
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception:
                    pass
    for jid in expired_ids:
        del spotify_jobs[jid]
        _log_spotify(f"Nettoyage job expiré : {jid}")
    return len(expired_ids)


def _log_spotify(msg):
    print(f"[spotify-cleanup] {msg}", flush=True)


def init_routes(app):
    """Enregistre toutes les routes sur l'application Flask."""

    @app.route('/')
    def index():
        return render_template('index.html', spotify_enabled=SPOTIFY_ENABLED)

    @app.route('/convert', methods=['POST'])
    def convert():
        if 'file' not in request.files:
            return jsonify({'error': 'Aucun fichier fourni.'}), 400

        file = request.files['file']

        if file.filename == '':
            return jsonify({'error': 'Nom de fichier vide.'}), 400

        if not allowed_file(file.filename):
            return jsonify({'error': 'Format non supporté.'}), 400

        cleanup_old_files()

        job_id = str(uuid.uuid4())
        ext = file.filename.rsplit('.', 1)[1].lower()
        input_path = os.path.join(UPLOAD_FOLDER, f"{job_id}_input.{ext}")
        output_path = os.path.join(UPLOAD_FOLDER, f"{job_id}_output_8D.mp3")

        file.save(input_path)

        thread = threading.Thread(
            target=process_8d, args=(job_id, input_path, output_path, jobs), daemon=True
        )
        thread.start()

        return jsonify({'job_id': job_id})

    @app.route('/progress/<job_id>')
    def progress(job_id):
        job = jobs.get(job_id)
        if not job:
            return jsonify({'error': 'Job introuvable.'}), 404
        return jsonify(job)

    @app.route('/download/<job_id>')
    def download(job_id):
        job = jobs.get(job_id)
        if not job or job['status'] != 'done':
            return jsonify({'error': 'Fichier non prêt.'}), 404

        output_path = os.path.join(UPLOAD_FOLDER, f"{job_id}_output_8D.mp3")

        if not os.path.exists(output_path):
            return jsonify({'error': 'Fichier introuvable.'}), 404

        return send_file(
            output_path,
            as_attachment=True,
            download_name="audio_8D.mp3",
            mimetype="audio/mpeg"
        )

    @app.route('/convert-batch', methods=['POST'])
    def convert_batch():
        if 'files' not in request.files:
            return jsonify({'error': 'Aucun fichier fourni.'}), 400

        files = request.files.getlist('files')
        if not files or len(files) == 0:
            return jsonify({'error': 'Aucun fichier sélectionné.'}), 400

        cleanup_old_files()

        batch_id = str(uuid.uuid4())
        files_info = []

        for f in files:
            if f.filename == '' or not allowed_file(f.filename):
                continue

            ext = f.filename.rsplit('.', 1)[1].lower()
            input_path = os.path.join(UPLOAD_FOLDER, f"{batch_id}_{uuid.uuid4().hex}_input.{ext}")
            f.save(input_path)
            files_info.append((f.filename, input_path))

        if not files_info:
            return jsonify({'error': 'Aucun fichier valide fourni.'}), 400

        batches[batch_id] = {
            'status': 'uploading',
            'current_file': 0,
            'total_files': len(files_info),
            'current_file_name': '',
            'job_id': None,
            'progress': 0,
            'error': None
        }

        thread = threading.Thread(
            target=process_batch, args=(batch_id, files_info, batches, jobs), daemon=True
        )
        thread.start()

        return jsonify({'batch_id': batch_id, 'total_files': len(files_info)})

    @app.route('/batch-progress/<batch_id>')
    def batch_progress(batch_id):
        batch = batches.get(batch_id)
        if not batch:
            return jsonify({'error': 'Batch introuvable.'}), 404

        response = dict(batch)
        job_id = batch.get('job_id')
        if job_id and job_id in jobs:
            job = jobs[job_id]
            response['progress'] = job.get('progress', 0)
            response['job_status'] = job.get('status', '')

        return jsonify(response)

    @app.route('/download-batch/<batch_id>')
    def download_batch(batch_id):
        outputs_key = batch_id + '_outputs'
        output_paths = batches.get(outputs_key)

        if not output_paths:
            batch = batches.get(batch_id)
            if batch and batch.get('status') == 'done':
                return jsonify({'error': 'Fichiers de sortie introuvables.'}), 404
            return jsonify({'error': 'Batch pas encore terminé.'}), 404

        # ── Si un seul fichier, on l'envoie directement sans zip ──
        if len(output_paths) == 1:
            orig_filename, output_path = output_paths[0]
            if not os.path.exists(output_path):
                return jsonify({'error': 'Fichier de sortie introuvable.'}), 404
            download_name = f"{os.path.splitext(orig_filename)[0]}_8D.mp3"
            return send_file(
                output_path,
                as_attachment=True,
                download_name=download_name,
                mimetype="audio/mpeg"
            )

        # ── Plusieurs fichiers → zip ──
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            for orig_filename, output_path in output_paths:
                if os.path.exists(output_path):
                    zip_name = f"{os.path.splitext(orig_filename)[0]}_8D.mp3"
                    zf.write(output_path, zip_name)

        zip_buffer.seek(0)

        return send_file(
            zip_buffer,
            as_attachment=True,
            download_name="conversions_8D.zip",
            mimetype="application/zip"
        )

    # ── Routes Spotify ──
    @app.route('/spotify-download', methods=['POST'])
    def spotify_download():
        data = request.get_json()
        if not data or 'url' not in data:
            return jsonify({'error': 'URL Spotify requise.'}), 400

        url = data['url'].strip()

        if not url or ('spotify.com' not in url and 'open.spotify.com' not in url):
            return jsonify({'error': 'URL Spotify invalide.'}), 400

        job_id = str(uuid.uuid4())
        thread = threading.Thread(
            target=process_spotify_download, args=(job_id, url, spotify_jobs), daemon=True
        )
        thread.start()

        return jsonify({'job_id': job_id})

    @app.route('/spotify-progress/<job_id>')
    def spotify_progress(job_id):
        job = spotify_jobs.get(job_id)
        if not job:
            return jsonify({'error': 'Job introuvable.'}), 404

        # Ajouter le temps restant avant expiration
        resp = dict(job)
        created = job.get('created_at')
        if created:
            remaining = max(0, int(SPOTIFY_FILE_TTL - (time.time() - created)))
            resp['expires_in'] = remaining
        else:
            resp['expires_in'] = 0

        return jsonify(resp)

    @app.route('/spotify-download-file/<job_id>')
    def spotify_download_file(job_id):
        # Nettoyer les jobs expirés avant de vérifier
        cleanup_expired_spotify_jobs()

        job = spotify_jobs.get(job_id)
        if not job or job['status'] != 'done':
            return jsonify({'error': 'Fichier non prêt ou expiré.'}), 404

        file_path = job.get('file_path')
        if not file_path or not os.path.exists(file_path):
            return jsonify({'error': 'Fichier introuvable ou expiré (plus d\'1h).'}), 404

        # Vérifier l'expiration
        created = job.get('created_at')
        if created and (time.time() - created) > SPOTIFY_FILE_TTL:
            # Nettoyer et signaler
            try:
                os.remove(file_path)
            except Exception:
                pass
            del spotify_jobs[job_id]
            return jsonify({'error': 'Fichier expiré (plus d\'1h). Veuillez relancer le téléchargement.'}), 410

        # Utilise le nom "Title - Artist.mp3" calculé lors du téléchargement
        filename = job.get('download_name') or 'spotify_music.mp3'

        return send_file(
            file_path,
            as_attachment=True,
            download_name=filename,
            mimetype="audio/mpeg"
        )

    @app.route('/health')
    def health():
        ffmpeg_ok = which("ffmpeg") is not None
        return jsonify({
            'status': 'ok',
            'ffmpeg_installed': ffmpeg_ok,
        })
