import os
import math
import uuid
import struct
import wave
import subprocess
import threading
import tempfile
from flask import Flask, request, jsonify, send_file, render_template

app = Flask(__name__)
jobs = {}

UPLOAD_FOLDER = tempfile.gettempdir()
ALLOWED_EXTENSIONS = {'mp3', 'wav', 'mp4', 'mkv', 'flac', 'm4a', 'aac', 'ogg'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def run_ffmpeg(args):
    """Run an ffmpeg command, return (returncode, stderr)."""
    result = subprocess.run(
        ['ffmpeg', '-y'] + args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    return result.returncode, result.stderr.decode(errors='replace')

def process_8d(job_id, input_path, output_path):
    try:
        jobs[job_id] = {'status': 'loading', 'progress': 0, 'error': None}

        # Step 1 — extract/convert to 16-bit stereo PCM WAV at 44100 Hz
        raw_wav = os.path.join(UPLOAD_FOLDER, f"{job_id}_raw.wav")
        code, err = run_ffmpeg([
            '-i', input_path,
            '-vn',                     # drop video
            '-ac', '2',                # stereo
            '-ar', '44100',            # 44.1 kHz
            '-sample_fmt', 's16',      # 16-bit signed PCM
            raw_wav
        ])
        if code != 0:
            raise RuntimeError(f"ffmpeg decode failed:\n{err}")

        jobs[job_id]['status'] = 'processing'
        jobs[job_id]['progress'] = 5

        # Step 2 — read raw PCM frames
        with wave.open(raw_wav, 'rb') as wf:
            n_channels  = wf.getnchannels()   # should be 2
            sampwidth   = wf.getsampwidth()    # should be 2
            framerate   = wf.getframerate()
            n_frames    = wf.getnframes()
            raw_data    = wf.readframes(n_frames)

        # Convert bytes → list of (L, R) int16 samples
        fmt        = f"<{n_frames * n_channels}h"
        all_samples = list(struct.unpack(fmt, raw_data))
        # Pair into stereo: [(L0,R0), (L1,R1), ...]
        pairs = [(all_samples[i], all_samples[i+1]) for i in range(0, len(all_samples), 2)]

        # Step 3 — apply 8D panning (sine LFO, one full rotation every 10 s)
        period_samples = framerate * 10   # 10-second period
        out_samples    = []
        total          = len(pairs)

        for i, (l, r) in enumerate(pairs):
            # pan_amount in [-1.0, +1.0]
            pan = math.sin((i / period_samples) * 2 * math.pi)

            # Equal-power panning
            angle   = (pan + 1) / 2 * (math.pi / 2)   # 0 … π/2
            gain_l  = math.cos(angle)
            gain_r  = math.sin(angle)

            mono    = (l + r) / 2
            new_l   = int(max(-32768, min(32767, mono * gain_l)))
            new_r   = int(max(-32768, min(32767, mono * gain_r)))
            out_samples.append(new_l)
            out_samples.append(new_r)

            if i % 44100 == 0:   # update every ~1 s
                jobs[job_id]['progress'] = 5 + int((i / total) * 85)

        # Step 4 — write processed PCM back to a temp WAV
        processed_wav = os.path.join(UPLOAD_FOLDER, f"{job_id}_processed.wav")
        out_bytes     = struct.pack(f"<{len(out_samples)}h", *out_samples)
        with wave.open(processed_wav, 'wb') as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(framerate)
            wf.writeframes(out_bytes)

        # Step 5 — encode to MP3 with ffmpeg
        jobs[job_id]['status'] = 'saving'
        jobs[job_id]['progress'] = 92

        code, err = run_ffmpeg([
            '-i', processed_wav,
            '-codec:a', 'libmp3lame',
            '-b:a', '192k',
            output_path
        ])
        if code != 0:
            raise RuntimeError(f"ffmpeg encode failed:\n{err}")

        jobs[job_id]['status'] = 'done'
        jobs[job_id]['progress'] = 100

    except Exception as e:
        jobs[job_id]['status'] = 'error'
        jobs[job_id]['error'] = str(e)

    finally:
        for p in [input_path,
                  os.path.join(UPLOAD_FOLDER, f"{job_id}_raw.wav"),
                  os.path.join(UPLOAD_FOLDER, f"{job_id}_processed.wav")]:
            try:
                os.remove(p)
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

    job_id  = str(uuid.uuid4())
    ext     = file.filename.rsplit('.', 1)[1].lower()
    in_path = os.path.join(UPLOAD_FOLDER, f"{job_id}_input.{ext}")
    out_path= os.path.join(UPLOAD_FOLDER, f"{job_id}_output_8D.mp3")

    file.save(in_path)
    threading.Thread(target=process_8d, args=(job_id, in_path, out_path), daemon=True).start()
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
    out_path = os.path.join(UPLOAD_FOLDER, f"{job_id}_output_8D.mp3")
    if not os.path.exists(out_path):
        return jsonify({'error': 'Fichier introuvable.'}), 404
    return send_file(out_path, as_attachment=True,
                     download_name='audio_8D.mp3', mimetype='audio/mpeg')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)