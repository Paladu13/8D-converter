import os
import math
import uuid
import threading
import time
import glob
from flask import Flask, request, jsonify, send_file, render_template
from pydub import AudioSegment
from pydub.utils import which
import tempfile

# --- Configuration de ffmpeg pour pydub ---
# Sur Render, ffmpeg est installé dans /usr/bin/ffmpeg via le buildCommand du render.yaml
ffmpeg_path = which("ffmpeg")
ffprobe_path = which("ffprobe")
if ffmpeg_path:
    AudioSegment.converter = ffmpeg_path
if ffprobe_path:
    AudioSegment.ffprobe = ffprobe_path

app = Flask(__name__)

# Stockage temporaire des progressions par job_id
jobs = {}

UPLOAD_FOLDER = tempfile.gettempdir()  # Sur Render = /tmp
ALLOWED_EXTENSIONS = {'mp3', 'wav', 'mp4', 'mkv', 'flac', 'm4a', 'aac', 'ogg'}

# Durée de vie max des fichiers temporaires (en secondes)
MAX_FILE_AGE = 3600  # 1 heure

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def cleanup_old_files():
    """Nettoie les fichiers temporaires de plus de MAX_FILE_AGE secondes."""
    now = time.time()
    for f in glob.glob(os.path.join(UPLOAD_FOLDER, "*_input.*")) + \
             glob.glob(os.path.join(UPLOAD_FOLDER, "*_output_8D.mp3")):
        try:
            if now - os.path.getmtime(f) > MAX_FILE_AGE:
                os.remove(f)
        except Exception:
            pass

def process_8d(job_id, input_path, output_path):
    try:
        jobs[job_id] = {'status': 'loading', 'progress': 0, 'error': None}

        # Vérifier que ffmpeg est disponible
        if not which("ffmpeg"):
            raise RuntimeError(
                "ffmpeg n'est pas installé sur le serveur. "
                "Contactez l'administrateur."
            )

        audio = AudioSegment.from_file(input_path)

        jobs[job_id]['status'] = 'processing'

        chunk_length_ms = 100
        chunks = [audio[i:i + chunk_length_ms] for i in range(0, len(audio), chunk_length_ms)]

        panned_audio = AudioSegment.empty()
        period_ms = 10000
        total_chunks = len(chunks)

        for i, chunk in enumerate(chunks):
            time_ms = i * chunk_length_ms
            pan_amount = math.sin((time_ms / period_ms) * 2 * math.pi)
            panned_audio += chunk.pan(pan_amount)

            if i % 10 == 0:
                jobs[job_id]['progress'] = int(((i + 1) / total_chunks) * 100)

        jobs[job_id]['status'] = 'saving'
        jobs[job_id]['progress'] = 99

        panned_audio.export(output_path, format="mp3", bitrate="192k")

        jobs[job_id]['status'] = 'done'
        jobs[job_id]['progress'] = 100

    except Exception as e:
        jobs[job_id]['status'] = 'error'
        jobs[job_id]['error'] = str(e)
    finally:
        try:
            os.remove(input_path)
        except Exception:
            pass

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

    # Nettoyage périodique des vieux fichiers
    cleanup_old_files()

    job_id = str(uuid.uuid4())
    ext = file.filename.rsplit('.', 1)[1].lower()
    input_path = os.path.join(UPLOAD_FOLDER, f"{job_id}_input.{ext}")
    output_path = os.path.join(UPLOAD_FOLDER, f"{job_id}_output_8D.mp3")

    file.save(input_path)

    thread = threading.Thread(target=process_8d, args=(job_id, input_path, output_path), daemon=True)
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

@app.route('/health')
def health():
    """Endpoint de health check pour Render."""
    ffmpeg_ok = which("ffmpeg") is not None
    return jsonify({
        'status': 'ok',
        'ffmpeg_installed': ffmpeg_ok
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)