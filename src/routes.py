"""
Routes Flask pour l'application 8D Audio Studio.
"""
import os
import uuid
import io
import zipfile
import threading
import tempfile

from flask import request, jsonify, send_file, render_template
from pydub.utils import which

from .audio_processor import process_8d, process_batch, cleanup_old_files, UPLOAD_FOLDER
from .spotify_downloader import (
    process_spotify_download,
    save_cookies_from_text,
    clear_cookies,
    has_cookies,
    init_cookies,
)

# Dictionnaires de progression partagés
jobs = {}
batches = {}
spotify_jobs = {}

ALLOWED_EXTENSIONS = {'mp3', 'wav', 'mp4', 'mkv', 'flac', 'm4a', 'aac', 'ogg'}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def init_routes(app):
    """Enregistre toutes les routes sur l'application Flask."""

    @app.route('/')
    def index():
        return render_template('index.html')

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
        return jsonify(job)

    @app.route('/spotify-download-file/<job_id>')
    def spotify_download_file(job_id):
        job = spotify_jobs.get(job_id)
        if not job or job['status'] != 'done':
            return jsonify({'error': 'Fichier non prêt.'}), 404

        file_path = job.get('file_path')
        if not file_path or not os.path.exists(file_path):
            return jsonify({'error': 'Fichier introuvable.'}), 404

        # Utilise le nom "Title - Artist.mp3" calculé lors du téléchargement
        filename = job.get('download_name') or 'spotify_music.mp3'

        return send_file(
            file_path,
            as_attachment=True,
            download_name=filename,
            mimetype="audio/mpeg"
        )

    # ── Routes Cookies YouTube ──
    @app.route('/cookies-status')
    def cookies_status():
        return jsonify({
            'has_cookies': has_cookies()
        })

    @app.route('/cookies-upload', methods=['POST'])
    def cookies_upload():
        if 'cookies' not in request.files:
            return jsonify({'error': 'Aucun fichier cookies fourni.'}), 400

        file = request.files['cookies']
        if file.filename == '':
            return jsonify({'error': 'Nom de fichier vide.'}), 400

        try:
            content = file.read().decode('utf-8')
            if not content.strip():
                return jsonify({'error': 'Fichier cookies vide.'}), 400

            if save_cookies_from_text(content):
                return jsonify({'success': True, 'message': 'Cookies importés avec succès.'})
            else:
                return jsonify({'error': 'Erreur lors de la sauvegarde des cookies.'}), 500
        except Exception as e:
            return jsonify({'error': f'Erreur de lecture du fichier: {str(e)}'}), 400

    @app.route('/cookies-delete', methods=['POST'])
    def cookies_delete():
        if clear_cookies():
            return jsonify({'success': True, 'message': 'Cookies supprimés.'})
        return jsonify({'error': 'Aucun cookie à supprimer.'}), 404

    @app.route('/health')
    def health():
        ffmpeg_ok = which("ffmpeg") is not None
        return jsonify({
            'status': 'ok',
            'ffmpeg_installed': ffmpeg_ok,
            'cookies_configured': has_cookies()
        })
