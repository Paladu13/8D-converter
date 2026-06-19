"""
8D Audio Studio - Point d'entrée de l'application Flask.
"""
import os

# ── Charger les variables d'environnement du fichier .env ──
from dotenv import load_dotenv
load_dotenv()

# ── Polyfill audioop DOIT être avant pydub ──
from src.audioop_polyfill import install_audioop
install_audioop()

# ── Configuration ffmpeg pour pydub ──
from pydub import AudioSegment
from pydub.utils import which

ffmpeg_path = which("ffmpeg")
ffprobe_path = which("ffprobe")
if ffmpeg_path:
    AudioSegment.converter = ffmpeg_path
if ffprobe_path:
    AudioSegment.ffprobe = ffprobe_path

# ── Application Flask ──
from flask import Flask
from src.routes import init_routes
from src.routes import cleanup_expired_spotify_jobs

app = Flask(__name__)
init_routes(app)

# ── Nettoyage périodique des fichiers Spotify expirés ──
import threading
import time as _time

def _periodic_cleanup():
    """Nettoie les fichiers Spotify expirés toutes les 5 minutes."""
    while True:
        _time.sleep(300)  # 5 minutes
        try:
            cleaned = cleanup_expired_spotify_jobs()
            if cleaned:
                print(f"[cleanup] {cleaned} job(s) Spotify expiré(s) nettoyé(s)", flush=True)
        except Exception:
            pass

cleanup_thread = threading.Thread(target=_periodic_cleanup, daemon=True)
cleanup_thread.start()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
