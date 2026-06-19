// ────────────────────────────────────────────────────────────
// TAB SYSTEM
// ────────────────────────────────────────────────────────────
const tabBtns = document.querySelectorAll('.tab-btn');
const tabContents = document.querySelectorAll('.tab-content');

tabBtns.forEach(btn => {
  btn.addEventListener('click', () => {
    const tabId = btn.dataset.tab;
    tabBtns.forEach(b => { b.classList.remove('active'); b.setAttribute('aria-selected', 'false'); });
    tabContents.forEach(c => c.classList.remove('active'));
    btn.classList.add('active');
    btn.setAttribute('aria-selected', 'true');
    document.getElementById(tabId).classList.add('active');
  });
});

// ────────────────────────────────────────────────────────────
// TAB 1 : CONVERSION 8D (copie de l'existant)
// ────────────────────────────────────────────────────────────
const dropzone    = document.getElementById('dropzone');
const fileInput   = document.getElementById('fileInput');
const fileList    = document.getElementById('fileList');
const fileCount   = document.getElementById('fileCount');
const convertBtn  = document.getElementById('convertBtn');
const progressSec = document.getElementById('progressSection');
const progressLbl = document.getElementById('progressLabel');
const progressPct = document.getElementById('progressPct');
const progressFil = document.getElementById('progressFill');
const progressSub = document.getElementById('progressSub');
const statusMsg   = document.getElementById('statusMsg');
const statusIcon  = document.getElementById('statusIcon');
const statusText  = document.getElementById('statusText');
const downloadBtn = document.getElementById('downloadBtn');

let selectedFiles = [];
let pollInterval = null;

function formatSize(bytes) {
  if (bytes < 1024) return bytes + ' o';
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' Ko';
  return (bytes / 1048576).toFixed(1) + ' Mo';
}

// ── Drag & drop ──
dropzone.addEventListener('dragover', e => { e.preventDefault(); dropzone.classList.add('dragging'); });
dropzone.addEventListener('dragleave', () => dropzone.classList.remove('dragging'));
dropzone.addEventListener('drop', e => {
  e.preventDefault();
  dropzone.classList.remove('dragging');
  const files = Array.from(e.dataTransfer.files).filter(f => {
    const ext = '.' + f.name.split('.').pop().toLowerCase();
    return '.mp3.wav.mp4.mkv.flac.m4a.aac.ogg'.includes(ext);
  });
  if (files.length) addFiles(files);
});

fileInput.addEventListener('change', () => {
  if (fileInput.files.length) {
    addFiles(Array.from(fileInput.files));
    fileInput.value = '';
  }
});

function addFiles(files) {
  selectedFiles = selectedFiles.concat(files);
  renderFileList();
  convertBtn.disabled = selectedFiles.length === 0;
  resetConvertUI();
}

function removeFile(index) {
  selectedFiles.splice(index, 1);
  renderFileList();
  convertBtn.disabled = selectedFiles.length === 0;
  resetConvertUI();
}

function renderFileList() {
  fileList.innerHTML = '';
  selectedFiles.forEach((f, i) => {
    const item = document.createElement('div');
    item.className = 'file-item';
    item.innerHTML = `
      <div class="fi-icon-small">
        <svg viewBox="0 0 24 24"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>
      </div>
      <span class="fi-name">${f.name}</span>
      <span class="fi-size">${formatSize(f.size)}</span>
      <button class="fi-remove" data-index="${i}" title="Retirer">✕</button>
    `;
    item.querySelector('.fi-remove').addEventListener('click', () => removeFile(i));
    fileList.appendChild(item);
  });
  fileList.classList.toggle('visible', selectedFiles.length > 0);
  fileCount.textContent = selectedFiles.length > 0 ? `${selectedFiles.length} fichier${selectedFiles.length > 1 ? 's' : ''} sélectionné${selectedFiles.length > 1 ? 's' : ''}` : '';
}

function resetConvertUI() {
  progressSec.classList.remove('visible');
  statusMsg.classList.remove('visible', 'success', 'error', 'info');
  downloadBtn.classList.remove('visible');
  progressFil.style.width = '0%';
  progressSub.textContent = '';
  if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
}

function showConvertStatus(type, iconPath, text) {
  statusMsg.className = `status-msg visible ${type}`;
  statusIcon.innerHTML = iconPath;
  statusText.textContent = text;
}

// ── Conversion batch ──
convertBtn.addEventListener('click', () => {
  if (selectedFiles.length === 0) return;

  convertBtn.disabled = true;
  downloadBtn.classList.remove('visible');
  statusMsg.classList.remove('visible', 'success', 'error', 'info');
  progressSec.classList.add('visible');
  progressFil.style.width = '0%';
  progressPct.textContent = '0%';
  progressLbl.textContent = 'Envoi des fichiers… 0%';
  progressSub.textContent = `${selectedFiles.length} fichier${selectedFiles.length > 1 ? 's' : ''} à traiter`;

  const formData = new FormData();
  selectedFiles.forEach(f => formData.append('files', f));

  const xhr = new XMLHttpRequest();

  // ── Suivi progression upload ──
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) {
      const pct = Math.round((e.loaded / e.total) * 100);
      progressFil.style.width = pct + '%';
      progressPct.textContent = pct + '%';
      progressLbl.textContent = 'Envoi des fichiers… ' + pct + '%';
    }
  };

  // ── Upload terminé → polling batch ──
  xhr.onload = () => {
    if (xhr.status !== 200) {
      let msg = 'Erreur lors de l\'envoi';
      try {
        const d = JSON.parse(xhr.responseText);
        if (d.error) msg = d.error;
      } catch (_) {}
      showConvertStatus('error', '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>', msg);
      convertBtn.disabled = false;
      return;
    }

    let batchId, totalFiles;
    try {
      const data = JSON.parse(xhr.responseText);
      batchId = data.batch_id;
      totalFiles = data.total_files;
    } catch (_) {
      showConvertStatus('error', '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>', 'Réponse invalide du serveur.');
      convertBtn.disabled = false;
      return;
    }

    progressFil.style.width = '100%';
    progressPct.textContent = '100%';
    progressLbl.textContent = 'Fichiers envoyés ! Conversion en cours…';

    // ── Polling progression batch ──
    pollInterval = setInterval(async () => {
      try {
        const res  = await fetch(`/batch-progress/${batchId}`);
        const data = await res.json();

        const pct = data.progress || 0;
        progressFil.style.width = pct + '%';
        progressPct.textContent = pct + '%';

        if (data.status === 'processing') {
          progressLbl.textContent = `Conversion ${data.current_file}/${data.total_files} : ${data.current_file_name || ''}`;
          progressSub.textContent = `Fichier ${data.current_file} sur ${data.total_files}`;
        } else if (data.status === 'uploading') {
          progressLbl.textContent = 'Préparation des fichiers…';
        }

        if (data.status === 'done') {
          clearInterval(pollInterval);
          progressLbl.textContent = 'Terminé !';
          progressSub.textContent = `${data.output_count || totalFiles} fichier${(data.output_count || totalFiles) > 1 ? 's' : ''} converti${(data.output_count || totalFiles) > 1 ? 's' : ''}`;
          showConvertStatus('success', '<polyline stroke-linecap="round" stroke-linejoin="round" stroke-width="2" points="20 6 9 17 4 12"/>', 'Toutes les conversions sont terminées !');
          downloadBtn.href = `/download-batch/${batchId}`;
          downloadBtn.classList.add('visible');
          convertBtn.disabled = false;
        }

        if (data.status === 'error') {
          clearInterval(pollInterval);
          showConvertStatus('error', '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>', 'Erreur : ' + (data.error || 'inconnue'));
          convertBtn.disabled = false;
        }

      } catch (e) {
        clearInterval(pollInterval);
        showConvertStatus('error', '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>', 'Impossible de joindre le serveur.');
        convertBtn.disabled = false;
      }
    }, 800);
  };

  // ── Erreur réseau ──
  xhr.onerror = () => {
    showConvertStatus('error', '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>', 'Erreur réseau. Vérifie ta connexion.');
    convertBtn.disabled = false;
  };

  xhr.open('POST', '/convert-batch');
  xhr.send(formData);
});

// ────────────────────────────────────────────────────────────
// TAB 2 : PLAYLIST SPOTIFY
// ────────────────────────────────────────────────────────────
const spotifyUrl         = document.getElementById('spotifyUrl');
const spotifyBtn         = document.getElementById('spotifyBtn');
const spotifyProgressSec = document.getElementById('spotifyProgressSection');
const spotifyProgressLbl = document.getElementById('spotifyProgressLabel');
const spotifyProgressPct = document.getElementById('spotifyProgressPct');
const spotifyProgressFil = document.getElementById('spotifyProgressFill');
const spotifyProgressSub = document.getElementById('spotifyProgressSub');
const spotifyStatusMsg   = document.getElementById('spotifyStatusMsg');
const spotifyStatusIcon  = document.getElementById('spotifyStatusIcon');
const spotifyStatusText  = document.getElementById('spotifyStatusText');
const spotifyDownloadBtn = document.getElementById('spotifyDownloadBtn');
const playlistInfo       = document.getElementById('playlistInfo');
const playlistTrackCount = document.getElementById('playlistTrackCount');
const playlistTracks     = document.getElementById('playlistTracks');

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
  playlistInfo.style.display = 'none';
  if (spotifyPollInterval) { clearInterval(spotifyPollInterval); spotifyPollInterval = null; }
}

function showSpotifyStatus(type, iconPath, text) {
  spotifyStatusMsg.className = `status-msg visible ${type}`;
  spotifyStatusIcon.innerHTML = iconPath;
  spotifyStatusText.textContent = text;
}

// ── Téléchargement Playlist Spotify ──
spotifyBtn.addEventListener('click', () => {
  const url = spotifyUrl.value.trim();
  if (!url) return;

  spotifyBtn.disabled = true;
  spotifyDownloadBtn.classList.remove('visible');
  spotifyStatusMsg.classList.remove('visible', 'success', 'error', 'info');
  spotifyProgressSec.classList.add('visible');
  spotifyProgressFil.style.width = '0%';
  spotifyProgressPct.textContent = '0%';
  spotifyProgressLbl.textContent = 'Analyse de la playlist…';
  spotifyProgressSub.textContent = 'Récupération des pistes Spotify';

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

    // ── Polling progression Spotify ──
    spotifyPollInterval = setInterval(async () => {
      try {
        const res  = await fetch(`/spotify-progress/${jobId}`);
        const job  = await res.json();

        const pct = job.progress || 0;
        spotifyProgressFil.style.width = pct + '%';
        spotifyProgressPct.textContent = pct + '%';

        if (job.status === 'downloading') {
          if (job.current_track) {
            spotifyProgressLbl.textContent = `Téléchargement : ${job.current_track}`;
            spotifyProgressSub.textContent = `${job.downloaded || 0} / ${job.total || '?'} pistes réussies`;
          } else {
            spotifyProgressLbl.textContent = 'Téléchargement en cours…';
            spotifyProgressSub.textContent = 'Patiente quelques instants';
          }
        }

        if (job.status === 'done') {
          clearInterval(spotifyPollInterval);
          const total = job.total_count || job.total || 0;
          const success = job.success_count || job.downloaded || 0;
          spotifyProgressLbl.textContent = 'Terminé !';
          spotifyProgressSub.textContent = `${success}/${total} pistes téléchargées`;
          
          showSpotifyStatus('success', '<polyline stroke-linecap="round" stroke-linejoin="round" stroke-width="2" points="20 6 9 17 4 12"/>', 
            `Playlist téléchargée ! ${success}/${total} pistes récupérées`);
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

// ── Nettoyage automatique au refresh/quit de la page ──
window.addEventListener('beforeunload', function() {
  // Envoie une requête synchrone (keepalive) pour nettoyer les fichiers Spotify
  navigator.sendBeacon('/spotify-cleanup', JSON.stringify({}));
});
