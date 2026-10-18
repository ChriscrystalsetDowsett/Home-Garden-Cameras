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

from .config import CAMERAS, TILE_QUALITY, DASHBOARD_PASSWORD, API_TOKEN, DASHBOARD_LINKS
from . import access

dashboard = Blueprint("dashboard", __name__)

# ── Access: signed-in devices (see app/access.py) ──────────────────────────────

def _authed():
    """True when this browser is a signed-in device (or no password is set)."""
    return access.signed_in()


def _require_auth(f):
    @wraps(f)
    def _wrapped(*args, **kwargs):
        if not _authed():
            return redirect("/dashboard/login")
        return f(*args, **kwargs)
    return _wrapped


def _ago(ts):
    if not ts:
        return "never"
    s = max(0, int(_time.time() - ts))
    if s < 120:
        return "just now"
    if s < 7200:
        return f"{s // 60} min ago"
    if s < 172800:
        return f"{s // 3600} h ago"
    return _time.strftime("%-d %b %Y", _time.localtime(ts))


def _page(mode, **kw):
    resp = make_response(render_template("access.html", mode=mode, **kw))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@dashboard.route("/dashboard/login", methods=["GET", "POST"])
def login():
    if _authed():
        return redirect("/dashboard")
    suggested = access.device_name_from_ua(request.headers.get("User-Agent"))
    error = ""
    if request.method == "POST":
        pw = request.form.get("password", "")
        if hmac.compare_digest(pw.encode(), DASHBOARD_PASSWORD.encode()):
            access.sign_in(request.form.get("name"))
            return redirect("/dashboard")
        _time.sleep(1)          # slow down guessing
        error = "Incorrect password. Try again."
    return _page("login", error=error, suggested=suggested)


@dashboard.route("/dashboard/join/<code>", methods=["GET", "POST"])
def join(code):
    """Invite link: GET only shows the page; the POST (a tap) uses the invite."""
    if request.method == "POST":
        if access.redeem_invite(code, request.form.get("name")):
            return redirect("/dashboard")
        return _page("join", valid=False)
    inv = access._load()["invites"].get(code)
    return _page("join", valid=access.invite_valid(code), by=(inv or {}).get("by", "Someone"),
                 suggested=access.device_name_from_ua(request.headers.get("User-Agent")))


@dashboard.route("/dashboard/logout")
def logout():
    dev = access.current_device()
    if dev:
        access.sign_out(dev["id"])
    session.clear()
    return redirect("/dashboard/login")


@dashboard.route("/dashboard/devices")
@_require_auth
def devices():
    me = (access.current_device() or {}).get("id")
    devs = [dict(d, last_seen_txt=_ago(d.get("last_seen")),
                 created_txt=_time.strftime("%-d %b %Y", _time.localtime(d.get("created", 0))))
            for d in access.all_devices()]
    return _page("devices", devices=devs, me=me)


@dashboard.route("/dashboard/devices/<dev_id>/signout", methods=["POST"])
@_require_auth
def device_signout(dev_id):
    me = (access.current_device() or {}).get("id")
    access.sign_out(dev_id)
    if dev_id == me:
        session.clear()
        return redirect("/dashboard/login")
    return redirect("/dashboard/devices")


@dashboard.route("/dashboard/devices/signout-others", methods=["POST"])
@_require_auth
def device_signout_others():
    me = (access.current_device() or {}).get("id")
    for d in access.all_devices():
        if d["id"] != me:
            access.sign_out(d["id"])
    return redirect("/dashboard/devices")


@dashboard.route("/dashboard/devices/invite", methods=["POST"])
@_require_auth
def device_invite():
    me = access.current_device() or {}
    code, expires = access.create_invite(me.get("name", "Someone"))
    # Behind the tunnel the app sees plain http; the link must be the public https one.
    base = (f"https://{request.host}" if access.via_tunnel() else request.host_url.rstrip("/"))
    url = base + "/dashboard/join/" + code
    return jsonify({"ok": True, "url": url, "expires": expires, "qr_svg": _qr_svg(url)})


def _qr_svg(text):
    """QR code as inline SVG when the qrcode package is installed, else ''."""
    try:
        import qrcode
        import qrcode.image.svg
        img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=1)
        svg = img.to_string(encoding="unicode")
        return svg[svg.find("<svg"):]
    except Exception:
        return ""


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
                                         tile_quality=TILE_QUALITY, hero_idx=hero_idx,
                                         links=DASHBOARD_LINKS))
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

    # Forward a Bearer token when the target Pi has one configured.
    # Each camera in settings.yaml → cameras[] may declare `api_token: ...`;
    # without it the remote Pi's auth guard will reject mutating verbs.
    cam_cfg   = CAMERAS[idx] if 0 <= idx < len(CAMERAS) else {}
    cam_token = cam_cfg.get("api_token") or API_TOKEN     # one fleet token by default
    if cam_token and request.method in ("POST", "DELETE", "PUT", "PATCH"):
        fwd_headers["Authorization"] = f"Bearer {cam_token}"

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
