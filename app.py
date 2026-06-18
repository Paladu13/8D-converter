import os
import math
import uuid
import threading
import time
import glob
import struct
import sys
import tempfile

# ──────────────────────────────────────────────────────────────
# Polyfill audioop pour Python 3.13+ (audioop retiré de la stdlib)
# DOIT être AVANT l'import de pydub, car pydub importe audioop
# ──────────────────────────────────────────────────────────────
try:
    import audioop
except ImportError:
    def _unpack(data, width):
        """Helper: unpack audio samples based on width (1=8bit, 2=16bit)."""
        if width == 2:
            count = len(data) // 2
            return struct.unpack(f"<{count}h", data)
        elif width == 1:
            return list(data)  # bytes -> list of ints (already signed for 8-bit)
        return []

    def _pack(samples, width):
        """Helper: pack samples back into bytes based on width."""
        if width == 2:
            samples = [max(-32768, min(32767, s)) for s in samples]
            return struct.pack(f"<{len(samples)}h", *samples)
        elif width == 1:
            samples = [max(-128, min(127, s)) for s in samples]
            return bytes(samples)
        return b""

    class _audioop:
        @staticmethod
        def tostereo(data, width, lfactor, rfactor):
            samples = _unpack(data, width)
            left = [max(-32768, min(32767, int(s * lfactor))) for s in samples]
            right = [max(-32768, min(32767, int(s * rfactor))) for s in samples]
            result = bytearray(len(samples) * 4)
            for i, (l, r) in enumerate(zip(left, right)):
                struct.pack_into("<hh", result, i * 4, l, r)
            return bytes(result)

        @staticmethod
        def max(data, width):
            samples = _unpack(data, width)
            if not samples:
                return 0
            return max(abs(s) for s in samples)

        @staticmethod
        def avg(data, width):
            samples = _unpack(data, width)
            if not samples:
                return 0
            return sum(samples) // len(samples)

        @staticmethod
        def avgpp(data, width):
            return 0

        @staticmethod
        def maxpp(data, width):
            return 0

        @staticmethod
        def cross(data, width):
            samples = _unpack(data, width)
            crosses = 0
            for i in range(1, len(samples)):
                if (samples[i-1] < 0 and samples[i] >= 0) or \
                   (samples[i-1] >= 0 and samples[i] < 0):
                    crosses += 1
            return crosses

        @staticmethod
        def mul(data, width, factor):
            samples = _unpack(data, width)
            samples = [int(s * factor) for s in samples]
            return _pack(samples, width)

        @staticmethod
        def bias(data, width, bias_val):
            samples = _unpack(data, width)
            samples = [s + bias_val for s in samples]
            return _pack(samples, width)

        @staticmethod
        def lin2lin(data, width, newwidth):
            samples = _unpack(data, width)
            if width == 2 and newwidth == 1:
                samples = [s >> 2 for s in samples]
            elif width == 1 and newwidth == 2:
                samples = [s << 2 for s in samples]
            return _pack(samples, newwidth)

        @staticmethod
        def getsample(data, width, index):
            samples = _unpack(data, width)
            if 0 <= index < len(samples):
                return samples[index]
            return 0

        @staticmethod
        def add(data1, data2, width):
            samples1 = _unpack(data1, width)
            samples2 = _unpack(data2, width)
            count = min(len(samples1), len(samples2))
            samples = [samples1[i] + samples2[i] for i in range(count)]
            return _pack(samples, width)

        @staticmethod
        def minmax(data, width):
            samples = _unpack(data, width)
            if not samples:
                return (0, 0)
            return (min(samples), max(samples))

        @staticmethod
        def findfactor(data, reference):
            return 1.0

        @staticmethod
        def findmax(data, length):
            return 0

    audioop = _audioop()
    sys.modules['audioop'] = audioop

# pydub doit être importé APRÈS le polyfill
from flask import Flask, request, jsonify, send_file, render_template
from pydub import AudioSegment
from pydub.utils import which

# --- Configuration de ffmpeg pour pydub ---
ffmpeg_path = which("ffmpeg")
ffprobe_path = which("ffprobe")
if ffmpeg_path:
    AudioSegment.converter = ffmpeg_path
if ffprobe_path:
    AudioSegment.ffprobe = ffprobe_path

app = Flask(__name__)

# Stockage temporaire des progressions par job_id
jobs = {}

UPLOAD_FOLDER = tempfile.gettempdir()
ALLOWED_EXTENSIONS = {'mp3', 'wav', 'mp4', 'mkv', 'flac', 'm4a', 'aac', 'ogg'}

MAX_FILE_AGE = 3600  # 1 heure

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def cleanup_old_files():
    now = time.time()
    for pattern in [os.path.join(UPLOAD_FOLDER, "*_input.*"),
                    os.path.join(UPLOAD_FOLDER, "*_output_8D.mp3")]:
        for f in glob.glob(pattern):
            try:
                if now - os.path.getmtime(f) > MAX_FILE_AGE:
                    os.remove(f)
            except Exception:
                pass

def process_8d(job_id, input_path, output_path):
    try:
        jobs[job_id] = {'status': 'loading', 'progress': 0, 'error': None}

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
    ffmpeg_ok = which("ffmpeg") is not None
    return jsonify({
        'status': 'ok',
        'ffmpeg_installed': ffmpeg_ok
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)