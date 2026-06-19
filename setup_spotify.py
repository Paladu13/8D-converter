"""
Script de configuration Spotify OAuth.

Ce script génère le fichier .cache nécessaire à l'authentification Spotify.
Il ouvre un navigateur pour que tu autorises l'application, puis sauvegarde
le token d'accès dans le fichier .cache à la racine du projet.

Utilisation :
    python setup_spotify.py

Prérequis :
    - Les credentials Spotify doivent être dans le fichier .env
      (SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REDIRECT_URI)
    - pip install spotipy python-dotenv
"""
import os
import sys

# Charger le .env
from dotenv import load_dotenv
load_dotenv()

# Configuration Spotify
CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "005c10d472294a3e98d7fea8fbf52fe0")
CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "75929397792d4f1a9311bbc4a7f48b98")
REDIRECT_URI = os.environ.get("SPOTIFY_REDIRECT_URI", "https://spotidown.co/en6")

CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache.json")


def main():
    print("=" * 60)
    print("  Configuration Spotify OAuth")
    print("=" * 60)
    print()
    print(f"  Client ID     : {CLIENT_ID}")
    print(f"  Redirect URI  : {REDIRECT_URI}")
    print(f"  Cache path    : {CACHE_PATH}")
    print()

    # Vérifier si un cache existe déjà
    if os.path.exists(CACHE_PATH):
        print("  [!] Un fichier cache.json existe déjà.")
        reponse = input("  Voulez-vous le régénérer ? (o/N) : ").strip().lower()
        if reponse != 'o':
            print("  [i] Annulé. Le cache existant est conservé.")
            return

    print()
    print("  [i] Ouverture du navigateur pour autoriser l'application...")
    print("  [i] Connecte-toi à ton compte Spotify et autorise l'accès.")
    print()

    try:
        from spotipy.oauth2 import SpotifyOAuth

        auth_manager = SpotifyOAuth(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            redirect_uri=REDIRECT_URI,
            scope="playlist-read-private playlist-read-collaborative",
            cache_path=CACHE_PATH,
            open_browser=True,
        )

        # Tenter de récupérer un token (déclenche le flux OAuth)
        token_info = auth_manager.get_access_token(as_dict=False)

        if token_info:
            print()
            print("  [✓] Authentification réussie !")
            print(f"  [✓] Cache sauvegardé dans : {CACHE_PATH}")
            print()
            print("  Tu peux maintenant lancer l'application Flask :")
            print("      python app.py")
            print()
        else:
            print()
            print("  [x] Échec de l'authentification. Aucun token reçu.")
            sys.exit(1)

    except Exception as e:
        print()
        print(f"  [x] Erreur lors de l'authentification : {e}")
        print()
        print("  Vérifie que :")
        print("    - Les credentials dans .env sont corrects")
        print("    - Le REDIRECT_URI correspond à celui configuré")
        print("      dans le Spotify Developer Dashboard")
        print("    - Tu as une connexion internet")
        sys.exit(1)


if __name__ == "__main__":
    main()