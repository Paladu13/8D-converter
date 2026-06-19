"""
Spotify Download - Point d'entrée de l'application Flask.
"""
import os

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

app = Flask(__name__)
init_routes(app)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)