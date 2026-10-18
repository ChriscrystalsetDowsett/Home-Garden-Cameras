'use strict';
/* Quick capture + moving timelapses on the camera page.

   Quick capture: one tap each for a full-resolution photo, a 30 s clip (it
   stops on the camera by itself) and a 30 min timelapse. Tapping a running
   one stops it.

   Moving timelapse (cameras with a pan/tilt rig): pick a move (Glide between
   two saved views, Room sweep, Rise), a length, preview the move, start.
   Presets come from GET /api/timelapse/presets, so the camera decides.

   Uses apiPost(), toast(), syncTLFromServer(), syncRecFromServer(),
   loadGallery() and loadVideos() from index.js. */

const QC_VIDEO_SECONDS = 30;
const QC_TL_PRESET = 'tl30';
const MOTION_LENGTHS = [            // [label, seconds, interval]
  ['10 min', 600, 5], ['30 min', 1800, 10], ['1 hour', 3600, 12], ['2 hours', 7200, 24],
];

let _qc = { rec: null, tl: null, busy: false };
let _motion = { presets: [], views: [], preset: null, from: null, to: null, len: 1 };

function _el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function _mmss(s) {
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(ss).padStart(2, '0')}`
           : `${m}:${String(ss).padStart(2, '0')}`;
}

// ── Quick capture ──────────────────────────────────────────────────────────────

async function qcPhoto() {
  if (_qc.busy) return;
  _qc.busy = true;
  const tile = document.getElementById('qc-photo');
  tile.classList.add('working');
  document.getElementById('qc-photo-sub').textContent = 'Capturing…';
  const d = await apiPost('/api/snapshot', { quality: 95 });
  tile.classList.remove('working');
  if (d) {
    tile.classList.add('done');
    document.getElementById('qc-photo-sub').textContent = 'Saved ✓';
    toast('✓ Photo saved', 'success');
    loadGallery();
    setTimeout(() => {
      tile.classList.remove('done');
      document.getElementById('qc-photo-sub').textContent = 'Full resolution';
    }, 2500);
  } else {
    document.getElementById('qc-photo-sub').textContent = 'Full resolution';
  }
  _qc.busy = false;
}

async function qcVideo() {
  if (recording) { await stopRecording(); qcRefresh(); return; }
  const ok = await startRecording(QC_VIDEO_SECONDS);
  if (ok) toast('Recording 30 seconds', '');
  qcRefresh();
}

async function qcTimelapse() {
  if (tlRunning) {
    if (!confirm('Stop the timelapse? The frames so far become the video.')) return;
    await stopTL();
    qcRefresh();
    return;
  }
  const d = await apiPost('/api/timelapse/start', { preset: QC_TL_PRESET });
  if (!d) return;
  toast('Timelapse started · 30 min', 'success');
  await syncTLFromServer();
  qcRefresh(d);
}

async function qcRefresh(tlStatus) {
  try {
    const rec = await (await fetch('/api/record/status')).json();
    const tl = tlStatus || await (await fetch('/api/timelapse/status')).json();
    _qc.rec = rec; _qc.tl = tl;
  } catch (_) { return; }
  const rec = _qc.rec, tl = _qc.tl;

  const v = document.getElementById('qc-video');
  const vsub = document.getElementById('qc-video-sub');
  if (v) {
    v.classList.toggle('active', !!rec.running);
    vsub.textContent = rec.running
      ? (rec.limit ? _mmss(rec.limit - rec.duration) + ' left · tap to stop' : _mmss(rec.duration) + ' · tap to stop')
      : '1080p · stops by itself';
  }
  const t = document.getElementById('qc-tl');
  const tsub = document.getElementById('qc-tl-sub');
  if (t) {
    t.classList.toggle('active', !!tl.running);
    if (tl.running) {
      const left = tl.duration ? tl.duration - tl.elapsed : null;
      tsub.textContent = `${tl.count}${tl.expected ? '/' + tl.expected : ''} frames` +
                         (left != null ? ` · ${_mmss(left)} left` : '');
    } else {
      tsub.textContent = '4K · a frame every 10 s';
    }
  }
  _renderMotionRunning(tl);
}

// ── Moving timelapse ───────────────────────────────────────────────────────────

async function motionInit() {
  let d;
  try { d = await (await fetch('/api/timelapse/presets')).json(); } catch (_) { return; }
  if (!d.motion || !d.motion.length) return;          // no rig on this camera
  _motion.presets = d.motion;
  _motion.views = d.views || [];
  _motion.preset = d.motion[0].id;
  if (_motion.views.length >= 2) {
    _motion.from = _motion.views[0].id;
    _motion.to = _motion.views[_motion.views.length - 1].id;
  }
  document.getElementById('motion-card').hidden = false;
  _renderMotion();
}

function _chip(label, active, onclick, extra) {
  const b = _el('button', 'mc-chip' + (active ? ' active' : '') + (extra ? ' ' + extra : ''), label);
  b.type = 'button';
  b.onclick = onclick;
  return b;
}

function _renderMotion() {
  const host = document.getElementById('motion-body');
  host.replaceChildren();
  const p = _motion.presets.find(x => x.id === _motion.preset);

  const kinds = _el('div', 'mc-row mc-kinds');
  _motion.presets.forEach(x => kinds.appendChild(
    _chip(x.name, x.id === _motion.preset, () => { _motion.preset = x.id; _renderMotion(); }, 'mc-kind')));
  host.appendChild(kinds);
  host.appendChild(_el('p', 'mc-detail', p.detail.split(' · ')[0]));

  if (p.kind === 'views') {
    if (_motion.views.length < 2) {
      host.appendChild(_el('p', 'mc-detail', 'Save at least two camera views to glide between them.'));
    } else {
      [['From', 'from'], ['To', 'to']].forEach(([label, key]) => {
        const row = _el('div', 'mc-row');
        row.appendChild(_el('span', 'mc-label', label));
        const chips = _el('div', 'mc-chips');
        _motion.views.forEach(v => chips.appendChild(_chip(v.name, _motion[key] === v.id, () => {
          _motion[key] = v.id;
          const other = key === 'from' ? 'to' : 'from';
          if (_motion[other] === v.id) {
            const alt = _motion.views.find(w => w.id !== v.id);
            _motion[other] = alt ? alt.id : null;
          }
          _renderMotion();
        })));
        row.appendChild(chips);
        host.appendChild(row);
      });
    }
  }

  const lens = _el('div', 'mc-row');
  lens.appendChild(_el('span', 'mc-label', 'Length'));
  const lchips = _el('div', 'mc-chips');
  MOTION_LENGTHS.forEach(([label], i) => lchips.appendChild(
    _chip(label, i === _motion.len, () => { _motion.len = i; _renderMotion(); })));
  lens.appendChild(lchips);
  host.appendChild(lens);

  const [, secs, interval] = MOTION_LENGTHS[_motion.len];
  const frames = Math.floor(secs / interval) + 1;
  host.appendChild(_el('p', 'mc-estimate',
    `${frames} frames, one every ${interval} s → ${Math.round(frames / 24)} s of 4K video`));

  const actions = _el('div', 'mc-actions');
  const prev = _el('button', 'btn btn-secondary', 'Preview move');
  prev.type = 'button';
  prev.onclick = motionPreview;
  const go = _el('button', 'btn btn-tl', 'Start moving timelapse');
  go.type = 'button';
  go.id = 'mc-start';
  go.onclick = motionStart;
  actions.append(prev, go);
  host.appendChild(actions);
  host.appendChild(_el('p', 'mc-note',
    'The camera views are locked while it runs. Focus is set so near and far stay sharp.'));
}

function _motionBody() {
  const [, secs, interval] = MOTION_LENGTHS[_motion.len];
  const body = { preset: _motion.preset, duration: secs, interval };
  if (_motion.presets.find(x => x.id === _motion.preset)?.kind === 'views') {
    body.from = _motion.from; body.to = _motion.to;
  }
  return body;
}

async function motionPreview() {
  const d = await apiPost('/api/timelapse/preview', _motionBody());
  if (d) toast('Previewing the move…', '');
}

async function motionStart() {
  if (tlRunning) { toast('A timelapse is already running', 'error'); return; }
  const d = await apiPost('/api/timelapse/start', _motionBody());
  if (!d) return;
  toast('Moving timelapse started', 'success');
  await syncTLFromServer();
  qcRefresh(d);
}

function _renderMotionRunning(tl) {
  const card = document.getElementById('motion-card');
  if (!card || card.hidden) return;
  const run = document.getElementById('motion-running');
  const body = document.getElementById('motion-body');
  const moving = tl && tl.running && tl.motion;
  card.classList.toggle('running', !!moving);
  run.hidden = !moving;
  body.hidden = !!moving;
  if (!moving) return;
  const left = tl.duration ? tl.duration - tl.elapsed : 0;
  const pct = tl.expected ? Math.min(100, 100 * tl.count / tl.expected) : 0;
  document.getElementById('mr-title').textContent = tl.title || 'Moving timelapse';
  document.getElementById('mr-progress').style.width = pct + '%';
  document.getElementById('mr-meta').textContent =
    `Frame ${tl.count} of ${tl.expected} · ${_mmss(left)} left`;
}

async function motionStop() {
  if (!confirm('Stop the moving timelapse? The frames so far become the video.')) return;
  await stopTL();
  qcRefresh();
}

document.addEventListener('DOMContentLoaded', () => {
  qcRefresh();
  motionInit();
  setInterval(() => { if (document.visibilityState === 'visible') qcRefresh(); }, 2000);
});
