"""Dashboard blueprint — multi-camera overview, API proxy, and full UI proxy."""
import hmac
import re as _re
import time as _time
import requests as _requests
import cv2 as _cv2
import numpy as _np
from functools import wraps
from flask import Blueprint, Response, make_response, jsonify, render_template, request, \
                  session, redirect, stream_with_context

from .config import CAMERAS, TILE_QUALITY, DASHBOARD_PASSWORD

dashboard = Blueprint("dashboard", __name__)

# ── Authentication ─────────────────────────────────────────────────────────────

_LOGIN_PAGE = """\
<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>Garden Cams — Login</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
*{-webkit-tap-highlight-color:transparent}
:root{
  --bg:#070d07;--surface:#0d180d;--surface2:#132013;--border:#1a2e1a;
  --green:#22c55e;--green-dk:#166534;--green-md:#16a34a;--green-lt:#4ade80;
  --text:#edf6ed;--muted:#87a887;--dim:#4a6b4a;--red:#ef4444;--radius:12px;
}
body{
  background:var(--bg);
  background-image:radial-gradient(ellipse 80% 40% at 50% -5%,rgba(34,197,94,.07) 0%,transparent 70%);
  color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  min-height:100vh;display:flex;align-items:center;justify-content:center;
}
.card{
  background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  padding:36px 32px 32px;width:100%;max-width:360px;
  box-shadow:0 8px 32px rgba(0,0,0,.5);
}
.logo{
  width:52px;height:52px;background:var(--green-dk);border-radius:50%;
  display:flex;align-items:center;justify-content:center;font-size:24px;
  margin:0 auto 20px;
}
h1{font-size:18px;font-weight:700;color:var(--green-lt);text-align:center;margin-bottom:4px;}
p{font-size:12px;color:var(--muted);text-align:center;margin-bottom:24px;}
label{display:block;font-size:11px;font-weight:600;letter-spacing:.06em;
  text-transform:uppercase;color:var(--dim);margin-bottom:6px;}
input[type=password]{
  width:100%;padding:10px 14px;
  background:var(--surface2);border:1px solid var(--border);border-radius:8px;
  color:var(--text);font-size:14px;outline:none;
  transition:border-color .15s;
}
input[type=password]:focus{border-color:var(--green-md);}
.err{
  font-size:12px;color:var(--red);background:rgba(239,68,68,.08);
  border:1px solid rgba(239,68,68,.25);border-radius:7px;
  padding:8px 12px;margin-bottom:14px;
}
button{
  margin-top:16px;width:100%;padding:11px;
  background:linear-gradient(135deg,var(--green-dk),var(--green-md));
  color:#fff;border:none;border-radius:9px;
  font-size:14px;font-weight:600;cursor:pointer;
  transition:opacity .15s;
}
button:hover{opacity:.88;}
</style>
</head><body>
<div class="card">
  <div class="logo">🌿</div>
  <h1>Garden Cameras</h1>
  <p>Enter the dashboard password to continue</p>
  {error}
  <form method="post">
    <label for="pw">Password</label>
    <input type="password" id="pw" name="password" autofocus autocomplete="current-password">
    <button type="submit">Sign in</button>
  </form>
</div>
</body></html>"""


def _authed():
    """Return True if the current session is authenticated (or no password set)."""
    if not DASHBOARD_PASSWORD:
        return True
    return session.get("dashboard_authed") is True


def _require_auth(f):
    @wraps(f)
    def _wrapped(*args, **kwargs):
        if not _authed():
            return redirect("/dashboard/login")
        return f(*args, **kwargs)
    return _wrapped


@dashboard.route("/dashboard/login", methods=["GET", "POST"])
def login():
    if not DASHBOARD_PASSWORD:
        return redirect("/dashboard")
    error = ""
    if request.method == "POST":
        pw = request.form.get("password", "")
        if hmac.compare_digest(pw, DASHBOARD_PASSWORD):
            session["dashboard_authed"] = True
            return redirect("/dashboard")
        error = '<div class="err">Incorrect password — try again.</div>'
    return _LOGIN_PAGE.replace("{error}", error), 200, {"Content-Type": "text/html; charset=utf-8"}


@dashboard.route("/dashboard/logout")
def logout():
    session.pop("dashboard_authed", None)
    return redirect("/dashboard/login")


_API_TIMEOUT    = 8    # seconds for regular JSON API calls
_STREAM_CONNECT = 10   # seconds to establish a frame connection

# (fps, jpeg_quality, resize_width) per quality level
# resize_width > 0 shrinks the frame on the dashboard server before forwarding,
# cutting bandwidth over the two-hop proxy path (Pi → dashboard → browser).
# 0 = full resolution (no resize).
_QUALITY = {
    "low":    (5,  70, 480),
    "medium": (12, 75, 640),
    "high":   (24, 85,   0),
}


def _cam_base(idx):
    cam = CAMERAS[idx]
    return f"http://{cam['host']}:{cam['port']}"


# ── Dashboard grid ─────────────────────────────────────────────────────────────

@dashboard.route("/dashboard")
@_require_auth
def index():
    hero_idx = next((i for i, c in enumerate(CAMERAS) if c.get("hero")), 0)
    resp = make_response(render_template("dashboard.html", cameras=CAMERAS,
                                         tile_quality=TILE_QUALITY, hero_idx=hero_idx))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


@dashboard.route("/dashboard/v1")
@_require_auth
def index_v1():
    """Archived pre-redesign dashboard, kept for before/after reference."""
    resp = make_response(render_template("dashboard_v1.html", cameras=CAMERAS, tile_quality=TILE_QUALITY))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


# ── Quick-action API proxy (used by dashboard JS) ──────────────────────────────

@dashboard.route(
    "/dashboard/cam/<int:idx>/proxy/<path:api_path>",
    methods=["GET", "POST", "DELETE"],
)
@_require_auth
def cam_proxy(idx, api_path):
    """Proxy API + stream calls from the dashboard grid to a camera Pi."""
    if idx < 0 or idx >= len(CAMERAS):
        return jsonify({"error": "invalid camera index"}), 404
    return _proxy(idx, api_path)


# ── Full camera UI ─────────────────────────────────────────────────────────────

@dashboard.route("/camera/<int:idx>", methods=["GET"])
@dashboard.route("/camera/<int:idx>/", methods=["GET"])
@_require_auth
def camera_full(idx):
    """
    Serve the camera Pi's full UI with all URLs rewritten to pass through
    this server, so the page works from any domain (local or public).
    """
    if idx < 0 or idx >= len(CAMERAS):
        return jsonify({"error": "camera not found"}), 404

    try:
        resp = _requests.get(_cam_base(idx) + "/", timeout=_API_TIMEOUT)
    except (_requests.exceptions.ConnectionError, _requests.exceptions.Timeout):
        return "Camera offline", 502

    prefix = f"/camera/{idx}"
    html   = resp.text

    # ── Rewrite root-relative src/href attributes in HTML ─────────────────────
    # Matches src="/..." and href="/..." but not src="//cdn..." or src="data:..."
    html = _re.sub(
        r'((?:src|href)=")(/[^/"#][^"]*)"',
        lambda m: f'{m.group(1)}{prefix}{m.group(2)}"',
        html,
    )

    # ── Inject JS shims before </head> ────────────────────────────────────────
    # Rewrites root-relative URLs at runtime for:
    #   - fetch() API calls
    #   - img.src / anchor.href direct property assignments
    #   - element.setAttribute('src', ...) calls
    shim = f"""<script>
(function() {{
  var px = '{prefix}';
  function _rw(v) {{
    return (typeof v === 'string' && v.charAt(0) === '/' && v.indexOf(px) !== 0)
      ? px + v : v;
  }}

  /* fetch() */
  var _f = window.fetch;
  window.fetch = function(u, o) {{ return _f.call(this, _rw(u), o); }};

  /* img.src and similar direct property assignments */
  ['src', 'href'].forEach(function(prop) {{
    var targets = [HTMLImageElement, HTMLAnchorElement, HTMLSourceElement];
    targets.forEach(function(Ctor) {{
      var d = Object.getOwnPropertyDescriptor(Ctor.prototype, prop);
      if (d && d.set) {{
        Object.defineProperty(Ctor.prototype, prop, {{
          set: function(v) {{ d.set.call(this, _rw(v)); }},
          get: d.get,
          configurable: true,
        }});
      }}
    }});
  }});

  /* element.setAttribute('src'/'href', ...) */
  var _sa = Element.prototype.setAttribute;
  Element.prototype.setAttribute = function(n, v) {{
    if (n === 'src' || n === 'href') v = _rw(v);
    return _sa.call(this, n, v);
  }};
}})();
</script>"""
    html = html.replace("</head>", shim + "\n</head>", 1)

    return Response(html, content_type="text/html; charset=utf-8")


@dashboard.route("/camera/<int:idx>/<path:path>", methods=["GET", "POST", "DELETE"])
@_require_auth
def camera_proxy(idx, path):
    """Proxy all sub-requests (stream, API, snapshots, videos) for the full UI."""
    if idx < 0 or idx >= len(CAMERAS):
        return jsonify({"error": "camera not found"}), 404
    return _proxy(idx, path)


# ── Shared proxy helper ────────────────────────────────────────────────────────

def _proxy(idx, path):
    """
    Forward a request to the target camera Pi.
    MJPEG streams are chunked without buffering; everything else is buffered.
    """
    url = f"{_cam_base(idx)}/{path}"

    fwd_headers = {}
    if request.content_type:
        fwd_headers["Content-Type"] = request.content_type

    # Pull-based MJPEG stream — fetches the latest frame on each tick.
    # This avoids frame-queue buildup that causes lag over the internet.
    # Re-encoding at lower quality happens here so camera Pis stay stateless.
    if path == "stream":
        quality          = request.args.get("quality", "medium")
        fps, q, resize_w = _QUALITY.get(quality, _QUALITY["medium"])
        min_delay        = 1.0 / fps
        frame_url        = f"{_cam_base(idx)}/api/frame"

        def _encode(data):
            """Re-encode JPEG at target quality and/or resize on the dashboard server."""
            needs_resize   = resize_w > 0
            needs_reencode = q < 83
            if not needs_resize and not needs_reencode:
                return data
            arr = _np.frombuffer(data, dtype=_np.uint8)
            img = _cv2.imdecode(arr, _cv2.IMREAD_COLOR)
            if img is None:
                return data
            if needs_resize and img.shape[1] > resize_w:
                h = round(img.shape[0] * resize_w / img.shape[1])
                img = _cv2.resize(img, (resize_w, h), interpolation=_cv2.INTER_AREA)
            ok, enc = _cv2.imencode(".jpg", img, [_cv2.IMWRITE_JPEG_QUALITY, q])
            return enc.tobytes() if ok else data

        def _generate():
            session = _requests.Session()
            try:
                err_count = 0
                while True:
                    t0 = _time.monotonic()
                    try:
                        r = session.get(frame_url, timeout=3)
                        if r.status_code == 200 and r.content:
                            yield (
                                b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                                + _encode(r.content) + b"\r\n"
                            )
                            err_count = 0
                        else:
                            err_count += 1
                            _time.sleep(min(err_count, 5) * 0.5)
                            continue
                    except _requests.exceptions.RequestException:
                        err_count += 1
                        _time.sleep(min(err_count, 5) * 0.5)
                        continue
                    elapsed = _time.monotonic() - t0
                    gap = min_delay - elapsed
                    if gap > 0:
                        _time.sleep(gap)
            except Exception:
                return
            finally:
                session.close()

        return Response(
            stream_with_context(_generate()),
            content_type="multipart/x-mixed-replace; boundary=frame",
            direct_passthrough=True,
            headers={"X-Accel-Buffering": "no"},
        )

    # Live audio streams — must be proxied as streaming responses, not buffered.
    if path in ("api/audio/stream", "api/audio/stream/raw"):
        upstream = f"{_cam_base(idx)}/{path}"
        is_raw   = path.endswith("/raw")
        ctype    = "application/octet-stream" if is_raw else "audio/ogg"
        chunk_sz = 512 if is_raw else 4096

        def _audio_gen():
            try:
                with _requests.get(
                    upstream, stream=True, timeout=(_STREAM_CONNECT, None),
                ) as r:
                    for chunk in r.iter_content(chunk_size=chunk_sz):
                        if chunk:
                            yield chunk
            except Exception:
                return
        return Response(
            stream_with_context(_audio_gen()),
            content_type=ctype,
            headers={"Cache-Control": "no-store, no-cache", "X-Accel-Buffering": "no"},
        )

    # Everything else — JSON API, snapshots, videos, static assets.
    try:
        resp = _requests.request(
            method=request.method,
            url=url,
            data=request.get_data(),
            headers=fwd_headers,
            timeout=_API_TIMEOUT,
        )
        return Response(
            resp.content,
            status=resp.status_code,
            content_type=resp.headers.get("Content-Type", "application/json"),
        )
    except _requests.exceptions.ConnectionError:
        return jsonify({"error": "offline"}), 502
    except _requests.exceptions.Timeout:
        return jsonify({"error": "timeout"}), 504
