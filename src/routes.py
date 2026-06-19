"""
Routes Flask pour l'application Spotify Download.
"""
import os
import uuid
import threading
import tempfile

from flask import request, jsonify, send_file, render_template
from pydub.utils import which

from .spotify_downloader import process_spotify_download

# Dictionnaires de progression partagés
spotify_jobs = {}


def init_routes(app):
    """Enregistre toutes les routes sur l'application Flask."""

    @app.route('/')
    def index():
        return render_template('index.html')

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

    @app.route('/health')
    def health():
        ffmpeg_ok = which("ffmpeg") is not None
        return jsonify({
            'status': 'ok',
            'ffmpeg_installed': ffmpeg_ok
        })