'use strict';
/* ── Pan/tilt views (shared by the camera page and the dashboard) ────────────
 *
 *   PTZ.chips(el, base, {big})   one-tap view pills (e.g. over a dashboard tile)
 *   PTZ.panel(el, base)          thumbnails, fine-tune arrows, save + edit
 *
 * base is the URL prefix of the camera's API: '' on the camera's own page,
 * '/dashboard/cam/<idx>/proxy' on the dashboard.  A camera without a rig
 * answers {"enabled": false} (or 404 on older code) and nothing is drawn, so
 * every camera can be wired up the same way.
 *
 * One poller per camera is shared by all widgets showing it: slow while idle,
 * fast while the camera is moving.  Each mount returns {destroy()}.
 */
window.PTZ = (function () {
  const POLL_IDLE_MS   = 5000;
  const POLL_MOVING_MS = 400;
  const cams = {};          // base -> {state, subs:Set, timer, lastView}

  // ── Shared state per camera ────────────────────────────────────────────────

  function cam(base) {
    if (!cams[base]) cams[base] = { state: null, subs: new Set(), timer: null, lastView: null };
    return cams[base];
  }

  async function api(base, path, method, body) {
    const opts = { method: method || 'GET', headers: {} };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    const r = await fetch(base + '/api/ptz' + path, opts);
    let data = null;
    try { data = await r.json(); } catch (_) {}
    if (!r.ok) throw new Error((data && data.error) || ('HTTP ' + r.status));
    return data;
  }

  function publish(base, state) {
    const c = cam(base);
    c.state = state;
    if (state && state.active) c.lastView = state.active;
    c.subs.forEach(fn => fn(state));
    schedule(base);
  }

  function schedule(base) {
    const c = cam(base);
    clearTimeout(c.timer);
    if (!c.subs.size || (c.state && c.state.enabled === false)) return;
    const ms = c.state && c.state.moving ? POLL_MOVING_MS : POLL_IDLE_MS;
    c.timer = setTimeout(() => refresh(base), document.hidden ? Math.max(ms, 15000) : ms);
  }

  async function refresh(base) {
    try {
      publish(base, await api(base, ''));
    } catch (_) {
      const c = cam(base);
      if (!c.state) publish(base, { enabled: false });   // no rig / older camera code
      else schedule(base);
    }
  }

  function subscribe(base, fn) {
    const c = cam(base);
    c.subs.add(fn);
    if (c.state) fn(c.state);
    if (c.subs.size === 1) refresh(base);
    return () => { c.subs.delete(fn); if (!c.subs.size) clearTimeout(c.timer); };
  }

  async function act(base, path, method, body) {
    const data = await api(base, path, method, body);
    if (data && data.state) publish(base, data.state);
    return data;
  }

  async function goView(base, id) {
    const c = cam(base);
    if (c.state && c.state.timelapse) {
      const ok = await dialog({ title: 'Timelapse running',
        text: 'Moving the camera will change the timelapse shot. Move anyway?', ok: 'Move' });
      if (!ok) return;
    }
    c.lastView = id;
    try { await act(base, '/goto', 'POST', { id }); }
    catch (e) { flash(base, e.message); }
  }

  // Errors are shown by panels on the same camera; chips have no text area.
  function flash(base, msg) {
    const c = cam(base);
    if (c.state) publish(base, Object.assign({}, c.state, { error: msg }));
  }

  // ── Small modal (rename / new / confirm) ───────────────────────────────────

  function dialog({ title, text, input, ok }) {
    return new Promise(resolve => {
      const back = document.createElement('div');
      back.className = 'ptz-dialog-back';
      back.innerHTML =
        '<div class="ptz-dialog" role="dialog" aria-modal="true">' +
          '<h3></h3>' + (text ? '<p></p>' : '') +
          (input !== undefined ? '<input type="text" maxlength="40" autocomplete="off">' : '') +
          '<div class="ptz-dialog-btns">' +
            '<button class="ptz-btn" data-r="0">Cancel</button>' +
            '<button class="ptz-btn primary" data-r="1"></button>' +
          '</div></div>';
      back.querySelector('h3').textContent = title;
      if (text) back.querySelector('p').textContent = text;
      back.querySelector('[data-r="1"]').textContent = ok || 'OK';
      const inp = back.querySelector('input');
      if (inp) inp.value = input;
      const done = v => { back.remove(); resolve(v); };
      back.addEventListener('click', e => {
        if (e.target === back) return done(null);
        const r = e.target.closest('[data-r]');
        if (!r) return;
        if (r.dataset.r === '0') return done(null);
        done(inp ? inp.value.trim() || null : true);
      });
      if (inp) inp.addEventListener('keydown', e => {
        if (e.key === 'Enter') done(inp.value.trim() || null);
        if (e.key === 'Escape') done(null);
      });
      document.body.appendChild(back);
      if (inp) setTimeout(() => { inp.focus(); inp.select(); }, 30);
    });
  }

  function esc(s) {
    return String(s).replace(/[&<>"']/g, ch =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
  }

  function stop(e) { e.stopPropagation(); }

  // ── Chips ──────────────────────────────────────────────────────────────────

  function chips(el, base, opts) {
    opts = opts || {};
    const box = document.createElement('div');
    box.className = 'ptz-chips' + (opts.big ? ' big' : '');
    box.hidden = true;
    el.appendChild(box);
    ['click', 'touchstart', 'mousedown'].forEach(ev => box.addEventListener(ev, stop));
    box.addEventListener('click', e => {
      const b = e.target.closest('[data-view]');
      if (b) goView(base, b.dataset.view);
    });
    let sig = '';
    const unsub = subscribe(base, s => {
      if (!s || !s.enabled || !s.views || !s.views.length) { box.hidden = true; return; }
      box.hidden = false;
      const next = JSON.stringify([s.views.map(v => [v.id, v.name]), s.active, s.moving_to]);
      if (next === sig) return;
      sig = next;
      box.innerHTML = s.views.map(v => {
        const cls = v.id === s.moving_to ? ' is-going' : v.id === s.active ? ' is-here' : '';
        return '<button class="ptz-chip' + cls + '" data-view="' + esc(v.id) + '">' +
               '<span class="ptz-dot"></span>' + esc(v.name) + '</button>';
      }).join('');
    });
    return { destroy() { unsub(); box.remove(); } };
  }

  // ── Panel ──────────────────────────────────────────────────────────────────

  const ARROW = {
    up:    '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 10 8 5 13 10"/></svg>',
    down:  '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 8 11 13 6"/></svg>',
    left:  '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="10 3 5 8 10 13"/></svg>',
    right: '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 3 11 8 6 13"/></svg>',
  };

  function panel(el, base) {
    const root = document.createElement('div');
    root.className = 'ptz-panel';
    root.hidden = true;
    root.innerHTML =
      '<div class="ptz-head">' +
        '<span class="ptz-title">Camera views</span>' +
        '<span class="ptz-status"></span>' +
        '<button class="ptz-edit-btn" data-act="edit">Edit</button>' +
      '</div>' +
      '<div class="ptz-grid"></div>' +
      '<div class="ptz-tune">' +
        '<div class="ptz-pad">' +
          ['up', 'left', 'right', 'down'].map(d =>
            '<button class="ptz-arrow ' + d + '" data-nudge="' + d + '" aria-label="Move ' + d + '">' + ARROW[d] + '</button>'
          ).join('') +
          '<span class="ptz-pad-mid">aim</span>' +
        '</div>' +
        '<div class="ptz-tune-side">' +
          '<div class="ptz-hint">Tap a view to point the camera there. Use the arrows to fine-tune.</div>' +
          '<div class="ptz-save-row"></div>' +
        '</div>' +
      '</div>';
    el.appendChild(root);
    ['click', 'touchstart', 'mousedown'].forEach(ev => root.addEventListener(ev, stop));

    const grid = root.querySelector('.ptz-grid');
    const status = root.querySelector('.ptz-status');
    const hint = root.querySelector('.ptz-hint');
    const saveRow = root.querySelector('.ptz-save-row');
    let editing = false;
    let nudged = false;        // arrows used since the last view was chosen (only user actions
                               // reset it: a poll answer already in flight must not)
    let cardSig = '';
    let state = null;

    function viewName(id) {
      const v = state && state.views.find(x => x.id === id);
      return v ? v.name : null;
    }

    function renderCards(s) {
      const sig = JSON.stringify([s.views, s.active, s.moving_to, editing]);
      if (sig === cardSig) return;
      cardSig = sig;
      grid.innerHTML = s.views.map(v => {
        const going = v.id === s.moving_to, here = !going && v.id === s.active;
        const cls = going ? ' is-going' : here ? ' is-here' : '';
        const tag = going ? '<span class="ptz-tag">Moving</span>' : here ? '<span class="ptz-tag">Live</span>' : '';
        const img = v.thumb
          ? ' style="background-image:url(\'' + base + '/api/ptz/thumb/' + encodeURIComponent(v.id) + '.jpg?v=' + v.thumb + '\')"'
          : '';
        return '<div class="ptz-card' + cls + '" data-view="' + esc(v.id) + '" role="button" tabindex="0">' +
                 '<div class="ptz-thumb"' + img + '>' + (v.thumb ? '' : 'no picture yet') + '</div>' +
                 '<div class="ptz-card-name">' + esc(v.name) + tag + '</div>' +
                 '<div class="ptz-card-tools">' +
                   '<button class="ptz-tool" data-act="aim" data-id="' + esc(v.id) + '" title="Point this view at where the camera is aimed now">Update</button>' +
                   '<button class="ptz-tool" data-act="rename" data-id="' + esc(v.id) + '">Rename</button>' +
                   '<button class="ptz-tool danger" data-act="delete" data-id="' + esc(v.id) + '" aria-label="Delete">✕</button>' +
                 '</div>' +
               '</div>';
      }).join('') +
      '<button class="ptz-add" data-act="add"><span>+</span>Save current aim as a view</button>';
    }

    function renderTune(s) {
      const last = cam(base).lastView;
      const lastName = viewName(last);
      root.querySelectorAll('[data-nudge]').forEach(b => {
        const d = b.dataset.nudge;
        b.disabled = (d === 'left' && s.pan <= -s.pan_limit + 0.5) ||
                     (d === 'right' && s.pan >= s.pan_limit - 0.5) ||
                     (d === 'down' && s.tilt <= s.tilt_limits[0] + 0.5) ||
                     (d === 'up' && s.tilt >= s.tilt_limits[1] - 0.5);
      });
      if (nudged && !s.active && !s.moving) {
        hint.textContent = lastName
          ? 'Like this better than "' + lastName + '"? Save it.'
          : 'Like this aim? Save it as a view.';
        saveRow.innerHTML =
          (lastName ? '<button class="ptz-btn primary" data-act="update" data-id="' + esc(last) + '">Update "' + esc(lastName) + '"</button>' : '') +
          '<button class="ptz-btn' + (lastName ? '' : ' primary') + '" data-act="add">Save as new view</button>';
      } else {
        hint.textContent = 'Tap a view to point the camera there. Use the arrows to fine-tune.';
        saveRow.innerHTML = '';
      }
    }

    function renderStatus(s) {
      status.className = 'ptz-status';
      if (s.error) {
        status.classList.add('err');
        status.textContent = s.error.indexOf('busy') >= 0 ? 'Camera busy, try again' : s.error;
      } else if (s.moving) {
        status.textContent = s.moving_to ? 'Moving to ' + (viewName(s.moving_to) || 'view') + '…' : 'Moving…';
      } else if (s.timelapse) {
        status.classList.add('warn');
        status.textContent = 'Timelapse running';
      } else {
        status.textContent = s.active ? '' : 'Custom aim';
      }
    }

    const unsub = subscribe(base, s => {
      if (!s || !s.enabled || !s.views) { root.hidden = true; return; }
      state = s;
      root.hidden = false;
      renderCards(s);
      renderTune(s);
      renderStatus(s);
    });

    async function run(fn) {
      try { await fn(); } catch (e) { flash(base, e.message); }
    }

    root.addEventListener('click', async e => {
      const n = e.target.closest('[data-nudge]');
      if (n) {
        const s = state, d = n.dataset.nudge;
        nudged = true;
        const dp = d === 'left' ? -s.nudge.pan : d === 'right' ? s.nudge.pan : 0;
        const dt = d === 'down' ? -s.nudge.tilt : d === 'up' ? s.nudge.tilt : 0;
        return run(() => act(base, '/nudge', 'POST', { pan: dp, tilt: dt }));
      }
      const a = e.target.closest('[data-act]');
      if (a) {
        const id = a.dataset.id;
        switch (a.dataset.act) {
          case 'edit':
            editing = !editing;
            root.classList.toggle('editing', editing);
            a.classList.toggle('on', editing);
            a.textContent = editing ? 'Done' : 'Edit';
            cardSig = '';
            if (state) renderCards(state);
            return;
          case 'add': {
            const name = await dialog({ title: 'New view', text: 'Name this view so anyone can tap it later.',
                                        input: '', ok: 'Save' });
            if (!name) return;
            nudged = false;
            return run(async () => {
              const d = await act(base, '/views', 'POST', { name });
              if (d && d.result) cam(base).lastView = d.result.id;
            });
          }
          case 'update':
          case 'aim':
            nudged = false;
            return run(() => act(base, '/views/' + encodeURIComponent(id), 'POST', { save_view: true }));
          case 'rename': {
            const name = await dialog({ title: 'Rename view', input: viewName(id) || '', ok: 'Rename' });
            if (name) return run(() => act(base, '/views/' + encodeURIComponent(id), 'POST', { name }));
            return;
          }
          case 'delete': {
            const ok = await dialog({ title: 'Delete "' + viewName(id) + '"?',
                                      text: 'The camera stays where it is; only the saved view goes.', ok: 'Delete' });
            if (ok) return run(() => act(base, '/views/' + encodeURIComponent(id), 'DELETE'));
            return;
          }
        }
        return;
      }
      const card = e.target.closest('.ptz-card');
      if (card && !editing) {
        nudged = false;
        goView(base, card.dataset.view);
      }
    });
    root.addEventListener('keydown', e => {
      const card = e.target.closest('.ptz-card');
      if (card && !editing && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); goView(base, card.dataset.view); }
    });

    return { destroy() { unsub(); root.remove(); } };
  }

  return { chips, panel, refresh };
})();
