"""
Module de traitement audio 8D.
Contient les fonctions de conversion 8D et de batch.
"""
import math
import os
import uuid
import tempfile
import time
import glob
import threading

from pydub import AudioSegment
from pydub.utils import which

UPLOAD_FOLDER = tempfile.gettempdir()
MAX_FILE_AGE = 3600  # 1 heure


def cleanup_old_files():
    """Nettoie les fichiers temporaires de plus d'une heure."""
    now = time.time()
    for pattern in [os.path.join(UPLOAD_FOLDER, "*_input.*"),
                    os.path.join(UPLOAD_FOLDER, "*_output_8D.*"),
                    os.path.join(UPLOAD_FOLDER, "spotify_*")]:
        for f in glob.glob(pattern):
            try:
                if now - os.path.getmtime(f) > MAX_FILE_AGE:
                    os.remove(f)
            except Exception:
                pass


def process_8d(job_id, input_path, output_path, jobs):
    """Convertit un fichier audio en 8D avec panning sinusoidal."""
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

        output_path_with_ext = output_path
        if not output_path.endswith('.mp3'):
            output_path_with_ext = output_path + '.mp3'

        panned_audio.export(output_path_with_ext, format="mp3", bitrate="192k")

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


def process_batch(batch_id, files_info, batches, jobs):
    """Traite les fichiers un par un à la chaîne."""
    try:
        total = len(files_info)
        output_paths = []

        for idx, (orig_filename, input_path) in enumerate(files_info):
            job_id = str(uuid.uuid4())
            output_filename = f"{os.path.splitext(orig_filename)[0]}_8D.mp3"
            output_path = os.path.join(UPLOAD_FOLDER, f"{batch_id}_{idx}_output_8D.mp3")

            batches[batch_id] = {
                'status': 'processing',
                'current_file': idx + 1,
                'total_files': total,
                'current_file_name': orig_filename,
                'job_id': job_id,
                'progress': 0,
                'error': None
            }

            try:
                if not which("ffmpeg"):
                    raise RuntimeError("ffmpeg n'est pas installé.")

                jobs[job_id] = {'status': 'loading', 'progress': 0, 'error': None}
                batches[batch_id]['job_id'] = job_id

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
                        pct = int(((i + 1) / total_chunks) * 100)
                        jobs[job_id]['progress'] = pct
                        batches[batch_id]['progress'] = pct

                jobs[job_id]['status'] = 'saving'
                jobs[job_id]['progress'] = 99
                batches[batch_id]['progress'] = 99

                panned_audio.export(output_path, format="mp3", bitrate="192k")

                jobs[job_id]['status'] = 'done'
                jobs[job_id]['progress'] = 100
                output_paths.append((orig_filename, output_path))

            except Exception as e:
                jobs[job_id] = {'status': 'error', 'progress': 0, 'error': str(e)}
                batches[batch_id] = {
                    'status': 'error',
                    'error': f"Erreur sur {orig_filename} : {str(e)}",
                    'current_file': idx + 1,
                    'total_files': total,
                    'current_file_name': orig_filename,
                    'job_id': job_id,
                    'progress': 0
                }
                return
            finally:
                try:
                    os.remove(input_path)
                except Exception:
                    pass

        batches[batch_id] = {
            'status': 'done',
            'current_file': total,
            'total_files': total,
            'current_file_name': files_info[-1][0] if files_info else '',
            'job_id': None,
            'progress': 100,
            'error': None,
            'output_count': len(output_paths)
        }

        batches[batch_id + '_outputs'] = output_paths

    except Exception as e:
        batches[batch_id] = {
            'status': 'error',
            'error': str(e),
            'current_file': 0,
            'total_files': len(files_info),
            'current_file_name': '',
            'job_id': None,
            'progress': 0
        }