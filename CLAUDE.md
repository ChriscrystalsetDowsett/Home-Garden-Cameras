# CLAUDE.md — Fleet Ground Truth
**Last verified:** 2026-06-17
**Verified from:** live service files, cloudflared configs, and crontabs — not from documentation

> Read this entire file before planning or changing anything. Do not ask Chris what is running where. Look it up here or via SSH.

## Mandatory pre-work checklist
Before proposing any plan or making any change, you must:
- [ ] Confirm the target Pi's live app path matches this document (cat the service file)
- [ ] Confirm the correct Cloudflare hostname for any externally-facing change
- [ ] Check git status on bigpc and the target Pi before touching anything
- [ ] Show a diff before applying any file change
- [ ] Never assume settings.yaml content — read it first (it is gitignored and differs per Pi)

## Fleet inventory

| Pi | LAN IP | Tailscale IP | Connection | Role |
|---|---|---|---|---|
| pi3 | 192.168.4.127 (wlan0) / 192.168.4.206 (eth0) | 100.121.172.30 | WiFi (eth0 available) | Camera node — currently unreachable on LAN, accessible via Tailscale relay only |
| pi4 | 192.168.4.186 | 100.110.247.12 | WiFi only (eth0 NO-CARRIER) | Greenhouse camera + sensor/relay node |
| pi5 | 192.168.4.132 (eth0) | 100.94.38.51 | Ethernet + WiFi | Primary dev/commit node + camera dashboard |
| bigpc | 192.168.4.138 | 100.75.224.125 | Ethernet | AI pipeline, journal app, Ollama |

## Live app paths (authoritative — from service files)

| Pi | Service | Live path | Port |
|---|---|---|---|
| pi3 | camera-stream | /home/chris/dev/apps/home-garden-cameras (unverified — pi3 unreachable via SSH) | 5000 |
| pi4 | garden-monitor | /home/chris/dev/apps/home-garden-cameras | 8080 |
| pi4 | greenhouse-monitor | /home/chris/dev/apps/greenhouse-monitor | 5000 |
| pi4 | motor-control | /home/chris/dev/apps/camera-pan-tilt | 5001 |
| pi5 | garden-monitor | /home/chris/dev/apps/home-garden-cameras | 8080 |

**Critical:** All documentation previously said /home/chris/pi-apps/… — this is wrong everywhere. The correct path on every Pi is /home/chris/dev/apps/…

## Cloudflare public routing (authoritative — from /etc/cloudflared/config.yml on each Pi)

| Public URL | Terminates at | Pi | Service |
|---|---|---|---|
| greenhouse.chrisdowsett.com | pi4:5000 | pi4 | greenhouse-monitor (sensor dashboard + relay control) |
| cam.chrisdowsett.com | pi4:8080 | pi4 | garden-monitor (C930e USB camera) |
| cameras.chrisdowsett.com | pi5:8080 | pi5 | garden-monitor (IMX708 CSI camera + multi-camera dashboard + greenhouse proxy at /greenhouse) |
| greenhousejournal.chrisdowsett.com | bigpc:5000 | bigpc | greenhouse-analysis (AI journal) |
| media.chrisdowsett.com | bigpc:5001 | bigpc | bigpc-dashboard |
| chat.chrisdowsett.com | bigpc:3000 | bigpc | Claude Code gateway |
| comfyui.chrisdowsett.com | bigpc:8188 | bigpc | ComfyUI |

**Critical:** cam.chrisdowsett.com is pi4, not pi5. Pi5's public hostname is cameras.chrisdowsett.com. The greenhouse proxy page deployed to pi5 is at cameras.chrisdowsett.com/greenhouse.

## Services per Pi

### pi4
| Service | Port | Purpose |
|---|---|---|
| garden-monitor | 8080 | C930e USB camera stream + snapshots |
| greenhouse-monitor | 5000 | DHT11 sensor + 4-relay automation + web UI |
| motor-control | 5001 | Camera pan/tilt PWM (not externally exposed) |
| pigpiod | — | GPIO daemon required by greenhouse-monitor and motor-control |
| cloudflared | — | Tunnels: greenhouse.chrisdowsett.com + cam.chrisdowsett.com |

### pi5
| Service | Port | Purpose |
|---|---|---|
| garden-monitor | 8080 | IMX708 CSI camera + multi-camera dashboard + greenhouse proxy |
| cloudflared | — | Tunnel: cameras.chrisdowsett.com |

### pi3
| Service | Port | Purpose |
|---|---|---|
| camera-stream | 5000 | Pi camera node (LAN only — no cloudflared) |

## Cron schedules (authoritative — from live crontabs)

### pi4
| Time | Job |
|---|---|
| Every 6 min, 07:00–19:00 | POST /api/snapshot label=GH quality=95 → greenhouse stills (~120/day) |
| Every 5 min | watchdog-garden-monitor.sh + watchdog-greenhouse-monitor.sh |
| 02:15 daily | /home/chris/dev/apps/home-garden-cameras/scripts/sync-to-bigpc.sh |

### pi5
| Time | Job |
|---|---|
| 02:30 daily | /home/chris/sync-to-bigpc.sh |

## Known traps — read before every session

1. **settings.yaml is gitignored on every Pi.** It never travels through GitHub. Any settings change must be applied manually on each Pi after git pull. Always read the live settings.yaml before assuming its contents: `ssh <pi> "cat /home/chris/dev/apps/home-garden-cameras/config/settings.yaml"`

2. **Ollama on bigpc binds to 172.17.0.1:11434, not localhost.** Any script calling Ollama must use http://172.17.0.1:11434 — localhost will fail silently.

3. **pi3 is currently unreachable on LAN.** SSH via Tailscale (100.121.172.30) only. Its WiFi (wlan0) bounces continuously — a watchdog reconnects it but it may be down at any moment.

4. **pi4 is WiFi only.** eth0 shows NO-CARRIER. A WiFi dropout takes greenhouse-monitor, garden-monitor, motor-control, and both Cloudflare tunnels offline simultaneously.

5. **cam.chrisdowsett.com is pi4, not pi5.** Pi5's public URL is cameras.chrisdowsett.com. Confusing these breaks external routing assumptions.

6. **The greenhouse proxy page is at cameras.chrisdowsett.com/greenhouse.** greenhouse.chrisdowsett.com still routes directly to pi4:5000 until Cloudflare is updated.

7. **git pull on pi4 or pi3 may reveal local uncommitted changes.** Always run git status on the target Pi before pulling. The shutter speed feature on pi5 was nearly lost this way.

8. **Pi5 is the fleet commit node.** pi3 and pi4 are pull-only. Never commit or push from pi3 or pi4.

---

# Home Garden Cameras — Claude Code Instructions

## Project layout

```
home-garden-cameras/
  app/           Flask application package
    app.py       All HTTP routes
    camera.py    Camera backend (picamera2 / V4L2)
    config.py    Loads settings.yaml — single source of truth for all constants
    dashboard.py Dashboard blueprint + proxy routes
    recorder.py  VideoRecorder, AudioStreamer classes
    servo.py     ServoController (stub — no GPIO touched until SERVO_ENABLED=true)
    timelapse.py TimelapseCapturer
    scheduler.py Camera on/off schedule
    stats.py     Pi system info
    film.py      Film filter effects
    postprocess.py  Post-capture image processing
  static/
    index.css / index.js        Solo camera page
    dashboard.css / dashboard.js  Multi-camera dashboard
  templates/
    index.html         Solo camera page
    dashboard.html     Multi-camera grid + drawer
    partials/          Jinja2 include fragments
  config/
    settings.yaml      All user-editable settings (never hardcode device config in Python)
  data/photos, data/videos   Runtime output — not in git
```

---

## Deployment model

- **Pi 5** is the primary development and commit machine. All code changes are tested
  here and pushed to GitHub from here.
- **Pi 3 and Pi 4** are camera nodes. They pull from GitHub to get updates — never
  push code directly from them.
- After pushing from pi5: SSH into each camera Pi and run
  `git pull && sudo systemctl restart <service>`.
- **`/etc/systemd/system/<service>.service` is NOT in git.** Changes to it (e.g. new
  `Environment=` lines) must be applied manually on every Pi that needs them.

---

## Restart rules — know what requires what

Service name: **garden-monitor** (pi4 and pi5); **camera-stream** (pi3) — see Services table above

| What changed | Action required |
|---|---|
| Any `.py` file | `sudo systemctl restart garden-monitor` |
| Any `templates/*.html` | `sudo systemctl restart garden-monitor` (Flask caches templates in production) |
| `config/settings.yaml` | `sudo systemctl restart garden-monitor` (config is loaded at import time) |
| `static/*.css` or `static/*.js` | Bump `?v=N` in the HTML `<link>`/`<script>` tag **and** restart so iOS Safari sees the new HTML |
| `/etc/systemd/system/garden-monitor.service` | `sudo systemctl daemon-reload && sudo systemctl restart garden-monitor` |

**If a restart appears to do nothing**, check for a stale process holding port 8080:

```bash
sudo fuser 8080/tcp        # shows PID if something is squatting on the port
sudo fuser -k 8080/tcp     # kills it; then restart normally
```

---

## Static asset cache-busting

Every `<link>` and `<script>` in the HTML templates uses a `?v=N` query string:

```html
<link rel="stylesheet" href="/static/index.css?v=5">
<script src="/static/index.js?v=3"></script>
```

**Rules:**
- Bump the version any time the file content changes.
- Bumping the static file version alone is not enough — iOS Safari caches the *HTML page* itself. The Flask routes for `index.html` and `dashboard.html` already return `Cache-Control: no-store` via `make_response()`. Do not remove those headers.
- Keep CSS and JS versions in sync with what the HTML references. An out-of-sync version means users run old JS against new CSS or vice-versa.

---

## Python / backend

### Config
- All runtime constants live in `app/config.py`, which reads `config/settings.yaml` once at startup.
- Never hardcode device-specific values (pins, IPs, resolution, credentials) in Python. They belong in `settings.yaml`.
- Adding a new setting: add it to `settings.yaml` with a comment, then expose it as a constant in `config.py`.

### Flask routes
- HTML routes (`index.html`, `dashboard.html`) must use `make_response()` and set `Cache-Control: no-store, no-cache, must-revalidate`. This is already in place — do not remove it.
- API routes return `jsonify()`. Never return raw strings for structured data.
- The dashboard is a separate Blueprint in `dashboard.py`. Camera-Pi proxy calls go through `/dashboard/cam/<idx>/proxy/<path>`.

### Threading
- The camera runs in a background thread. Use `cam_ctrl_lock` (a `threading.Lock`) when reading or writing `cam_ctrl`.
- `VideoRecorder` and `ServoController` are also thread-safe via their own locks. Follow the same pattern for any new shared state.
- Never sleep in a Flask route. Long-running work belongs in a background thread.

### Audio streaming
- The `AudioStreamer.subscribe_raw()` generator spawns an ffmpeg subprocess using PulseAudio (`-f pulse`).
- **Critical:** The systemd service must have `Environment=XDG_RUNTIME_DIR=/run/user/1000` or ffmpeg cannot reach PipeWire and the stream silently produces 0 bytes. Verify with: `curl --max-time 2 http://localhost:8080/api/audio/stream/raw | wc -c` — a working stream produces tens of thousands of bytes.
- To find the correct PulseAudio source name: `pactl list sources short`.

### Camera backend
- `CAM_BACKEND` in config selects `"picamera2"` (CSI ribbon cable) or `"v4l2"` (USB webcam).
- The V4L2 backend tries MJPEG fourcc first, then falls back to YUYV. USB cameras sometimes drop MJPEG after a reconnect — do not revert this fallback.
- If the camera stops producing frames after a USB reconnect, a physical replug may be needed; ffmpeg/OpenCV cannot always recover without it.

### Servo
- `SERVO_ENABLED = false` in `settings.yaml` by default. In stub mode all commands are logged and no GPIO is touched. Do not enable without hardware connected.

---

## JavaScript

### Strict rules
- All JS files begin with `'use strict';`.
- No `var` — use `const` or `let`.
- Async functions that fetch from the API must have a `try/catch` that updates UI on error (toast or offline message). Never swallow errors silently in user-facing paths.

### iOS Safari audio
- `AudioContext` must be **created synchronously inside a user gesture handler** (the click/touch callback), not inside an `async` function that was called from one. iOS invalidates the gesture context across `await` boundaries.
- Check for fetch-based streaming support with `typeof ReadableStream !== 'undefined' && typeof ReadableStream.prototype.getReader === 'function'` before using the raw PCM path. The `<audio>` element fallback uses `/api/audio/stream` — that route must exist if you add it.
- Use `window.AudioContext || window.webkitAudioContext` for the constructor.

### iOS Safari fullscreen
- `requestFullscreen()` / `webkitRequestFullscreen()` **do not work on arbitrary `<div>` elements in iOS Safari** — only on `<video>`. Check by testing `el.requestFullscreen || el.webkitRequestFullscreen` on the target div.
- Fake-fullscreen pattern: toggle a class on `<body>` (e.g. `body.ios-fs`) and use CSS to pin the element to the viewport with `position: fixed; inset: 0`.
- **CSS `transform` trap:** any element with a CSS `transform` (including `transform: translateY(0)`) becomes the containing block for `position: fixed` descendants. A `position: fixed` child of a transformed parent is trapped inside the parent, not the viewport. To avoid this, expand the *parent itself* to full-screen rather than making its child fixed.

### iOS Safari MJPEG
- Safari does not support MJPEG streams (`multipart/x-mixed-replace`). The camera feed uses a pull-based loop: fetch a JPEG from `/api/frame?t=<timestamp>`, display it, then schedule the next fetch after a timeout. Do not replace this with a native MJPEG `<img src>`.

### Responsive sizing for phones/tablets
- Use `clamp(min, preferred, max)` for all touch targets and text that must scale across phone/tablet/desktop.
- Use `vmin` (the shorter viewport dimension) for circular controls that must fit in both portrait and landscape — `vw` equals 844px in landscape on an iPhone 14, making buttons far too large.
- Use `100dvh` / `100dvw` (dynamic viewport units) for full-screen elements to account for iOS Safari's collapsible browser chrome.
- Add `@media (orientation: portrait)` rules when elements that are comfortably separated in landscape overlap in portrait.

---

## CSS conventions

- All colour and radius tokens are CSS custom properties defined in `:root` in both stylesheets. Extend the palette there; never hardcode hex values in component rules.
- Theme: dark green (`--bg: #070d07`, `--green: #22c55e`). Match this in any new UI.
- Overlay buttons on the camera stream use the shared pattern: `position: absolute`, `background: rgba(0,0,0,0.6)`, `backdrop-filter: blur(8px)`, `border: 1.5px solid rgba(255,255,255,0.16)`, `border-radius: 999px`. Reuse these values for new overlay controls.
- `z-index` ladder: drawer backdrop 200 → drawer 201 → fake-fullscreen content 500+ → toast 300.

---

## Things that waste time — do not repeat these mistakes

1. **Forgetting to restart after editing a template.** Flask production mode caches templates. The change will not appear until the service restarts, no matter how many refreshes you do.

2. **Bumping the `?v=N` on a static file but not restarting.** iOS Safari will still serve the old HTML (with the old version string) from its cache until the server sends `Cache-Control: no-store` on the HTML itself. The restart forces a new HTML response.

3. **Not checking for a stale process on the port.** `systemctl restart` can appear to succeed (`is-active` says `active`) while an old PID is still holding the port. Always confirm with `sudo fuser <port>/tcp` if behaviour doesn't change after a restart.

4. **Using `position: fixed` on a child of a transformed parent.** The element will appear to be fixed but is actually positioned relative to its transformed ancestor. Expand the ancestor to fill the screen instead.

5. **Forgetting `XDG_RUNTIME_DIR` in the service file.** Audio streaming silently produces 0 bytes. Test immediately after adding audio features: `curl --max-time 2 http://localhost:8080/api/audio/stream/raw | wc -c`.

6. **Hardcoding a device-specific value in Python.** Camera IP addresses, GPIO pins, resolution, credentials — all belong in `settings.yaml`. Python reads them from `config.py` constants.

7. **Accessing a DOM element that was moved or removed.** After any HTML refactor, grep the JS for `getElementById`/`querySelector` calls that reference the old IDs. A null-dereference here throws silently in some browsers and breaks everything downstream in the same function.

8. **Using `vw` instead of `vmin` for circular overlay buttons.** In landscape on an iPhone, `vw` equals the long dimension (~844px), so a `10vw` button hits 84px. `vmin` uses the short dimension (~390px) and stays proportional in both orientations.

9. **Touching GPIO with `SERVO_ENABLED = false`.** The servo stub is intentionally a no-op. Do not add direct `RPi.GPIO` or `pigpio` calls outside `servo.py`.

10. **Pushing to GitHub instead of testing locally first.** The other Pis pull from `main`. A broken push breaks all camera nodes. Always verify the service starts and the feed loads on the Pi 5 before pushing.
