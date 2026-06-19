// ────────────────────────────────────────────────────────────
// SPOTIFY DOWNLOAD
// ────────────────────────────────────────────────────────────
const spotifyUrl        = document.getElementById('spotifyUrl');
const spotifyBtn        = document.getElementById('spotifyBtn');
const spotifyProgressSec = document.getElementById('spotifyProgressSection');
const spotifyProgressLbl = document.getElementById('spotifyProgressLabel');
const spotifyProgressPct = document.getElementById('spotifyProgressPct');
const spotifyProgressFil = document.getElementById('spotifyProgressFill');
const spotifyProgressSub = document.getElementById('spotifyProgressSub');
const spotifyStatusMsg   = document.getElementById('spotifyStatusMsg');
const spotifyStatusIcon  = document.getElementById('spotifyStatusIcon');
const spotifyStatusText  = document.getElementById('spotifyStatusText');
const spotifyDownloadBtn = document.getElementById('spotifyDownloadBtn');
const spotifyResult      = document.getElementById('spotifyResult');
const spotifyResultName  = document.getElementById('spotifyResultName');
const spotifyResultArtist = document.getElementById('spotifyResultArtist');

let spotifyPollInterval = null;

// Activer/désactiver le bouton selon l'URL
spotifyUrl.addEventListener('input', () => {
  const val = spotifyUrl.value.trim();
  spotifyBtn.disabled = !val || !val.includes('spotify.com');
});

function resetSpotifyUI() {
  spotifyProgressSec.classList.remove('visible');
  spotifyStatusMsg.classList.remove('visible', 'success', 'error', 'info');
  spotifyDownloadBtn.classList.remove('visible');
  spotifyProgressFil.style.width = '0%';
  spotifyProgressSub.textContent = '';
  spotifyResult.classList.remove('visible');
  if (spotifyPollInterval) { clearInterval(spotifyPollInterval); spotifyPollInterval = null; }
}

function showSpotifyStatus(type, iconPath, text) {
  spotifyStatusMsg.className = `status-msg visible ${type}`;
  spotifyStatusIcon.innerHTML = iconPath;
  spotifyStatusText.textContent = text;
}

// ── Téléchargement Spotify ──
spotifyBtn.addEventListener('click', () => {
  const url = spotifyUrl.value.trim();
  if (!url) return;

  spotifyBtn.disabled = true;
  spotifyDownloadBtn.classList.remove('visible');
  spotifyResult.classList.remove('visible');
  spotifyStatusMsg.classList.remove('visible', 'success', 'error', 'info');
  spotifyProgressSec.classList.add('visible');
  spotifyProgressFil.style.width = '0%';
  spotifyProgressPct.textContent = '0%';
  spotifyProgressLbl.textContent = 'Téléchargement…';
  spotifyProgressSub.textContent = 'Recherche de la musique…';

  fetch('/spotify-download', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url })
  })
  .then(res => res.json())
  .then(data => {
    if (data.error) {
      showSpotifyStatus('error', '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>', data.error);
      spotifyBtn.disabled = false;
      return;
    }

    const jobId = data.job_id;
    spotifyProgressLbl.textContent = 'Téléchargement en cours…';
    spotifyProgressSub.textContent = 'Patiente quelques secondes';

    // ── Polling progression Spotify ──
    spotifyPollInterval = setInterval(async () => {
      try {
        const res  = await fetch(`/spotify-progress/${jobId}`);
        const job  = await res.json();

        const pct = job.progress || 0;
        spotifyProgressFil.style.width = pct + '%';
        spotifyProgressPct.textContent = pct + '%';

        if (job.status === 'downloading') {
          if (pct < 15) {
            spotifyProgressLbl.textContent = 'Vérification des dépendances…';
            spotifyProgressSub.textContent = 'Recherche de la musique sur YouTube Music';
          } else if (pct < 50) {
            spotifyProgressLbl.textContent = 'Téléchargement en cours…';
            spotifyProgressSub.textContent = 'Récupération depuis YouTube Music';
          } else {
            spotifyProgressLbl.textContent = 'Conversion en MP3…';
            spotifyProgressSub.textContent = 'Encodage audio 192k';
          }
        }

        if (job.status === 'done') {
          clearInterval(spotifyPollInterval);
          spotifyProgressLbl.textContent = 'Terminé !';
          spotifyProgressSub.textContent = 'Téléchargement réussi';
          spotifyResult.classList.add('visible');
          // Affiche le vrai nom de la piste si disponible
          // track_name est au format "Artist - Title" (venant du template spotdl)
          const trackName = job.track_name || 'Musique Spotify';
          const dashIdx = trackName.indexOf(' - ');
          if (dashIdx !== -1) {
            const artist = trackName.substring(0, dashIdx);
            const title  = trackName.substring(dashIdx + 3);
            spotifyResultName.textContent   = title;
            spotifyResultArtist.textContent = artist;
          } else {
            spotifyResultName.textContent   = trackName;
            spotifyResultArtist.textContent = '';
          }
          showSpotifyStatus('success', '<polyline stroke-linecap="round" stroke-linejoin="round" stroke-width="2" points="20 6 9 17 4 12"/>', 'Musique téléchargée avec succès !');
          spotifyDownloadBtn.href = `/spotify-download-file/${jobId}`;
          spotifyDownloadBtn.classList.add('visible');
          spotifyBtn.disabled = false;
        }

        if (job.status === 'error') {
          clearInterval(spotifyPollInterval);
          showSpotifyStatus('error', '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>', 'Erreur : ' + (job.error || 'inconnue'));
          spotifyBtn.disabled = false;
        }

      } catch (e) {
        clearInterval(spotifyPollInterval);
        showSpotifyStatus('error', '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>', 'Erreur de connexion au serveur.');
        spotifyBtn.disabled = false;
      }
    }, 800);
  })
  .catch(err => {
    showSpotifyStatus('error', '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>', 'Erreur réseau : ' + err.message);
    spotifyBtn.disabled = false;
  });
});