"""
Module de téléchargement Spotify (Playlist + Morceaux individuels).

Utilise l'API Spotify officielle (spotipy - OAuth) pour récupérer les pistes,
puis yt-dlp pour télécharger depuis YouTube Music.

Compatible : playlists ET tracks uniques.
Avec 3 tentatives par piste, progression en temps réel via hooks.
"""
import os
import re
import threading
import sys
import time
import io
import zipfile
import glob
import traceback

import spotipy
from spotipy.oauth2 import SpotifyOAuth
import yt_dlp

from .audio_processor import UPLOAD_FOLDER

# Configuration Spotify (depuis .env ou valeurs par défaut)
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = os.environ.get("SPOTIFY_REDIRECT_URI")


def _log(job_id, msg):
    print(f"[spotify:{job_id}] {msg}", flush=True, file=sys.stdout)


def sanitize_filename(name):
    """Nettoie un nom de fichier pour qu'il soit valide sur tous les OS."""
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    name = re.sub(r'\s+', ' ', name).strip()
    name = name[:200]
    return name


# Chemin du fichier de cache OAuth (au format .json, compatible spotipy)
CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "cache.json")


def _create_spotify_client():
    """Crée et retourne un client Spotify authentifié via OAuth.
    
    Utilise cache.json généré par setup_spotify.py pour éviter
    de devoir se réauthentifier à chaque démarrage de l'application.
    Le format est identique à l'ancien .cache, juste renommé pour plus de clarté.
    """
    auth_manager = SpotifyOAuth(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
        redirect_uri=SPOTIFY_REDIRECT_URI,
        scope="playlist-read-private playlist-read-collaborative",
        cache_path=CACHE_PATH,
        open_browser=False,
    )
    return spotipy.Spotify(auth_manager=auth_manager)


# Cache de session : stocke les job_ids par "session_token" (adresse IP + user-agent hash)
# Permet de nettoyer les fichiers quand l'utilisateur quitte ou rafraîchit la page.
SESSION_CACHE = {}
SESSION_CACHE_LOCK = threading.Lock()


def _fetch_spotify_tracks(url):
    """
    Récupère les titres depuis un lien Spotify (playlist ou track unique).
    
    Supporte :
      - https://open.spotify.com/playlist/XXXX
      - https://open.spotify.com/track/XXXX
    
    Retourne (liste_de_pistes ["Artiste - Titre", ...], erreur_ou_None).
    """
    try:
        sp = _create_spotify_client()
        tracks = []

        # --- Détection du type de lien ---
        playlist_match = re.search(r"playlist/([A-Za-z0-9]+)", url)
        track_match = re.search(r"track/([A-Za-z0-9]+)", url)
        album_match = re.search(r"album/([A-Za-z0-9]+)", url)

        if not playlist_match and not track_match and not album_match:
            return None, "Lien Spotify invalide. Utilisez un lien de playlist, d'album ou de morceau."

        if playlist_match:
            # ── MODE PLAYLIST ──
            playlist_id = playlist_match.group(1)
            _log(None, f"Mode playlist, ID : {playlist_id}")
            offset = 0

            while True:
                results = sp.playlist_items(playlist_id, offset=offset)
                if not results:
                    break

                items = results.get('items', [])
                if not items:
                    break

                _log(None, f"Traitement de {len(items)} pistes (offset {offset})...")

                for item in items:
                    if not item:
                        continue
                    track_obj = item.get('track') or item.get('item') or item
                    track_name = None
                    artist_name = "Artiste Inconnu"

                    if isinstance(track_obj, dict):
                        track_name = track_obj.get('name')
                        if not track_name and 'track' in track_obj and isinstance(track_obj['track'], dict):
                            track_name = track_obj['track'].get('name')

                        artists = track_obj.get('artists', [])
                        if not artists and 'track' in track_obj and isinstance(track_obj['track'], dict):
                            artists = track_obj['track'].get('artists', [])

                        if artists and isinstance(artists, list) and len(artists) > 0:
                            artist_name = artists[0].get('name', 'Artiste Inconnu')
                        elif track_obj.get('show'):
                            artist_name = track_obj.get('show', {}).get('name', 'Podcast')

                    if not track_name or str(track_name).strip() == "":
                        track_id = track_obj.get('id') or track_obj.get('uri') if isinstance(track_obj, dict) else None
                        track_name = f"Spotify Track {track_id}" if track_id else f"Morceau #{len(tracks) + 1}"

                    tracks.append(f"{track_name} - {artist_name}")

                if results.get('next'):
                    offset += len(items)
                else:
                    break

        elif album_match:
            # ── MODE ALBUM ──
            album_id = album_match.group(1)
            _log(None, f"Mode album, ID : {album_id}")

            results = sp.album_tracks(album_id)
            items = results.get('items', [])
            _log(None, f"Traitement de {len(items)} pistes d'album...")

            for track_item in items:
                if not track_item:
                    continue
                track_name = track_item.get('name', 'Morceau inconnu')
                track_artists = track_item.get('artists', [])
                artist_name = track_artists[0].get('name', 'Artiste Inconnu') if track_artists else 'Artiste Inconnu'
                tracks.append(f"{track_name} - {artist_name}")

        else:
            # ── MODE MORCEAU UNIQUE ──
            track_id = track_match.group(1)
            _log(None, f"Mode morceau unique, ID : {track_id}")

            track_data = sp.track(track_id)
            track_name = track_data.get('name', 'Morceau inconnu')
            artists = track_data.get('artists', [])
            artist_name = artists[0].get('name', 'Artiste Inconnu') if artists else 'Artiste Inconnu'

            tracks.append(f"{track_name} - {artist_name}")

        if not tracks:
            return None, "Aucune piste trouvée."

        # ── Déduplication des doublons ──
        seen = set()
        unique_tracks = []
        for t in tracks:
            if t not in seen:
                seen.add(t)
                unique_tracks.append(t)
        tracks = unique_tracks

        _log(None, f"Total : {len(tracks)} piste(s) chargée(s) (dont {len(seen)} uniques)")
        return tracks, None

    except Exception as e:
        traceback.print_exc()
        return None, f"Erreur API Spotify : {e}"


def _make_progress_hook(track_name, job_id, spotify_jobs):
    """Crée un hook yt-dlp qui met à jour la progression du job en temps réel."""

    def hook(d):
        if d.get('status') == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate')
            downloaded = d.get('downloaded_bytes', 0)
            pct = (downloaded / total * 100) if total else 0

            if job_id in spotify_jobs and spotify_jobs[job_id].get('status') == 'downloading':
                spotify_jobs[job_id]['current_track'] = track_name
                spotify_jobs[job_id]['track_progress'] = f"{pct:.1f}%"
                spotify_jobs[job_id]['track_downloading'] = True

        elif d.get('status') == 'finished':
            if job_id in spotify_jobs and spotify_jobs[job_id].get('status') == 'downloading':
                spotify_jobs[job_id]['current_track'] = f"Conversion MP3 : {track_name}"
                spotify_jobs[job_id]['track_progress'] = "conversion..."
                spotify_jobs[job_id]['track_downloading'] = True

    return hook


def _download_single_track(track_name, output_dir, job_id=None, spotify_jobs=None, timeout=180):
    """
    Recherche et télécharge un morceau depuis YouTube Music au format MP3.
    Effectue jusqu'à 3 tentatives.
    Retourne True/False comme le standalone (qui fait confiance à yt-dlp).
    """
    safe_name = sanitize_filename(track_name)
    expected_mp3 = os.path.join(output_dir, f"{safe_name}.mp3")

    # ── Vérifier si déjà téléchargé ──
    if os.path.exists(expected_mp3):
        _log(job_id, f"Déjà présent, ignoré : {track_name}")
        return True

    # Construire les hooks de progression
    progress_hooks = []
    if job_id and spotify_jobs:
        progress_hooks = [_make_progress_hook(track_name, job_id, spotify_jobs)]

    # ── Cookies YouTube (optionnel, pour Render/datacenter) ──
    # Les cookies aident à contourner les blocages YouTube sur les IPs de datacenter.
    # Générer avec : cat cookies.txt | base64 | pbcopy  (macOS)
    #              : cat cookies.txt | base64 | clip    (Windows)
    youtube_cookies_b64 = os.environ.get("YOUTUBE_COOKIES", "")
    cookies_file = None
    if youtube_cookies_b64:
        try:
            import base64, tempfile
            cookies_data = base64.b64decode(youtube_cookies_b64)
            cookies_file = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8')
            cookies_file.write(cookies_data.decode('utf-8'))
            cookies_file.close()
            _log(job_id, f"Cookies YouTube chargés depuis YOUTUBE_COOKIES")
        except Exception as e:
            _log(job_id, f"Erreur chargement cookies YouTube: {e}")

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': os.path.join(output_dir, f'{safe_name}.%(ext)s'),
        'default_search': 'ytsearch',
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'noprogress': True,
        'progress_hooks': progress_hooks,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'extractor_args': {
            'youtube': {
                'player_client': ['web', 'android'],
            }
        },
    }
    if cookies_file:
        ydl_opts['cookiefile'] = cookies_file.name

    for attempt in range(1, 4):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                _log(job_id, f"Recherche YouTube pour : {track_name}")
                info = ydl.extract_info(f"ytsearch1:{track_name}", download=True)
                if info and 'entries' in info and len(info['entries']) > 0:
                    entry_title = info['entries'][0].get('title', '?')
                    _log(job_id, f"✓ {track_name} → trouvé: {entry_title}")
                    return True
                else:
                    _log(job_id, f"Aucun résultat YouTube pour: {track_name}")
        except Exception as e:
            _log(job_id, f"Essai {attempt}/3 pour {track_name} : {type(e).__name__}: {e}")
            traceback.print_exc()
            if attempt < 3:
                time.sleep(2)

    # Nettoyer le fichier temporaire des cookies
    if cookies_file:
        try:
            os.unlink(cookies_file.name)
        except Exception:
            pass

    return False


def process_spotify_download(job_id, spotify_url, spotify_jobs):
    """
    Point d'entrée principal - traite un lien Spotify (playlist ou track unique).
    
    Même logique que le standalone : 
      - Télécharge séquentiellement 1 par 1
      - Utilise glob pour trouver les fichiers MP3 créés (comme le standalone)
    """
    try:
        _log(job_id, f"Démarrage : {spotify_url}")

        spotify_jobs[job_id] = {
            'status': 'downloading',
            'progress': 0,
            'error': None,
            'file_path': None,
            'tracks': [],
            'downloaded': 0,
            'failed': 0,
            'total': 0,
            'current_track': '',
            'track_progress': '',
            'track_downloading': False,
            'eta': 0,
        }

        output_dir = os.path.join(UPLOAD_FOLDER, f"spotify_{job_id}")
        os.makedirs(output_dir, exist_ok=True)

        # ── Étape 1 : Récupérer la liste des pistes ──
        spotify_jobs[job_id]['progress'] = 5
        spotify_jobs[job_id]['current_track'] = 'Récupération des pistes Spotify...'

        tracks, error = _fetch_spotify_tracks(spotify_url)

        if error or not tracks:
            raise RuntimeError(error or "Impossible de récupérer les pistes.")

        total = len(tracks)
        spotify_jobs[job_id]['tracks'] = tracks
        spotify_jobs[job_id]['total'] = total
        spotify_jobs[job_id]['progress'] = 10

        _log(job_id, f"{total} piste(s) trouvée(s)")

        # ── Étape 2 : Téléchargement séquentiel 1 par 1 ──
        success_count = 0
        fail_count = 0
        start_time = time.time()

        for i in range(total):
            track = tracks[i]

            try:
                # Mettre à jour le statut dans l'UI
                spotify_jobs[job_id]['current_track'] = f"[{i+1}/{total}] {track}"
                spotify_jobs[job_id]['track_progress'] = 'téléchargement...'
                spotify_jobs[job_id]['track_downloading'] = True

                # Télécharger ce track (comme le standalone : retourne True/False)
                result = _download_single_track(track, output_dir, job_id, spotify_jobs)

                if result:
                    success_count += 1
                    spotify_jobs[job_id]['track_progress'] = '✓'
                else:
                    fail_count += 1
                    spotify_jobs[job_id]['track_progress'] = '✗'

            except Exception as e:
                _log(job_id, f"Erreur sur {track} : {e}")
                fail_count += 1

            # Progression globale : 10% → 90% pour les téléchargements
            pct = 10 + int((i + 1) / total * 80)
            spotify_jobs[job_id]['progress'] = min(pct, 90)
            spotify_jobs[job_id]['downloaded'] = success_count
            spotify_jobs[job_id]['failed'] = fail_count
            spotify_jobs[job_id]['track_downloading'] = False

            # Temps estimé restant
            elapsed = time.time() - start_time
            avg = elapsed / (i + 1)
            remaining = int((total - i - 1) * avg)
            spotify_jobs[job_id]['eta'] = remaining

        _log(job_id, f"Téléchargement terminé : {success_count}/{total} réussis, {fail_count} échecs")
        spotify_jobs[job_id]['downloaded'] = success_count
        spotify_jobs[job_id]['failed'] = fail_count
        spotify_jobs[job_id]['progress'] = 95

        if success_count == 0:
            raise RuntimeError(
                f"Aucune piste n'a pu être téléchargée sur {total}.\n"
                "Vérifie que ffmpeg est installé et que YouTube est accessible."
            )

        # Attendre 1s que tous les postprocessors ffmpeg finissent
        time.sleep(1)

        # ── Étape 3 : Créer un ZIP avec tous les fichiers téléchargés ──
        # Trouver tous les MP3 dans le dossier (comme le standalone)
        all_mp3 = sorted(glob.glob(os.path.join(output_dir, "*.mp3")), key=os.path.getmtime)

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            for i, track in enumerate(tracks):
                safe_name = sanitize_filename(track)
                mp3_path = os.path.join(output_dir, f"{safe_name}.mp3")
                # Chercher le fichier correspondant
                found = None
                for mp3 in all_mp3:
                    if safe_name in mp3 or os.path.basename(mp3).startswith(safe_name):
                        found = mp3
                        break
                if found and os.path.exists(found):
                    # Nom propre dans le zip : "001 - Artiste - Titre.mp3"
                    zip_name = f"{i+1:03d} - {track}.mp3"
                    zf.write(found, sanitize_filename(zip_name))

        zip_buffer.seek(0)

        # Sauvegarder le zip
        zip_path = os.path.join(UPLOAD_FOLDER, f"spotify_{job_id}_playlist.zip")
        with open(zip_path, 'wb') as f:
            f.write(zip_buffer.getvalue())

        # Nettoyer les fichiers individuels
        for f in glob.glob(os.path.join(output_dir, "*")):
            try:
                os.remove(f)
            except Exception:
                pass
        try:
            os.rmdir(output_dir)
        except Exception:
            pass

        spotify_jobs[job_id].update({
            'status': 'done',
            'progress': 100,
            'file_path': zip_path,
            'download_name': 'spotify_playlist.zip',
            'success_count': success_count,
            'failed': fail_count,
            'total_count': total,
            'track_name': f"{success_count}/{total} pistes téléchargées | {fail_count} échecs",
        })

    except Exception as e:
        traceback.print_exc()
        spotify_jobs[job_id] = {
            'status': 'error',
            'progress': 0,
            'error': str(e),
            'file_path': None,
        }