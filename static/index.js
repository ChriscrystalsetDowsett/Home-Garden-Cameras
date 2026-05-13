// ── State ──────────────────────────────────────────────────────────────────────
let   tlRunning      = false;
let   tlPollTimer    = null;
let   tlClockTimer   = null;   // 1-second tick for elapsed/remaining display
let   tlStart_       = null;
let   tlInterval_    = 0;      // active interval in seconds
let   tlDuration_    = 0;      // active duration in seconds (0 = unlimited)
let   tlNextFrameAt_ = null;   // estimated epoch ms of next frame
let   _tlPollMs      = 1000;   // adaptive: 1 s (fast interval) → 30 s (slow interval)
let   statsTimer   = null;
let   _tickTimer   = null;
let   _lastTickTime = 0;
let   streamOnline = false;
let   currentRes   = '1920x1080';
let   recording    = false;
let   recordTimer  = null;
let   recordStart  = null;
let   tlPanelOpen   = false;
let   recPanelOpen  = false;
let   snapPanelOpen = false;
let   ctrlPanelOpen   = false;
let   filterPanelOpen = false;
let   resDDOpen    = false;
let   recCrf       = 23;
let   recAudio     = true;
let   streamAudio  = false;   // live audio playback while watching the feed

// ── Web Audio state (live streaming) ─────────────────────────────────────────
const _AUDIO_SR       = 16000;   // sample rate of /api/audio/stream/raw
const _AUDIO_AHEAD    = 0.15;    // seconds to schedule ahead (150 ms — absorbs PulseAudio burst jitter)
let   _audioCtx       = null;
let   _audioReader    = null;
let   _audioNext      = 0;
let   _audioLeftover  = null;
let   recTargetRes = '1920x1080';
let   snapFilter   = 'none';
let   snapQuality  = 95;
let   camEnabled   = true;

// Timelapse presets
const TL_INTERVAL_S    = [1, 5, 10, 30, 60, 300, 900, 3600];
const TL_INTERVAL_LBLS = ['1s','5s','10s','30s','1m','5m','15m','1h'];
const TL_DUR_S         = [300, 900, 1800, 3600, 7200, 14400, 28800, 86400, 0];
const TL_DUR_LBLS      = ['5m','15m','30m','1h','2h','4h','8h','24h','\u221e'];

// ── Init ───────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  loadInfo();
  loadCtrlDefaults();
  startStats();
  loadGallery();
  loadVideos();
  fetchCameraEnabled();

  // Restore timelapse state so any device sees the same running session
  try {
    const r = await fetch('/api/timelapse/status');
    const d = await r.json();
    if (d.running) {
      tlRunning      = true;
      tlInterval_    = d.interval;
      tlDuration_    = d.duration;
      tlStart_       = Date.now() - (d.elapsed * 1000);
      tlNextFrameAt_ = tlStart_ + d.count * d.interval * 1000;

      // Restore slider positions to match the running session
      const iIdx = TL_INTERVAL_S.findIndex(v => v === d.interval);
      if (iIdx >= 0) setInterval_(iIdx);
      const dIdx = TL_DUR_S.findIndex(v => v === d.duration);
      if (dIdx >= 0) setDur(dIdx);

      // Open the panel immediately showing live status
      tlPanelOpen = true;
      document.getElementById('tl-panel').classList.remove('panel-closed');
      document.getElementById('tl-interval-slider').disabled = true;
      document.getElementById('tl-dur-slider').disabled = true;
      document.getElementById('tl-btn').className = 'btn btn-tl active';
      document.getElementById('frame-count').textContent = String(d.count).padStart(4, '0');
      if (d.duration > 0) {
        const projFrames = Math.floor(d.duration / d.interval);
        document.getElementById('tl-videst').textContent = '~' + fmtVideoLen(projFrames / 24);
      } else {
        const fph = Math.floor(3600 / d.interval);
        document.getElementById('tl-videst').textContent = '~' + fmtVideoLen(fph / 24) + '/hr';
      }
      const startBtn = document.getElementById('tl-start-btn');
      startBtn.className = 'btn btn-danger';
      startBtn.style.cssText = 'width:100%;justify-content:center';
      startBtn.innerHTML = '<svg width="10" height="10" viewBox="0 0 16 16" fill="currentColor"><rect x="3" y="3" width="10" height="10" rx="1"/></svg> Stop Timelapse';
      _showTLRunning(d.duration > 0);
      _tlPollMs    = Math.max(1000, Math.min(30000, d.interval * 250));
      tlPollTimer  = setInterval(pollTL, _tlPollMs);
      tlClockTimer = setInterval(_tlClockTick, 1000);
      document.getElementById('tl-live-strip').style.display = '';
      _updateTLStrip(d.elapsed || 0);
    } else {
      _showTLIdle();
    }
  } catch (_) { _showTLIdle(); }

  // Restore recording state
  try {
    const r = await fetch('/api/record/status');
    const d = await r.json();
    if (d.running) {
      recording = true;
      recordStart = Date.now() - (d.duration * 1000);
      const btn = document.getElementById('record-btn');
      btn.className = 'btn btn-rec recording';
      recordTimer = setInterval(() => {
        const s  = Math.floor((Date.now() - recordStart) / 1000);
        const mm = String(Math.floor(s / 60)).padStart(2, '0');
        const ss = String(s % 60).padStart(2, '0');
        btn.innerHTML = '<svg width="10" height="10" viewBox="0 0 16 16" fill="currentColor"><rect x="3" y="3" width="10" height="10" rx="1"/></svg> ' + mm + ':' + ss;
      }, 500);
    }
  } catch (_) {}
});

// ── Camera enable / disable ────────────────────────────────────────────────────

async function fetchCameraEnabled() {
  try {
    const r = await fetch('/api/camera/enabled');
    const d = await r.json();
    camEnabled = d.enabled !== false;
    _updateCamToggleBtn();
  } catch (_) {}
}

async function toggleCamera() {
  const btn = document.getElementById('cam-toggle-btn');
  btn.disabled = true;
  try {
    const r = await fetch('/api/camera/enabled', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: !camEnabled }),
    });
    const d = await r.json();
    camEnabled = d.enabled !== false;
    _updateCamToggleBtn();
    showToast(camEnabled ? '📷 Camera enabled' : '⏸ Camera paused');
  } catch (_) {
    showToast('Failed to toggle camera');
  } finally {
    btn.disabled = false;
  }
}

function _updateCamToggleBtn() {
  const btn = document.getElementById('cam-toggle-btn');
  const lbl = document.getElementById('cam-toggle-label');
  btn.classList.toggle('off', !camEnabled);
  lbl.textContent = camEnabled ? 'Camera On' : 'Camera Off';
}

// ── Fullscreen ─────────────────────────────────────────────────────────────────

const _el = document.getElementById('feed-wrap');
// iOS Safari does not support the Fullscreen API on arbitrary elements.
const _supportsFS = !!(
  _el.requestFullscreen || _el.webkitRequestFullscreen
);
let _iosFakeFS = false;

function toggleFullscreen() {
  if (_supportsFS) {
    // Standard path — Chrome, Firefox, desktop Safari
    const inFS = !!(document.fullscreenElement || document.webkitFullscreenElement);
    if (!inFS) {
      (_el.requestFullscreen || _el.webkitRequestFullscreen).call(_el).catch(() => {});
    } else {
      (document.exitFullscreen || document.webkitExitFullscreen).call(document);
    }
  } else {
    // iOS fallback — pin the feed over the viewport with CSS
    _iosFakeFS = !_iosFakeFS;
    document.body.classList.toggle('ios-fs', _iosFakeFS);
    _applyFSState(_iosFakeFS);
  }
}

function _applyFSState(inFS) {
  document.getElementById('fs-expand').style.display   = inFS ? 'none' : '';
  document.getElementById('fs-collapse').style.display = inFS ? ''     : 'none';
  document.getElementById('fs-btn').classList.toggle('active', inFS);
}

// ── Fullscreen quick-action buttons ───────────────────────────────────────────
function fsqRecord() {
  if (recording) { stopRecording(); } else { startRecording(); }
}

function updateFSQuickBtns() {
  const recBtn = document.getElementById('fsq-rec-btn');
  const tlBtn  = document.getElementById('fsq-tl-btn');
  if (!recBtn || !tlBtn) return;
  if (recording) {
    recBtn.classList.add('active');
    recBtn.innerHTML = '<svg width="32" height="32" viewBox="0 0 24 24" fill="currentColor" stroke="none"><rect x="3" y="3" width="18" height="18" rx="3"/></svg>';
    recBtn.title = 'Stop Recording';
  } else {
    recBtn.classList.remove('active');
    recBtn.innerHTML = '<svg width="36" height="36" viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M2 8a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V8z"/><path d="M16 10.5 22 7v10l-6-3.5V10.5z"/></svg>';
    recBtn.title = 'Record';
  }
  if (tlRunning) {
    tlBtn.classList.add('active');
    tlBtn.innerHTML = '<svg width="32" height="32" viewBox="0 0 24 24" fill="currentColor" stroke="none"><rect x="3" y="3" width="18" height="18" rx="3"/></svg>';
    tlBtn.title = 'Stop Timelapse';
  } else {
    tlBtn.classList.remove('active');
    tlBtn.innerHTML = '<svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15.5 15.5"/></svg>';
    tlBtn.title = 'Timelapse';
  }
}

document.addEventListener('fullscreenchange',       () => _applyFSState(!!(document.fullscreenElement || document.webkitFullscreenElement)));
document.addEventListener('webkitfullscreenchange', () => _applyFSState(!!(document.fullscreenElement || document.webkitFullscreenElement)));

// ── Swipe-to-pan/tilt (fullscreen only) ───────────────────────────────────────
//
// A single-finger drag on the feed translates to a servo velocity command:
//   horizontal displacement → pan  (-1 = full left,  +1 = full right)
//   vertical displacement   → tilt (-1 = full down,  +1 = full up)
//
// Drag distance from the touch origin controls speed.  A dead zone of
// SERVO_DEAD_PX at the centre prevents accidental movement on taps.
// Commands are throttled to one per SERVO_THROTTLE_MS.

const SERVO_DEAD_PX    = 12;    // px — ignore sub-threshold movement
const SERVO_MAX_PX     = 90;    // px — reach full speed at this displacement
const SERVO_THROTTLE_MS = 50;   // ms — max 20 commands/sec
const SERVO_RING_R      = 42;   // px — joystick ring radius for visual clamp

let _st   = null;   // active touch: { id, x0, y0 } or null
let _stTs = 0;      // timestamp of last command sent

function _inFS() {
  return !!(document.fullscreenElement || document.webkitFullscreenElement || _iosFakeFS);
}

function _servoNorm(delta) {
  const sign = delta < 0 ? -1 : 1;
  const abs  = Math.abs(delta);
  if (abs < SERVO_DEAD_PX) return 0;
  return sign * Math.min(1.0, (abs - SERVO_DEAD_PX) / (SERVO_MAX_PX - SERVO_DEAD_PX));
}

function _servoSend(pan, tilt) {
  fetch('/api/servo/move', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({pan, tilt}),
  }).catch(() => {});
}

function _servoStop() {
  _st = null;
  _sjHide();
  fetch('/api/servo/stop', {method: 'POST'}).catch(() => {});
}

// ── Joystick visual ────────────────────────────────────────────────────────────
function _sjShow(x0, y0, cx, cy) {
  const wrap = document.getElementById('feed-wrap');
  const rect = wrap.getBoundingClientRect();
  const ox = x0 - rect.left;
  const oy = y0 - rect.top;
  const ring = document.getElementById('servo-ring');
  const dot  = document.getElementById('servo-dot');
  document.getElementById('servo-joystick').classList.add('active');
  ring.style.left = ox + 'px';
  ring.style.top  = oy + 'px';
  // Clamp dot to ring radius
  const dx   = cx - x0;
  const dy   = cy - y0;
  const dist = Math.sqrt(dx * dx + dy * dy);
  const r    = Math.min(dist, SERVO_RING_R);
  const ang  = Math.atan2(dy, dx);
  dot.style.left = (ox + Math.cos(ang) * r) + 'px';
  dot.style.top  = (oy + Math.sin(ang) * r) + 'px';
}

function _sjHide() {
  document.getElementById('servo-joystick').classList.remove('active');
}

// ── Touch listeners ────────────────────────────────────────────────────────────
const _feedEl = document.getElementById('feed-wrap');

_feedEl.addEventListener('touchstart', e => {
  if (!_inFS()) return;
  // Ignore touches that land on buttons or the top overlay
  if (e.target.closest('button, .feed-overlay, #res-options')) return;
  if (_st) return;
  const t = e.changedTouches[0];
  _st = {id: t.identifier, x0: t.clientX, y0: t.clientY};
}, {passive: true});

_feedEl.addEventListener('touchmove', e => {
  if (!_st) return;
  const t = Array.from(e.changedTouches).find(c => c.identifier === _st.id);
  if (!t) return;
  e.preventDefault();   // block scroll/zoom during active servo drag
  _sjShow(_st.x0, _st.y0, t.clientX, t.clientY);
  const now = Date.now();
  if (now - _stTs < SERVO_THROTTLE_MS) return;
  _stTs = now;
  const pan  =  _servoNorm(t.clientX - _st.x0);
  const tilt = -_servoNorm(t.clientY - _st.y0);  // Y axis inverted: up = positive tilt
  _servoSend(pan, tilt);
}, {passive: false});

_feedEl.addEventListener('touchend', e => {
  if (!_st) return;
  if (Array.from(e.changedTouches).some(c => c.identifier === _st.id)) _servoStop();
}, {passive: true});

_feedEl.addEventListener('touchcancel', () => { if (_st) _servoStop(); }, {passive: true});

// ── Camera info ────────────────────────────────────────────────────────────────
const MODEL_NAMES = {
  'OV5647': 'SainSmart OV5647 Wide Angle',
  'IMX219': 'Raspberry Pi Camera v2',
  'IMX477': 'Raspberry Pi HQ Camera',
  'IMX708': 'Raspberry Pi Camera v3',
};

// Stored once from /api/info for use in stats chips
let piModel = '—';
let camModel = '—';

function shortPiModel(m) {
  // "Raspberry Pi 4 Model B Rev 1.4" → "Pi 4B"
  const match = m.match(/Pi\s+(\d+)\s+Model\s+([A-Z]+)/i);
  if (match) return 'Pi ' + match[1] + match[2];
  const z = m.match(/Pi\s+Zero\s*(\w*)/i);
  if (z) return 'Pi Zero' + (z[1] ? ' ' + z[1] : '');
  return m.replace('Raspberry ', '').split(' Rev')[0].trim();
}

async function loadInfo() {
  try {
    const r = await fetch('/api/info');
    const d = await r.json();
    const model = (d.camera || '').toUpperCase();
    camModel = MODEL_NAMES[model] || d.camera || '?';
    document.getElementById('camera-model').textContent = camModel;
    setResDisplay(d.resolution || '1920x1080');
    if (d.model) {
      piModel = shortPiModel(d.model);
      document.getElementById('ic-pi').textContent = piModel;
    }
    document.getElementById('ic-cam').textContent = d.camera || '?';
    if (!d.audio_available) {
      document.getElementById('rec-audio-section').style.display = 'none';
      recAudio = false;
    } else {
      const micBtn = document.getElementById('fsq-mic-btn');
      if (micBtn) micBtn.style.display = '';
    }
    if (d.cam_backend === 'v4l2') {
      const nrSection = document.getElementById('ctrl-nr-section');
      if (nrSection) nrSection.style.display = 'none';
    }
  } catch (_) {}
}

function setResDisplay(res) {
  currentRes = res;
  document.getElementById('res-display').textContent = res.replace('x', ' \u00d7 ');
  updateResHighlight();
}

function updateResHighlight() {
  document.querySelectorAll('#res-options .res-dd-opt').forEach(b => {
    b.classList.toggle('active', b.dataset.res === currentRes);
  });
}

function toggleResDropdown() {
  resDDOpen ? closeResDropdown() : openResDropdown();
}

function openResDropdown() {
  resDDOpen = true;
  document.getElementById('res-options').style.display = 'block';
  document.getElementById('res-badge').classList.add('open');
}

function closeResDropdown() {
  resDDOpen = false;
  document.getElementById('res-options').style.display = 'none';
  document.getElementById('res-badge').classList.remove('open');
}

document.addEventListener('click', function(e) {
  if (resDDOpen && !document.getElementById('res-badge').contains(e.target)) {
    closeResDropdown();
  }
});

// ── Stream ─────────────────────────────────────────────────────────────────────
const _FRAME_MS = Math.round(1000 / 30);

function _tick() {
  _lastTickTime = Date.now();
  document.getElementById('stream-img').src = '/api/frame?t=' + _lastTickTime;
}

function onStreamLoad() {
  if (!streamOnline) {
    streamOnline = true;
    setStatus(true);
  }
  clearTimeout(_tickTimer);
  _tickTimer = setTimeout(_tick, _FRAME_MS);
}

function onStreamError() {
  streamOnline = false;
  setStatus(false);
  clearTimeout(_tickTimer);
  _tickTimer = setTimeout(_tick, 4000);
}

function reconnect() {
  clearTimeout(_tickTimer);
  _tick();
}

document.addEventListener('visibilitychange', function () {
  if (document.visibilityState === 'visible') reconnect();
});
// iOS web apps fire pagehide/pageshow when backgrounded/foregrounded
window.addEventListener('pageshow', function (e) {
  if (e.persisted) reconnect();
});

_tick(); // start on page load
// Watchdog: if onload/onerror hasn't fired within 4× the expected interval, force
// the next tick so a hung network request can't freeze the stream.
setInterval(function () {
  if (streamOnline && Date.now() - _lastTickTime > _FRAME_MS * 4) {
    clearTimeout(_tickTimer);
    _tick();
  }
}, _FRAME_MS * 2);

function setStatus(online) {
  document.getElementById('status-dot').className  = 'dot' + (online ? ' online' : '');
  document.getElementById('stream-dot').className  = 'dot pulse' + (online ? ' online' : '');
  document.getElementById('status-text').textContent = online ? 'Online' : 'Offline';
  document.getElementById('status-pill').classList.toggle('online', online);
}

// ── Resolution ─────────────────────────────────────────────────────────────────
async function setRes(btn) {
  const res = btn.dataset.res;
  if (res === currentRes) return;
  btn.disabled = true;
  try {
    const r = await fetch('/api/resolution', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({resolution: res}),
    });
    const d = await r.json();
    if (d.ok) {
      setResDisplay(d.resolution);
      closeResDropdown();
      toast('Resolution changed to ' + d.resolution.replace('x', ' \u00d7 '), 'success');
    } else {
      toast('Resolution change failed', 'error');
    }
  } catch (_) {
    toast('Request failed', 'error');
  }
  btn.disabled = false;
}

// ── Solo-mode helper ────────────────────────────────────────────────────────────
function closeOtherPanels(keep) {
  if (keep !== 'snap-panel') {
    snapPanelOpen = false;
    document.getElementById('snap-panel').classList.add('panel-closed');
  }
  if (keep !== 'rec-panel') {
    recPanelOpen = false;
    document.getElementById('rec-panel').classList.add('panel-closed');
  }
  if (keep !== 'tl-panel') {
    tlPanelOpen = false;
    if (!tlRunning) document.getElementById('tl-btn').classList.remove('active');
    document.getElementById('tl-panel').classList.add('panel-closed');
  }
  if (keep !== 'ctrl-panel') {
    ctrlPanelOpen = false;
    document.getElementById('ctrl-panel').classList.add('panel-closed');
    document.getElementById('ctrl-btn').classList.remove('active');
  }
  if (keep !== 'filter-panel') {
    filterPanelOpen = false;
    document.getElementById('filter-panel').classList.add('panel-closed');
    document.getElementById('filter-btn').classList.remove('active');
  }
}

// ── Section cards (Gallery / Stats) ────────────────────────────────────────────
const _secOpen = { gallery: false, stats: true };
function toggleSection(id) {
  const body    = document.getElementById(id + '-body');
  const chevron = document.getElementById(id + '-chevron');
  _secOpen[id]  = !_secOpen[id];
  body.classList.toggle('sec-closed', !_secOpen[id]);
  chevron.style.transform = _secOpen[id] ? 'rotate(180deg)' : 'rotate(0deg)';
}

// ── Snapshot panel ─────────────────────────────────────────────────────────────
function toggleSnapPanel() {
  if (snapPanelOpen) {
    snapPanelOpen = false;
    document.getElementById('snap-panel').classList.add('panel-closed');
  } else {
    closeOtherPanels('snap-panel');
    snapPanelOpen = true;
    document.getElementById('snap-panel').classList.remove('panel-closed');
  }
}

function setSnapFilter(btn) {
  document.querySelectorAll('#snap-filter-buttons .res-opt').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  snapFilter = btn.dataset.filter;
}

function setSnapQuality(btn) {
  document.querySelectorAll('#snap-quality-buttons .res-opt').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  snapQuality = parseInt(btn.dataset.q, 10);
}

async function doTakeSnapshot() {
  const btn = document.getElementById('snap-take-btn');
  btn.disabled = true;
  btn.innerHTML = `<svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="1" y="4" width="14" height="10" rx="2"/><circle cx="8" cy="9" r="2.5"/><path d="M5 4l1.5-2h3L11 4"/></svg> Capturing\u2026`;
  try {
    const r = await fetch('/api/snapshot', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({filter: snapFilter, quality: snapQuality}),
    });
    const d = await r.json();
    if (d.ok) {
      toast('\u2713 Snapshot saved', 'success');
      loadGallery();
    } else {
      toast('Snapshot failed', 'error');
    }
  } catch (_) {
    toast('Request failed', 'error');
  }
  btn.disabled = false;
  btn.innerHTML = `<svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="1" y="4" width="14" height="10" rx="2"/><circle cx="8" cy="9" r="2.5"/><path d="M5 4l1.5-2h3L11 4"/></svg> Take Photo`;
}

// ── Videos gallery ─────────────────────────────────────────────────────────────
async function loadVideos() {
  const section = document.getElementById('video-section');
  const grid    = document.getElementById('video-grid');
  const count   = document.getElementById('video-count');
  try {
    const r = await fetch('/api/videos');
    const files = await r.json();
    _videoCount = files.length;
    updateGalleryBarCount();
    if (files.length === 0) { section.style.display = 'none'; return; }
    section.style.display = '';
    count.textContent = '— ' + files.length + ' video' + (files.length !== 1 ? 's' : '');
    grid.innerHTML = '';
    files.forEach(f => {
      const a = document.createElement('a');
      a.className = 'gallery-item video-item';
      a.href = '/videos/' + f.filename;
      a.target = '_blank';
      a.title = f.filename;

      const thumbUrl = '/videos/' + f.filename.replace('.mp4', '.thumb.jpg');
      const img = document.createElement('img');
      img.alt = f.filename;
      if (f.has_thumb) {
        img.src = thumbUrl;
      } else {
        // No thumbnail yet — show a neutral placeholder and retry once
        img.style.opacity = '0.25';
        img.src = thumbUrl;
        img.onerror = () => { img.style.display = 'none'; };
        img.onload = () => { img.style.opacity = ''; img.onerror = null; };
      }
      a.appendChild(img);

      const play = document.createElement('div');
      play.className = 'video-play';
      play.innerHTML = '<svg width="30" height="30" viewBox="0 0 24 24" fill="rgba(255,255,255,0.9)"><path d="M8 5v14l11-7z"/></svg>';
      a.appendChild(play);

      const isTimelapse = f.filename.toLowerCase().startsWith('timelapse');
      const sz = f.size > 1048576 ? (f.size/1048576).toFixed(1)+' MB' : (f.size/1024).toFixed(0)+' KB';

      const lbl = document.createElement('div');
      lbl.className = 'video-label';
      lbl.textContent = isTimelapse ? 'Timelapse' : 'Video';
      a.appendChild(lbl);

      const sizeEl = document.createElement('div');
      sizeEl.className = 'video-size';
      sizeEl.textContent = sz;
      a.appendChild(sizeEl);

      const del = document.createElement('button');
      del.className = 'gallery-del';
      del.title = 'Delete';
      del.innerHTML = '&times;';
      del.onclick = e => deleteVideo(f.filename, e);
      a.appendChild(del);

      grid.appendChild(a);
    });
  } catch (_) {}
}

// ── Inline gallery ─────────────────────────────────────────────────────────────
let _photoCount = 0, _videoCount = 0;
function updateGalleryBarCount() {
  const parts = [];
  if (_photoCount > 0) parts.push(_photoCount + ' photo' + (_photoCount !== 1 ? 's' : ''));
  if (_videoCount > 0) parts.push(_videoCount + ' video' + (_videoCount !== 1 ? 's' : ''));
  document.getElementById('gallery-count').textContent = parts.join(' · ');
}

async function loadGallery() {
  const grid  = document.getElementById('gallery-grid');
  try {
    const r = await fetch('/api/gallery');
    const files = await r.json();
    _photoCount = files.length;
    updateGalleryBarCount();
    if (files.length === 0) {
      grid.innerHTML = '<div class="gallery-empty">No snapshots yet</div>';
    } else {
      grid.innerHTML = '';
      files.forEach(f => {
        const a = document.createElement('a');
        a.className = 'gallery-item';
        a.href = '/snapshots/' + f.filename;
        a.target = '_blank';
        a.title = f.filename;
        const img = document.createElement('img');
        img.src = '/snapshots/' + f.filename;
        img.loading = 'lazy';
        a.appendChild(img);

        const del = document.createElement('button');
        del.className = 'gallery-del';
        del.title = 'Delete';
        del.innerHTML = '&times;';
        del.onclick = e => deleteSnapshot(f.filename, e);
        a.appendChild(del);

        grid.appendChild(a);
      });
    }
  } catch (_) {}
}

async function deleteSnapshot(filename, e) {
  e.preventDefault();
  e.stopPropagation();
  try {
    const r = await fetch('/api/snapshot/' + encodeURIComponent(filename), {method: 'DELETE'});
    const d = await r.json();
    if (d.ok) { loadGallery(); toast('Photo deleted', ''); }
    else       { toast('Delete failed', 'error'); }
  } catch (_) { toast('Delete failed', 'error'); }
}

async function deleteVideo(filename, e) {
  e.preventDefault();
  e.stopPropagation();
  try {
    const r = await fetch('/api/videos/' + encodeURIComponent(filename), {method: 'DELETE'});
    const d = await r.json();
    if (d.ok) { loadVideos(); toast('Video deleted', ''); }
    else       { toast('Delete failed', 'error'); }
  } catch (_) { toast('Delete failed', 'error'); }
}

// ── Record panel ───────────────────────────────────────────────────────────────
function toggleRecPanel() {
  if (recording) { stopRecording(); return; }
  if (recPanelOpen) {
    recPanelOpen = false;
    document.getElementById('rec-panel').classList.add('panel-closed');
  } else {
    closeOtherPanels('rec-panel');
    recPanelOpen = true;
    document.getElementById('rec-panel').classList.remove('panel-closed');
  }
}

function setRecRes(btn) {
  document.querySelectorAll('#rec-res-buttons .res-opt').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  recTargetRes = btn.dataset.res;
}

function setRecQuality(btn) {
  document.querySelectorAll('#rec-quality-buttons .res-opt').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  recCrf = parseInt(btn.dataset.crf, 10);
}

async function startRecording() {
  // Switch camera resolution if a different one was chosen
  if (recTargetRes && recTargetRes !== currentRes) {
    const r = await fetch('/api/resolution', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({resolution: recTargetRes}),
    });
    const d = await r.json();
    if (d.ok) { currentRes = recTargetRes; updateResHighlight(); }
  }
  await fetch('/api/record/start', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({quality: recCrf, audio: recAudio}),
  });
  recording = true;
  recordStart = Date.now();
  recPanelOpen = false;
  document.getElementById('rec-panel').classList.add('panel-closed');
  document.getElementById('mic-btn').disabled = true;
  const btn = document.getElementById('record-btn');
  btn.className = 'btn btn-rec recording';
  recordTimer = setInterval(() => {
    const s  = Math.floor((Date.now() - recordStart) / 1000);
    const mm = String(Math.floor(s / 60)).padStart(2, '0');
    const ss = String(s % 60).padStart(2, '0');
    btn.innerHTML = '<svg width="10" height="10" viewBox="0 0 16 16" fill="currentColor"><rect x="3" y="3" width="10" height="10" rx="1"/></svg> ' + mm + ':' + ss;
  }, 500);
  updateFSQuickBtns();
}

async function stopRecording() {
  const r = await fetch('/api/record/stop', {method: 'POST'});
  const d = await r.json();
  recording = false;
  clearInterval(recordTimer);
  document.getElementById('mic-btn').disabled = false;
  const btn = document.getElementById('record-btn');
  btn.className = 'btn btn-rec';
  btn.innerHTML = '<svg width="13" height="13" viewBox="0 0 16 16" fill="currentColor"><circle cx="8" cy="8" r="5"/></svg> Record';
  if (d.filename && d.audio_ok === false && recAudio) {
    toast('\u2713 Recording saved (no audio captured)', 'error');
  } else if (d.filename) {
    toast('\u2713 Recording saved \u2014 converting\u2026', 'success');
  }
  setTimeout(loadVideos, 4000);
  updateFSQuickBtns();
}

// ── Timelapse interval slider ──────────────────────────────────────────────────
function onIntervalInput(idx) { setInterval_(parseInt(idx, 10)); }

function setInterval_(idx) {
  document.getElementById('tl-interval-slider').value = idx;
  document.getElementById('tl-interval-display').textContent = TL_INTERVAL_LBLS[idx];
  document.querySelectorAll('.tl-settings .tl-presets')[0]
    .querySelectorAll('.tl-preset').forEach((el, i) => el.classList.toggle('active', i === idx));
  if (!tlRunning) updateTLPreview();
}

// ── Timelapse duration slider ──────────────────────────────────────────────────
function onDurInput(idx) { setDur(parseInt(idx, 10)); }

function setDur(idx) {
  document.getElementById('tl-dur-slider').value = idx;
  document.getElementById('tl-dur-display').textContent = TL_DUR_LBLS[idx];
  document.querySelectorAll('.tl-settings .tl-presets')[1]
    .querySelectorAll('.tl-preset').forEach((el, i) => el.classList.toggle('active', i === idx));
  if (!tlRunning) updateTLPreview();
}

// ── Timelapse panel ────────────────────────────────────────────────────────────
function toggleTLPanel() {
  if (tlRunning) { stopTL(); return; }
  if (tlPanelOpen) {
    tlPanelOpen = false;
    document.getElementById('tl-panel').classList.add('panel-closed');
    document.getElementById('tl-btn').classList.remove('active');
  } else {
    closeOtherPanels('tl-panel');
    tlPanelOpen = true;
    document.getElementById('tl-panel').classList.remove('panel-closed');
    document.getElementById('tl-btn').classList.add('active');
  }
}

async function startStopTL() {
  if (tlRunning) { stopTL(); } else { await startTL(); }
}

async function startTL() {
  const iSlider  = document.getElementById('tl-interval-slider');
  const dSlider  = document.getElementById('tl-dur-slider');
  const interval = TL_INTERVAL_S[parseInt(iSlider.value, 10)];
  const duration = TL_DUR_S[parseInt(dSlider.value, 10)];
  await fetch('/api/timelapse/start', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({interval, duration}),
  });
  tlRunning      = true;
  tlStart_       = Date.now();
  tlInterval_    = interval;
  tlDuration_    = duration;
  tlNextFrameAt_ = Date.now() + interval * 1000;
  // Adaptive poll: 1 s for fast intervals, up to 30 s for slow ones
  _tlPollMs = Math.max(1000, Math.min(30000, interval * 250));
  iSlider.disabled = true;
  dSlider.disabled = true;
  document.getElementById('tl-btn').className = 'btn btn-tl active';
  const startBtn = document.getElementById('tl-start-btn');
  startBtn.className = 'btn btn-danger';
  startBtn.style.cssText = 'width:100%;justify-content:center';
  startBtn.innerHTML = '<svg width="10" height="10" viewBox="0 0 16 16" fill="currentColor"><rect x="3" y="3" width="10" height="10" rx="1"/></svg> Stop Timelapse';
  _showTLRunning(duration > 0);
  tlPollTimer  = setInterval(pollTL, _tlPollMs);
  tlClockTimer = setInterval(_tlClockTick, 1000);
  document.getElementById('tl-live-strip').style.display = '';
  _updateTLStrip(0);
  updateFSQuickBtns();
}

async function stopTL() {
  await fetch('/api/timelapse/stop', {method: 'POST'});
  _tlStopped();
  toast('Timelapse stopped', '');
}

function _tlStopped() {
  tlRunning      = false;
  tlNextFrameAt_ = null;
  clearInterval(tlPollTimer);
  clearInterval(tlClockTimer);
  tlStart_    = null;
  tlInterval_ = 0;
  tlDuration_ = 0;
  document.getElementById('tl-interval-slider').disabled = false;
  document.getElementById('tl-dur-slider').disabled = false;
  document.getElementById('tl-btn').className = 'btn btn-tl';
  document.getElementById('tl-btn').innerHTML = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="8" cy="8" r="6"/><path d="M8 4v4l2.5 2.5"/></svg> Timelapse';
  const startBtn = document.getElementById('tl-start-btn');
  startBtn.className = 'btn btn-tl';
  startBtn.style.cssText = 'width:100%;justify-content:center';
  startBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="8" cy="8" r="6"/><path d="M8 4v4l2.5 2.5"/></svg> Start Timelapse';
  _showTLIdle();
  document.getElementById('tl-live-strip').style.display = 'none';
  updateFSQuickBtns();
}

async function pollTL() {
  try {
    const r = await fetch('/api/timelapse/status');
    const d = await r.json();
    const count = d.count || 0;

    document.getElementById('frame-count').textContent = String(count).padStart(4, '0');
    if (tlDuration_ > 0) {
      const projFrames = Math.floor(tlDuration_ / tlInterval_);
      document.getElementById('tl-videst').textContent = '~' + fmtVideoLen(projFrames / 24);
    } else {
      const fph = Math.floor(3600 / tlInterval_);
      document.getElementById('tl-videst').textContent = '~' + fmtVideoLen(fph / 24) + '/hr';
    }

    // Advance next-frame estimate whenever a new frame arrives
    if (tlRunning && tlInterval_ && count > 0) {
      tlNextFrameAt_ = tlStart_ + count * tlInterval_ * 1000;
    }

    if (!d.running && tlRunning) {
      _tlStopped();
      toast('\u2713 Timelapse complete \u2014 ' + count + ' frames \u2014 compiling\u2026', 'success');
      setTimeout(loadVideos, 8000);
    }
  } catch (_) {}
}

// ── Stats ──────────────────────────────────────────────────────────────────────
function startStats() {
  fetchStats();
  statsTimer = setInterval(fetchStats, 5000);
  document.addEventListener('visibilitychange', _onVisibilityChange);
}

// Pause all background polling when the browser tab is hidden (power saving).
// Resume immediately when the tab becomes visible again.
function _onVisibilityChange() {
  if (document.hidden) {
    clearInterval(statsTimer);   statsTimer   = null;
    clearInterval(tlPollTimer);  tlPollTimer  = null;
    clearInterval(tlClockTimer); tlClockTimer = null;
  } else {
    fetchStats();
    if (!statsTimer)   statsTimer   = setInterval(fetchStats, 5000);
    if (tlRunning && !tlPollTimer)  tlPollTimer  = setInterval(pollTL, _tlPollMs);
    if (tlRunning && !tlClockTimer) tlClockTimer = setInterval(_tlClockTick, 1000);
  }
}

async function fetchStats() {
  try {
    const r = await fetch('/api/stats');
    const d = await r.json();
    renderStats(d);
  } catch (_) {}
}

// Gauge circumference for r=38: 2π×38 = 238.76
const CIRC = 238.76;

function gaugeColor(pct, warn = 60, crit = 80) {
  if (pct >= crit)  return '#ef4444';  // red
  if (pct >= warn)  return '#f59e0b';  // amber
  return '#22c55e';                    // leaf green
}

function setGauge(arcId, valId, pct, label) {
  const arc = document.getElementById(arcId);
  const val = document.getElementById(valId);
  if (!arc || !val) return;
  const clamped = Math.min(100, Math.max(0, pct));
  arc.style.strokeDashoffset = CIRC * (1 - clamped / 100);
  arc.style.stroke = gaugeColor(clamped);
  val.textContent = label;
}

function fmtBytes(bps) {
  if (bps < 1024)        return '< 1 KB/s';
  if (bps < 1024 * 1024) return (bps / 1024).toFixed(0) + ' KB/s';
  return (bps / (1024 * 1024)).toFixed(1) + ' MB/s';
}

function fmtUptime(s) {
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h > 0 ? h + 'h ' + m + 'm' : m + 'm';
}

function fmtGB(bytes) {
  return (bytes / (1024 * 1024 * 1024)).toFixed(1) + ' GB';
}

function renderStats(d) {
  // Circular gauges
  setGauge('arc-cpu',  'gv-cpu',  d.cpu_percent,  d.cpu_percent);
  setGauge('arc-ram',  'gv-ram',  d.mem_percent,  d.mem_percent);
  setGauge('arc-disk', 'gv-disk', d.disk_percent, d.disk_percent);

  if (d.temperature !== null) {
    const tPct = (d.temperature / 85) * 100;
    setGauge('arc-temp', 'gv-temp', tPct, d.temperature);
    // Override temp colour thresholds (55°C = warn, 72°C = crit)
    document.getElementById('arc-temp').style.stroke = gaugeColor(tPct, 65, 85);
  }

  // Info chips
  document.getElementById('ic-fps').textContent    = d.fps != null ? d.fps + ' fps' : '—';
  document.getElementById('ic-recv').textContent      = d.net_recv_bps != null ? fmtBytes(d.net_recv_bps) : '—';
  document.getElementById('ic-send').textContent      = d.net_send_bps != null ? fmtBytes(d.net_send_bps) : '—';
  document.getElementById('ic-wifi-rate').textContent = d.wifi_rate_mbps != null ? d.wifi_rate_mbps.toFixed(0) + ' Mb/s' : '—';
  if (d.wifi_signal_dbm != null) {
    const sig = d.wifi_signal_dbm;
    const sigEl = document.getElementById('ic-wifi-signal');
    const label = sig >= -55 ? 'Excellent' : sig >= -65 ? 'Good' : sig >= -75 ? 'Fair' : 'Weak';
    const color = sig >= -55 ? 'var(--green)' : sig >= -65 ? 'var(--green-md)' : sig >= -75 ? 'var(--amber-lt)' : 'var(--red)';
    sigEl.innerHTML = label + '<br><span style="font-size:0.85em;font-weight:400;opacity:0.8">' + sig + ' dBm</span>';
    sigEl.style.color = color;
  } else {
    document.getElementById('ic-wifi-signal').textContent = '—';
  }
  document.getElementById('ic-uptime').textContent = d.uptime ? fmtUptime(d.uptime) : '—';
  if (d.mem_used != null && d.mem_total != null) {
    document.getElementById('ic-ram-usage').textContent = fmtGB(d.mem_used) + ' / ' + fmtGB(d.mem_total);
  }
  if (d.disk_used != null && d.disk_total != null) {
    document.getElementById('ic-disk-used').textContent  = fmtGB(d.disk_used);
    document.getElementById('ic-disk-free').textContent  = fmtGB(d.disk_total - d.disk_used);
    document.getElementById('ic-disk-total').textContent = fmtGB(d.disk_total);
  }
}

// ── Toast ──────────────────────────────────────────────────────────────────────
let toastTimer = null;
function toast(msg, type = '') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast' + (type ? ' ' + type : '') + ' show';
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 3000);
}

// ── Film filter panel ──────────────────────────────────────────────────────────

const FILM_DESCS = {
  none:      '',
  portra:    'Warm skin tones, lifted shadows, creamy pastel palette.',
  velvia:    'Punchy, vivid colours with deep shadows — landscape favourite.',
  hp5:       'Classic panchromatic B&W — smooth mids, full tonal range.',
  cinestill: 'Tungsten-balanced motion-picture stock with cyan/teal shadows.',
  trix:      'High-contrast B&W street film — deep blacks, snappy highlights.',
  provia:    'Accurate, neutral colours with a crisp, slightly cool character.',
  ektar:     'Hyper-saturated fine-grain film — vivid reds and deep blues.',
  agfa:      'Warm, faded vintage look with a gentle green cast in mids.',
};

function toggleMic() {
  recAudio = !recAudio;
  const btn = document.getElementById('mic-btn');
  btn.classList.toggle('active', recAudio);
  btn.innerHTML = (recAudio
    ? '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a3 3 0 0 1 3 3v7a3 3 0 0 1-6 0V5a3 3 0 0 1 3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="22"/></svg> Microphone On'
    : '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a3 3 0 0 1 3 3v7a3 3 0 0 1-6 0V5a3 3 0 0 1 3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="22"/></svg> Microphone Off');
}

// Mic icon SVG paths — 36×36 to match the initial HTML icon size
const _MIC_PATH = '<path d="M12 2a3 3 0 0 1 3 3v7a3 3 0 0 1-6 0V5a3 3 0 0 1 3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="22"/>';
const _MIC_ON_SVG  = `<svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${_MIC_PATH}</svg>`;
const _MIC_OFF_SVG = `<svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${_MIC_PATH}<line x1="2" y1="2" x2="22" y2="22"/></svg>`;

// Returns true if the browser supports Fetch streaming (ReadableStream from fetch).
// iOS Safari < 14.5 and some older browsers return a non-readable resp.body.
function _fetchStreamingSupported() {
  return typeof ReadableStream !== 'undefined' &&
         typeof ReadableStream.prototype.getReader === 'function';
}

function toggleStreamAudio() {
  if (!streamAudio) {
    // AudioContext must be created/resumed synchronously inside the user-gesture
    // handler — iOS Safari rejects it if deferred into an async callback.
    if (!_audioCtx) {
      _audioCtx = new (window.AudioContext || window.webkitAudioContext)({ latencyHint: 'interactive' });
    }
    if (_audioCtx.state === 'suspended') _audioCtx.resume();
    streamAudio = true;
    _updateFSQMicBtn();
    if (_fetchStreamingSupported()) {
      _startStreamAudio('/api/audio/stream/raw');
    } else {
      // Fallback for Safari: use an <audio> element with the AAC/ADTS stream.
      // AAC is Apple's own codec — guaranteed to work in Safari's <audio> element.
      const audio = document.getElementById('live-audio');
      audio.src = '/api/audio/stream';
      audio.play().catch(() => {});
    }
  } else {
    streamAudio = false;
    _stopStreamAudio();
    _updateFSQMicBtn();
  }
}

async function _startStreamAudio(url) {
  try {
    const resp = await fetch(url);
    if (!resp.ok || !resp.body || typeof resp.body.getReader !== 'function') {
      // Streaming not actually supported at runtime — fall back to AAC <audio>.
      streamAudio = true;  // keep button on
      const audio = document.getElementById('live-audio');
      audio.src = '/api/audio/stream';
      audio.play().catch(() => {});
      return;
    }
    _audioReader   = resp.body.getReader();
    _audioNext     = _audioCtx.currentTime + _AUDIO_AHEAD;
    _audioLeftover = null;
    while (true) {
      const { done, value } = await _audioReader.read();
      if (done) break;
      let data = value;
      if (_audioLeftover) {
        const m = new Uint8Array(_audioLeftover.length + value.length);
        m.set(_audioLeftover); m.set(value, _audioLeftover.length);
        data = m; _audioLeftover = null;
      }
      const usable = data.length & ~1;
      if (data.length & 1) _audioLeftover = data.slice(usable);
      if (usable === 0) continue;
      const samples = usable >> 1;
      const f32     = new Float32Array(samples);
      for (let i = 0; i < samples; i++) {
        f32[i] = ((data[i*2] | (data[i*2+1] << 8)) << 16 >> 16) / 32768.0;
      }
      const buf = _audioCtx.createBuffer(1, samples, _AUDIO_SR);
      buf.copyToChannel(f32, 0);
      const src = _audioCtx.createBufferSource();
      src.buffer = buf;
      src.connect(_audioCtx.destination);
      const now = _audioCtx.currentTime;
      if (_audioNext < now) _audioNext = now + _AUDIO_AHEAD;
      src.start(_audioNext);
      _audioNext += samples / _AUDIO_SR;
    }
  } catch (_) { /* stream cancelled or connection lost */ }
  if (streamAudio) { streamAudio = false; _updateFSQMicBtn(); }
}

function _stopStreamAudio() {
  if (_audioReader) { _audioReader.cancel(); _audioReader = null; }
  _audioLeftover = null;
  const audio = document.getElementById('live-audio');
  audio.pause(); audio.src = '';
}

function _updateFSQMicBtn() {
  const btn = document.getElementById('fsq-mic-btn');
  if (!btn) return;
  btn.classList.toggle('active', streamAudio);
  btn.title = streamAudio ? 'Mute Audio' : 'Unmute Audio';
  btn.innerHTML = streamAudio ? _MIC_ON_SVG : _MIC_OFF_SVG;
}

function toggleFilterPanel() {
  if (filterPanelOpen) {
    filterPanelOpen = false;
    document.getElementById('filter-panel').classList.add('panel-closed');
    document.getElementById('filter-btn').classList.remove('active');
  } else {
    closeOtherPanels('filter-panel');
    filterPanelOpen = true;
    document.getElementById('filter-panel').classList.remove('panel-closed');
    document.getElementById('filter-btn').classList.add('active');
  }
}

function setFilm(el) {
  document.querySelectorAll('#film-chips .film-card, #film-chips .film-clear').forEach(b => b.classList.remove('active'));
  el.classList.add('active');
  const film = el.dataset.film;
  _ctrl.film_filter = film;
  _ctrl.film_strength = 100;
  // Reset all in-card sliders to 100% when switching filters
  document.querySelectorAll('.film-strength input[type=range]').forEach(sl => {
    sl.value = 100;
    sl.closest('.film-strength').querySelector('.film-strength-pct').textContent = '100%';
  });
  document.getElementById('film-desc').textContent = FILM_DESCS[film] || '';
  document.getElementById('filter-btn').classList.toggle('active', film !== 'none');
  sendCtrl();
}

function onFilmStrength(el) {
  const v = parseInt(el.value, 10);
  _ctrl.film_strength = v;
  el.closest('.film-strength').querySelector('.film-strength-pct').textContent = v + '%';
  scheduleCtrl();
}

// ── Camera controls panel ──────────────────────────────────────────────────────

// Fallback defaults (used before server defaults arrive)
const CTRL_DEFAULTS = {
  exposure_time: 0, analogue_gain: 0.0,
  awb_mode: 'auto', awb_kelvin: 5600,
  sharpness: 1.0, contrast: 1.0, noise_reduction: 'off',
  hdr_mode: 0, ae_metering_mode: 0, ae_constraint_mode: 0,
  brightness: 0, saturation: 0, tint: 0, warmth: 40,
  backlight_compensation: 0,
  hflip: false, vflip: false,
  film_filter: 'none',
  film_strength: 100,
};
// Loaded from server on startup; used by Reset button
let _serverDefaults = Object.assign({}, CTRL_DEFAULTS);
let _ctrl = Object.assign({}, CTRL_DEFAULTS);
let _ctrlDebounce = null;

// Discrete shutter speed steps (microseconds); index 0 = Auto
const SHUTTER_STEPS = [0, 500, 2000, 8333, 16667, 33333, 66667, 250000, 1000000];
const GAIN_STEPS = [0, 1, 2, 4, 8, 16];
let _shutterAngleMode = false;

function fmtShutterStep(idx) {
  const us = SHUTTER_STEPS[idx | 0];
  if (us === 0) return 'Auto';
  if (_shutterAngleMode) return Math.round(us * 360 / 33333) + '°';
  if (us >= 1000000) return (us / 1000000).toFixed(1) + 's';
  return '1/' + Math.round(1000000 / us);
}

function _exposureToStep(us) {
  let best = 0, bestDist = Infinity;
  for (let i = 0; i < SHUTTER_STEPS.length; i++) {
    const d = Math.abs(SHUTTER_STEPS[i] - us);
    if (d < bestDist) { bestDist = d; best = i; }
  }
  return best;
}

function onShutterSlider(el) {
  const idx = parseInt(el.value, 10);
  _ctrl.exposure_time = SHUTTER_STEPS[idx];
  document.getElementById('ctrl-val-exposure').textContent = fmtShutterStep(idx);
  document.querySelectorAll('#shutter-presets .tl-preset').forEach((p, i) =>
    p.classList.toggle('active', i === idx));
  scheduleCtrl();
}

function setShutterStep(idx) {
  const sl = document.getElementById('ctrl-sl-exposure');
  sl.value = idx;
  onShutterSlider(sl);
}

function onGainSlider(el) {
  const idx = parseInt(el.value, 10);
  const v = GAIN_STEPS[idx];
  _ctrl.analogue_gain = v;
  document.getElementById('ctrl-val-gain').textContent = fmtGain(v);
  document.querySelectorAll('#gain-presets .tl-preset').forEach((p, i) =>
    p.classList.toggle('active', i === idx));
  scheduleCtrl();
}

function setGainStep(idx) {
  const sl = document.getElementById('ctrl-sl-gain');
  sl.value = idx;
  onGainSlider(sl);
}

function _gainToStep(v) {
  let best = 0, bestDist = Infinity;
  GAIN_STEPS.forEach((s, i) => {
    const d = Math.abs(s - v);
    if (d < bestDist) { bestDist = d; best = i; }
  });
  return best;
}

function toggleShutterMode() {
  _shutterAngleMode = !_shutterAngleMode;
  document.getElementById('shutter-angle-btn').classList.toggle('active', _shutterAngleMode);
  const lbl = document.getElementById('shutter-mode-label');
  lbl.innerHTML = _shutterAngleMode
    ? 'Shutter angle <span style="color:var(--dim)">(0 = auto, @30fps)</span>'
    : 'Shutter speed <span style="color:var(--dim)">(0 = auto)</span>';
  const sl = document.getElementById('ctrl-sl-exposure');
  document.getElementById('ctrl-val-exposure').textContent = fmtShutterStep(parseInt(sl.value, 10));
}

function toggleFeedFlip(key) {
  _ctrl[key] = !_ctrl[key];
  document.getElementById('feed-hflip-btn').classList.toggle('active', _ctrl.hflip);
  document.getElementById('feed-vflip-btn').classList.toggle('active', _ctrl.vflip);
  scheduleCtrl();
}

function toggleCtrlPanel() {
  if (ctrlPanelOpen) {
    ctrlPanelOpen = false;
    document.getElementById('ctrl-panel').classList.add('panel-closed');
    document.getElementById('ctrl-btn').classList.remove('active');
  } else {
    closeOtherPanels('ctrl-panel');
    ctrlPanelOpen = true;
    document.getElementById('ctrl-panel').classList.remove('panel-closed');
    document.getElementById('ctrl-btn').classList.add('active');
  }
}

// Called by every slider — updates _ctrl, refreshes display label, debounces send
function onCtrlSlider(key, el, lblId, fmt) {
  const v = parseFloat(el.value);
  _ctrl[key] = v;
  document.getElementById(lblId).textContent = fmt(v);
  scheduleCtrl();
}

const _AWB_PRESET_K = { daylight: 5600, cloudy: 6500, fluorescent: 4000, indoor: 3200, tungsten: 2900 };

function setCtrlAwb(el) {
  document.querySelectorAll('#ctrl-awb-btns .res-opt').forEach(b => b.classList.remove('active'));
  el.classList.add('active');
  const mode = el.dataset.awb;
  if (mode === 'auto') {
    _ctrl.awb_mode = 'auto';
  } else {
    _ctrl.awb_mode = 'manual';
    if (_AWB_PRESET_K[mode] !== undefined) {
      _ctrl.awb_kelvin = _AWB_PRESET_K[mode];
      _setKelvinDisplay(_ctrl.awb_kelvin);
    }
  }
  document.getElementById('ctrl-kelvin-row').style.display = mode === 'auto' ? 'none' : '';
  scheduleCtrl();
}

function onKelvinSlider(el) {
  const k = parseInt(el.value, 10);
  _ctrl.awb_kelvin = k;
  document.getElementById('ctrl-val-ktemp').textContent = k + ' K';
  document.querySelectorAll('#ctrl-awb-btns .res-opt').forEach(b =>
    b.classList.toggle('active', b.dataset.awb !== 'auto' && _AWB_PRESET_K[b.dataset.awb] === k));
  scheduleCtrl();
}

function _setKelvinDisplay(k) {
  const sl = document.getElementById('ctrl-sl-ktemp');
  const lbl = document.getElementById('ctrl-val-ktemp');
  if (sl) sl.value = k;
  if (lbl) lbl.textContent = k + ' K';
}

function setCtrlNR(el) {
  document.querySelectorAll('#ctrl-nr-btns .res-opt').forEach(b => b.classList.remove('active'));
  el.classList.add('active');
  _ctrl.noise_reduction = el.dataset.nr;
  scheduleCtrl();
}

function setCtrlHDR(el) {
  document.querySelectorAll('#ctrl-hdr-btns .res-opt').forEach(b => b.classList.remove('active'));
  el.classList.add('active');
  const on = parseInt(el.dataset.hdr, 10) !== 0;
  _ctrl.hdr_mode           = on ? 2 : 0;
  _ctrl.ae_metering_mode   = on ? 2 : 0;
  _ctrl.ae_constraint_mode = on ? 2 : 0;
  scheduleCtrl();
}

function toggleCtrlFlip(key, btn) {
  _ctrl[key] = !_ctrl[key];
  btn.classList.toggle('active', _ctrl[key]);
  scheduleCtrl();
}

function scheduleCtrl() {
  clearTimeout(_ctrlDebounce);
  _ctrlDebounce = setTimeout(sendCtrl, 80);
}

async function sendCtrl() {
  try {
    await fetch('/api/camera_controls', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(_ctrl),
    });
  } catch (_) {}
}

async function loadCtrlDefaults() {
  try {
    const r = await fetch('/api/camera_controls/defaults');
    const d = await r.json();
    Object.assign(_serverDefaults, d);
    Object.assign(_ctrl, d);
    syncCtrlUI();
  } catch (_) {}
}

async function resetCtrlDefaults() {
  Object.assign(_ctrl, _serverDefaults);
  syncCtrlUI();
  await sendCtrl();
  toast('Controls reset to defaults', 'success');
}

function syncCtrlUI() {
  // Sliders + value labels
  // Exposure: discrete step slider
  const expStep = _exposureToStep(_ctrl.exposure_time);
  const expSl = document.getElementById('ctrl-sl-exposure');
  if (expSl) expSl.value = expStep;
  const expLbl = document.getElementById('ctrl-val-exposure');
  if (expLbl) expLbl.textContent = fmtShutterStep(expStep);
  document.querySelectorAll('#shutter-presets .tl-preset').forEach((p, i) =>
    p.classList.toggle('active', i === expStep));

  // Gain: discrete step slider
  const gainStep = _gainToStep(_ctrl.analogue_gain);
  const gainSl = document.getElementById('ctrl-sl-gain');
  if (gainSl) gainSl.value = gainStep;
  const gainLbl = document.getElementById('ctrl-val-gain');
  if (gainLbl) gainLbl.textContent = fmtGain(_ctrl.analogue_gain);
  document.querySelectorAll('#gain-presets .tl-preset').forEach((p, i) =>
    p.classList.toggle('active', i === gainStep));

  const sliders = [
    ['ctrl-sl-sharpness', 'ctrl-val-sharpness', _ctrl.sharpness,      v => v.toFixed(1)],
    ['ctrl-sl-contrast',  'ctrl-val-contrast',  _ctrl.contrast,       v => v.toFixed(2)],
    ['ctrl-sl-brightness','ctrl-val-brightness',_ctrl.brightness,     v => (v > 0 ? '+' : '') + v],
    ['ctrl-sl-saturation','ctrl-val-saturation',_ctrl.saturation,     v => (v > 0 ? '+' : '') + v],
    ['ctrl-sl-warmth',    'ctrl-val-warmth',    _ctrl.warmth,         fmtWarmth],
    ['ctrl-sl-tint',      'ctrl-val-tint',      _ctrl.tint,           fmtTint],
  ];
  sliders.forEach(([slId, lblId, val, fmt]) => {
    const sl = document.getElementById(slId);
    if (sl) sl.value = val;
    const lbl = document.getElementById(lblId);
    if (lbl) lbl.textContent = fmt(val);
  });
  // AWB
  document.querySelectorAll('#ctrl-awb-btns .res-opt').forEach(b => {
    const isAuto   = b.dataset.awb === 'auto' && _ctrl.awb_mode === 'auto';
    const isPreset = b.dataset.awb !== 'auto' && _ctrl.awb_mode !== 'auto'
                     && _AWB_PRESET_K[b.dataset.awb] === _ctrl.awb_kelvin;
    b.classList.toggle('active', isAuto || isPreset);
  });
  const _kRow = document.getElementById('ctrl-kelvin-row');
  if (_kRow) _kRow.style.display = _ctrl.awb_mode === 'auto' ? 'none' : '';
  _setKelvinDisplay(_ctrl.awb_kelvin);
  // NR chips
  document.querySelectorAll('#ctrl-nr-btns .res-opt').forEach(b =>
    b.classList.toggle('active', b.dataset.nr === _ctrl.noise_reduction));
  // HDR toggle
  document.querySelectorAll('#ctrl-hdr-btns .res-opt').forEach(b =>
    b.classList.toggle('active', parseInt(b.dataset.hdr, 10) === _ctrl.hdr_mode));
  // Flip buttons (feed overlay)
  document.getElementById('feed-hflip-btn').classList.toggle('active', _ctrl.hflip);
  document.getElementById('feed-vflip-btn').classList.toggle('active', _ctrl.vflip);
  // Film chips
  document.querySelectorAll('#film-chips .film-card, #film-chips .film-clear').forEach(b =>
    b.classList.toggle('active', b.dataset.film === _ctrl.film_filter));
  document.getElementById('film-desc').textContent = FILM_DESCS[_ctrl.film_filter] || '';
  document.getElementById('filter-btn').classList.toggle('active', _ctrl.film_filter !== 'none');
  // Sync in-card strength slider for the active film
  const _activeCard = document.querySelector('#film-chips .film-card.active');
  if (_activeCard) {
    const _sSl = _activeCard.querySelector('.film-strength input[type=range]');
    const _sPct = _activeCard.querySelector('.film-strength-pct');
    if (_sSl)  _sSl.value = _ctrl.film_strength;
    if (_sPct) _sPct.textContent = _ctrl.film_strength + '%';
  }
}

// Value formatters
function fmtExposure(v) {
  if (v === 0) return 'Auto';
  if (v < 1000) return v + ' µs';
  return (v / 1000).toFixed(1) + ' ms';
}
function fmtGain(v) { return v === 0 ? 'Auto' : '×' + v.toFixed(1); }
function fmtWarmth(v) {
  if (v === 0) return 'Neutral';
  if (v > 0)   return '+' + v + ' warm';
  return Math.abs(v) + ' cool';
}
function fmtTint(v) {
  if (v === 0) return 'Neutral';
  if (v > 0)   return '+' + v + ' magenta';
  return Math.abs(v) + ' green';
}

// ── Timelapse time/duration helpers ────────────────────────────────────────────
function fmtHMS(secs) {
  const s = Math.max(0, Math.round(secs));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) return `${h}:${String(m).padStart(2,'0')}:${String(sec).padStart(2,'0')}`;
  return `${String(m).padStart(2,'0')}:${String(sec).padStart(2,'0')}`;
}
function fmtVideoLen(secs) {
  if (secs < 60) return secs.toFixed(1) + 's';
  const m = Math.floor(secs / 60);
  const s = Math.round(secs % 60);
  return s > 0 ? `${m}m ${s}s` : `${m}m`;
}

// Compute and render the idle "preview" line from current slider positions.
// Update the frame counter + video estimate with projected values from current slider pos.
// Called whenever sliders move (idle) or on load.
function updateTLPreview() {
  const iIdx     = parseInt(document.getElementById('tl-interval-slider').value, 10);
  const dIdx     = parseInt(document.getElementById('tl-dur-slider').value, 10);
  const interval = TL_INTERVAL_S[iIdx];
  const duration = TL_DUR_S[dIdx];
  const fc  = document.getElementById('frame-count');
  const vid = document.getElementById('tl-videst');
  if (duration === 0) {
    const fph = Math.floor(3600 / interval);
    fc.textContent  = '∞';
    vid.textContent = `~${fmtVideoLen(fph / 24)}/hr`;
  } else {
    const frames = Math.floor(duration / interval);
    fc.textContent  = String(frames);
    vid.textContent = `~${fmtVideoLen(frames / 24)}`;
  }
}

// Switch status block between running / idle appearances.
function _showTLRunning(hasDeadline) {
  document.getElementById('tl-prog-track').style.display  = hasDeadline ? 'block' : 'none';
  document.getElementById('tl-time-row').style.display    = 'flex';
  document.getElementById('tl-rem-col').style.opacity     = hasDeadline ? '1' : '0.35';
  document.getElementById('tl-next-row').style.display    = 'flex';
  document.getElementById('tl-frame-lbl').textContent     = 'frames captured';
  document.getElementById('tl-videst-lbl').textContent    = 'video length';
  document.getElementById('frame-count').style.opacity    = '1';
  document.getElementById('tl-videst').style.opacity      = '1';
}
function _showTLIdle() {
  document.getElementById('tl-prog-track').style.display  = 'none';
  document.getElementById('tl-time-row').style.display    = 'none';
  document.getElementById('tl-next-row').style.display    = 'none';
  document.getElementById('tl-frame-lbl').textContent     = 'total frames';
  document.getElementById('tl-videst-lbl').textContent    = 'video length';
  document.getElementById('frame-count').style.opacity    = '0.55';
  document.getElementById('tl-videst').style.opacity      = '0.75';
  updateTLPreview();
}

// Update the persistent countdown strip above the panels.
function _updateTLStrip(elapsed) {
  const prog = document.getElementById('tl-strip-prog');
  const fill = document.getElementById('tl-strip-fill');
  const timeEl = document.getElementById('tl-strip-time');
  const subEl  = document.getElementById('tl-strip-sub');
  const frmEl  = document.getElementById('tl-strip-frames');
  if (!timeEl) return;
  const count = parseInt(document.getElementById('frame-count').textContent, 10) || 0;
  frmEl.textContent = count > 0 ? count.toLocaleString() + ' frames captured' : '';
  if (tlDuration_ > 0) {
    const rem = Math.max(0, tlDuration_ - elapsed);
    timeEl.textContent = fmtHMS(rem);
    subEl.textContent  = 'remaining';
    prog.style.display = 'block';
    fill.style.width   = Math.min(100, elapsed / tlDuration_ * 100) + '%';
  } else {
    timeEl.textContent = fmtHMS(elapsed);
    subEl.textContent  = 'elapsed';
    prog.style.display = 'none';
  }
}

// 1-second clock tick: updates elapsed, remaining, next-shot countdown, and tl-btn.
function _tlClockTick() {
  if (!tlRunning || !tlStart_) return;
  const elapsed = (Date.now() - tlStart_) / 1000;

  document.getElementById('tl-elapsed').textContent = fmtHMS(elapsed);

  const remEl = document.getElementById('tl-remaining');
  if (tlDuration_ > 0) {
    remEl.textContent = fmtHMS(Math.max(0, tlDuration_ - elapsed));
    const pct = Math.min(100, elapsed / tlDuration_ * 100);
    document.getElementById('tl-prog-fill').style.width = pct + '%';
  } else {
    remEl.textContent = '∞';
  }

  if (tlNextFrameAt_) {
    const sec = Math.max(0, Math.ceil((tlNextFrameAt_ - Date.now()) / 1000));
    document.getElementById('tl-next-txt').textContent =
      sec <= 1 ? 'capturing…' : `next shot in ${sec}s`;
  }

  // Also keep the tl-btn header badge updated
  const s  = Math.floor(elapsed);
  const mm = String(Math.floor(s / 60)).padStart(2, '0');
  const ss = String(s % 60).padStart(2, '0');
  document.getElementById('tl-btn').innerHTML =
    '<svg width="11" height="11" viewBox="0 0 16 16" fill="currentColor"><rect x="3" y="3" width="10" height="10" rx="1"/></svg> ' + mm + ':' + ss;

  _updateTLStrip(elapsed);
}

// ── Inject strength slider into each film card ─────────────────────────────────
document.querySelectorAll('#film-chips .film-card').forEach(card => {
  const body = card.querySelector('.film-body');
  if (!body) return;
  const div = document.createElement('div');
  div.className = 'film-strength';
  div.innerHTML =
    '<div class="film-strength-hdr">' +
      '<span class="film-strength-lbl">Strength</span>' +
      '<span class="film-strength-pct">100%</span>' +
    '</div>' +
    '<input type="range" min="0" max="100" step="1" value="100" ' +
           'oninput="onFilmStrength(this)" style="touch-action:none">';
  // Prevent slider clicks/touches from bubbling to the film-card button,
  // which would call setFilm() and reset strength back to 100.
  div.addEventListener('pointerdown', e => e.stopPropagation());
  div.addEventListener('click',       e => e.stopPropagation());
  body.appendChild(div);
});

// ── Keyboard shortcuts ─────────────────────────────────────────────────────────
document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT') return;
  if (e.key === 's' || e.key === 'S') takeSnapshot();
  if (e.key === 'r' || e.key === 'R') toggleRecord();
  if (e.key === 't' || e.key === 'T') toggleTimelapse();
});
