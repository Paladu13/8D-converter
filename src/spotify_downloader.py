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
    Retourne le chemin du fichier MP3 ou None.
    """
    safe_name = sanitize_filename(track_name)
    expected_mp3 = os.path.join(output_dir, f"{safe_name}.mp3")
    output_template = os.path.join(output_dir, f"{safe_name}.%(ext)s")

    # ── Vérifier si déjà téléchargé ──
    if os.path.exists(expected_mp3):
        _log(job_id, f"Déjà présent, ignoré : {track_name}")
        return expected_mp3

    # Construire les hooks de progression
    progress_hooks = []
    if job_id and spotify_jobs:
        progress_hooks = [_make_progress_hook(track_name, job_id, spotify_jobs)]

    # Extraire juste le nom du morceau (sans artiste) pour la recherche YouTube
    search_query = track_name.replace(' - ', ' ').strip()

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': output_template,
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
                'player_client': ['android', 'tv', 'web'],
            }
        },
    }

    for attempt in range(1, 4):  # 3 tentatives max
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"ytsearch1:{search_query}", download=True)
                if 'entries' in info and len(info['entries']) > 0:
                    # Vérifier le fichier par son chemin exact
                    if os.path.exists(expected_mp3):
                        return expected_mp3
                    # Fallback: attendre 1s que ffmpeg finisse la conversion
                    time.sleep(1)
                    if os.path.exists(expected_mp3):
                        return expected_mp3
                    # Dernier recours: chercher le .mp3 le plus récent
                    all_mp3 = sorted(glob.glob(os.path.join(output_dir, "*.mp3")), key=os.path.getmtime)
                    for mp3 in reversed(all_mp3):
                        if abs(time.time() - os.path.getmtime(mp3)) < 30:
                            return mp3
                    return None
        except Exception as e:
            _log(job_id, f"Essai {attempt}/3 pour {track_name} : {e}")
            if attempt < 3:
                time.sleep(2 ** attempt)

    return None


def _preload_metadata(track_name, output_dir, prefetch_cache):
    """
    Pré-charge les métadonnées YouTube pour un morceau.
    Stocke le résultat dans prefetch_cache pour accélérer le download réel.
    """
    safe_name = sanitize_filename(track_name)
    expected_mp3 = os.path.join(output_dir, f"{safe_name}.mp3")

    # Déjà téléchargé ? skip
    if os.path.exists(expected_mp3):
        prefetch_cache[track_name] = ('cached', expected_mp3)
        return

    search_query = track_name.replace(' - ', ' ').strip()
    ydl_opts = {
        'format': 'bestaudio/best',
        'default_search': 'ytsearch',
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'noprogress': True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch1:{search_query}", download=False)
            if 'entries' in info and len(info['entries']) > 0:
                entry = info['entries'][0]
                web_url = entry.get('webpage_url', '') or entry.get('url', '')
                prefetch_cache[track_name] = ('ready', web_url)
            else:
                prefetch_cache[track_name] = ('failed', None)
    except Exception:
        prefetch_cache[track_name] = ('failed', None)


def process_spotify_download(job_id, spotify_url, spotify_jobs):
    """
    Point d'entrée principal - traite un lien Spotify (playlist ou track unique).
    
    Nouveau comportement :
      - Télécharge UNE par UNE (séquentiel), pas 5 en parallèle
      - Pré-charge les 5 suivantes en arrière-plan (extraction métadonnées YouTube)
      - Évite les conflits de fichiers parallèles
      - Progression % = nombre total / traité
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

        # ── Étape 2 : Téléchargement séquentiel 1 par 1 avec pré-chargement des 5 suivantes ──
        from concurrent.futures import ThreadPoolExecutor, as_completed

        success_count = 0
        fail_count = 0
        start_time = time.time()
        results = {}  # idx -> file_path
        prefetch_cache = {}  # track_name -> (status, url_or_path)

        # Lancer le pré-chargement des métadonnées pour les 5 premières
        lookahead = 5
        prefetch_executor = ThreadPoolExecutor(max_workers=lookahead)
        prefetch_futures = {}

        def _start_prefetch(start_idx):
            nonlocal prefetch_futures
            # Nettoyer les futures terminées
            prefetch_futures = {f: t for f, t in prefetch_futures.items() if not f.done()}
            # Lancer le pré-chargement pour les prochains
            for idx in range(start_idx, min(start_idx + lookahead, total)):
                track = tracks[idx]
                if track not in prefetch_cache:
                    f = prefetch_executor.submit(_preload_metadata, track, output_dir, prefetch_cache)
                    prefetch_futures[f] = track

        # Démarrer le pré-chargement initial
        _start_prefetch(0)

        # Boucle séquentielle : 1 téléchargement à la fois
        for i in range(total):
            track = tracks[i]
            safe_name = sanitize_filename(track)
            expected_mp3 = os.path.join(output_dir, f"{safe_name}.mp3")

            try:
                # Attendre que le pré-chargement de CE track soit fini
                for f, t in list(prefetch_futures.items()):
                    if t == track:
                        f.result(timeout=30)
                        break

                # Mettre à jour le statut dans l'UI
                spotify_jobs[job_id]['current_track'] = f"[{i+1}/{total}] {track}"
                spotify_jobs[job_id]['track_progress'] = 'téléchargement...'
                spotify_jobs[job_id]['track_downloading'] = True

                # Télécharger ce track
                file_path = _download_single_track(track, output_dir, job_id, spotify_jobs)
                results[i] = file_path

                if file_path:
                    success_count += 1
                    spotify_jobs[job_id]['track_progress'] = '✓'
                else:
                    fail_count += 1
                    spotify_jobs[job_id]['track_progress'] = '✗'

                # Lancer le pré-chargement pour les suivants
                _start_prefetch(i + 1)

            except Exception as e:
                _log(job_id, f"Erreur sur {track} : {e}")
                results[i] = None
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

        prefetch_executor.shutdown(wait=False)

        _log(job_id, f"Téléchargement terminé : {success_count}/{total} réussis, {fail_count} échecs")
        spotify_jobs[job_id]['downloaded'] = success_count
        spotify_jobs[job_id]['failed'] = fail_count
        spotify_jobs[job_id]['progress'] = 95

        if success_count == 0:
            raise RuntimeError(
                f"Aucune piste n'a pu être téléchargée sur {total}.\n"
                "Vérifie que ffmpeg est installé et que YouTube est accessible."
            )

        # ── Étape 3 : Créer un ZIP avec tous les fichiers téléchargés ──
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            # Utiliser results dict indexé par idx pour garder l'ordre
            for i, track in enumerate(tracks):
                file_path = results.get(i)
                if file_path and os.path.exists(file_path):
                    # Nom propre dans le zip : "001 - Artiste - Titre.mp3"
                    zip_name = f"{i+1:03d} - {track}.mp3"
                    zf.write(file_path, sanitize_filename(zip_name))

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