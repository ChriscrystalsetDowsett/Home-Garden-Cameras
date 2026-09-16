"""Flask application — all HTTP routes.

v2 additions vs v1:
- Bearer-token auth on all mutating verbs (with loopback bypass so cron
  keeps working). See app/auth.py.
- CORS restricted to a whitelist (settings.yaml → server.allowed_origins).
- Single source of truth for PHOTO_BASE_URL — no hardcoded fallback.
- Disk-space guard on /api/snapshot (507 if free < 5%).
- GET /api/health for watchdogs and external dashboards.
- Machine-readable error responses: {ok: false, error: CODE, message: ...}.
- Rotating file logs under data/logs/app.log.
"""
import logging
import os
import shutil
import time
from pathlib import Path
from PIL import Image

from flask import (
    Flask, Response, render_template, make_response, jsonify, request,
    redirect, send_from_directory,
)

from .config import (
    SNAPSHOT_DIR, VIDEOS_DIR, LOG_DIR, CAM_CTRL_DEFAULTS, SECRET_KEY,
    CAM_BACKEND, PI_NAME, PHOTO_BASE_URL, API_TOKEN, ALLOWED_ORIGINS,
)
from .camera import camera, cam_ctrl, cam_ctrl_lock
from .timelapse import timelapse, get_compile_status
from .recorder import video_recorder, AUDIO_AVAILABLE, audio_streamer
from .stats import get_stats, get_pi_info
from .dashboard import dashboard as dashboard_bp
from .servo import servo
from . import scheduler
from . import auth as auth_mod
from . import logs as logs_mod


# ── Logging (rotating file + stderr) ───────────────────────────────────────────
logs_mod.configure(LOG_DIR)
_log = logging.getLogger(__name__)


# Wire the recorder into the camera stream so it captures what the user sees
camera.output.recorder = video_recorder
scheduler.start(camera)

app = Flask(
    __name__,
    template_folder=str(Path(__file__).parent.parent / "templates"),
    static_folder=str(Path(__file__).parent.parent / "static"),
)
app.secret_key = SECRET_KEY
app.config["API_TOKEN"] = API_TOKEN
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.register_blueprint(dashboard_bp)


# ── Auth guard on every mutating request ──────────────────────────────────────
app.before_request(auth_mod.before_request_guard)


# ── CORS — whitelist (empty list = same-origin only) ──────────────────────────
@app.after_request
def _cors(response):
    origin = request.headers.get("Origin")
    if origin and ALLOWED_ORIGINS and origin in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers.setdefault("Vary", "Origin")
    return response


# ── Health check (watchdog target) ─────────────────────────────────────────────
DISK_FREE_REFUSAL_PCT = 5.0   # /api/snapshot returns 507 below this


def _disk_free_pct(path):
    try:
        du = shutil.disk_usage(path)
        return (du.free / du.total) * 100.0
    except OSError:
        return None


@app.route("/api/health")
def api_health():
    frame_age = None
    if camera.output.last_write:
        frame_age = round(time.time() - camera.output.last_write, 2)
    live = frame_age is not None and frame_age < 10
    free_pct = _disk_free_pct(SNAPSHOT_DIR)
    return jsonify({
        "status": "ok",
        "camera": "live" if live else ("stalled" if frame_age is not None else "starting"),
        "backend": CAM_BACKEND,
        "resolution": camera.res_key,
        "frame_age_s": frame_age,
        "disk_free_pct": round(free_pct, 1) if free_pct is not None else None,
        "audio_available": AUDIO_AVAILABLE,
    })


# ── Stream ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    resp = make_response(render_template(
        "index.html",
        audio_available=AUDIO_AVAILABLE,
        cam_backend=CAM_BACKEND,
        pi_name=PI_NAME,
        photo_base_url=PHOTO_BASE_URL,
    ))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


@app.route("/api/frame")
def api_frame():
    if not camera.enabled:
        return "", 503
    frame = camera.get_stream_frame()
    if not frame:
        return "", 503
    return Response(
        frame,
        mimetype="image/jpeg",
        headers={"Cache-Control": "no-store, no-cache"},
    )


@app.route("/api/camera/enabled", methods=["GET"])
def camera_enabled_get():
    return jsonify({"enabled": camera.enabled})


@app.route("/api/camera/enabled", methods=["POST"])
def camera_enabled_set():
    camera.set_enabled((request.json or {}).get("enabled", True))
    return jsonify({"enabled": camera.enabled})


# ── Resolution ─────────────────────────────────────────────────────────────────

@app.route("/api/resolution", methods=["POST"])
def set_resolution():
    res = (request.json or {}).get("resolution")
    ok = camera.set_resolution(res)
    return jsonify({"ok": ok, "resolution": camera.res_key})


# ── Snapshot ───────────────────────────────────────────────────────────────────

def _err(code, message, status=400):
    return jsonify({"ok": False, "error": code, "message": message}), status


@app.route("/api/snapshot", methods=["POST"])
def snapshot():
    free_pct = _disk_free_pct(SNAPSHOT_DIR)
    if free_pct is not None and free_pct < DISK_FREE_REFUSAL_PCT:
        _log.warning("snapshot refused — disk free %.1f%% < %.1f%%",
                     free_pct, DISK_FREE_REFUSAL_PCT)
        return _err("DISK_FULL", f"Disk free {free_pct:.1f}% below refusal threshold", 507)

    payload     = request.json or {}
    prefix      = payload.get("label", "Photo")
    filter_name = payload.get("filter", "none")
    quality     = int(payload.get("quality", 85))
    filename    = camera.capture(prefix=prefix, filter_name=filter_name, quality=quality)
    if not filename:
        return _err("CAPTURE_FAILED", "camera returned no frame", 500)

    # Thumbnail sidecar — best-effort; contract requires the primary file only.
    try:
        thumb_path = SNAPSHOT_DIR / (Path(filename).stem + ".thumb.jpg")
        img = Image.open(SNAPSHOT_DIR / filename)
        img.thumbnail((400, 300))
        img.save(thumb_path, "JPEG", quality=75)
    except Exception as e:
        _log.debug("thumbnail generation failed for %s: %s", filename, e)
    return jsonify({"ok": True, "filename": filename})


@app.route("/snapshots/<filename>")
def serve_snapshot(filename):
    return send_from_directory(SNAPSHOT_DIR, filename)


@app.route("/photo/<filename>")
def serve_photo_local(filename):
    path = SNAPSHOT_DIR / filename
    if path.is_file():
        return send_from_directory(SNAPSHOT_DIR, filename)
    return redirect(f"{PHOTO_BASE_URL}/{filename}")


@app.route("/api/snapshot/<filename>", methods=["DELETE"])
def delete_snapshot(filename):
    if "/" in filename or ".." in filename:
        return _err("INVALID_FILENAME", "filename may not contain / or ..", 400)
    path = SNAPSHOT_DIR / filename
    thumb = SNAPSHOT_DIR / (path.stem + ".thumb.jpg")
    if not path.exists() and not thumb.exists():
        return _err("NOT_FOUND", "photo does not exist", 404)
    path.unlink(missing_ok=True)
    thumb.unlink(missing_ok=True)
    return jsonify({"ok": True})


# ── Gallery ────────────────────────────────────────────────────────────────────

@app.route("/api/gallery")
def gallery():
    entries = []
    with os.scandir(SNAPSHOT_DIR) as it:
        for entry in it:
            if entry.name.endswith(".thumb.jpg"):
                name = entry.name.replace(".thumb.jpg", ".jpg")
                try:
                    st = (SNAPSHOT_DIR / name).stat()
                    local, size = True, st.st_size
                except OSError:
                    local, size = False, entry.stat().st_size
                entries.append((entry.stat().st_mtime, name, size, local))
    entries.sort(key=lambda x: x[0], reverse=True)
    return jsonify([
        {"filename": n, "size": s, "has_thumb": True, "local": l}
        for _, n, s, l in entries[:100]
    ])


# ── Timelapse ──────────────────────────────────────────────────────────────────

@app.route("/api/timelapse/start", methods=["POST"])
def tl_start():
    payload  = request.json or {}
    interval = payload.get("interval", 5)
    duration = payload.get("duration", 0)
    timelapse.start(interval, duration)
    return jsonify({"ok": True, **timelapse.status()})


@app.route("/api/timelapse/stop", methods=["POST"])
def tl_stop():
    timelapse.stop()
    return jsonify({"ok": True, **timelapse.status()})


@app.route("/api/timelapse/status")
def tl_status():
    return jsonify(timelapse.status())


@app.route("/api/timelapse/compile_status")
def tl_compile_status():
    return jsonify(get_compile_status())


# ── Live audio stream ─────────────────────────────────────────────────────────

@app.route("/api/audio/stream")
def audio_stream_aac():
    if not AUDIO_AVAILABLE:
        return "", 503

    def generate():
        yield from audio_streamer.subscribe_aac()

    return Response(
        generate(),
        mimetype="audio/aac",
        headers={"Cache-Control": "no-store, no-cache"},
    )


@app.route("/api/audio/stream/raw")
def audio_stream_raw():
    if not AUDIO_AVAILABLE:
        return "", 503

    def generate():
        yield from audio_streamer.subscribe_raw()

    return Response(
        generate(),
        mimetype="application/octet-stream",
        headers={
            "Cache-Control": "no-store, no-cache",
            "X-Audio-Sample-Rate": "16000",
            "X-Audio-Channels": "1",
            "X-Audio-Format": "s16le",
        },
    )


# ── Recording ──────────────────────────────────────────────────────────────────

@app.route("/api/record/start", methods=["POST"])
def record_start():
    payload = request.json or {}
    crf     = int(payload.get("quality", 23))
    audio   = bool(payload.get("audio", False))
    ok      = video_recorder.start(crf=crf, audio=audio)
    return jsonify({"ok": ok, **video_recorder.status()})


@app.route("/api/record/stop", methods=["POST"])
def record_stop():
    filename, audio_ok = video_recorder.stop()
    return jsonify({"ok": bool(filename), "filename": filename, "audio_ok": audio_ok})


@app.route("/api/record/status")
def record_status():
    return jsonify(video_recorder.status())


# ── Videos ─────────────────────────────────────────────────────────────────────

@app.route("/api/videos")
def list_videos():
    entries = []
    with os.scandir(VIDEOS_DIR) as it:
        for entry in it:
            if entry.name.endswith(".thumb.jpg"):
                name = entry.name.replace(".thumb.jpg", ".mp4")
                try:
                    st = (VIDEOS_DIR / name).stat()
                    local, size = True, st.st_size
                except OSError:
                    local, size = False, entry.stat().st_size
                entries.append((entry.stat().st_mtime, name, size, local))
    entries.sort(key=lambda x: x[0], reverse=True)
    return jsonify([
        {"filename": n, "size": s, "has_thumb": True, "local": l}
        for _, n, s, l in entries[:50]
    ])


@app.route("/videos/<filename>")
def serve_video(filename):
    return send_from_directory(VIDEOS_DIR, filename)


@app.route("/api/videos/<filename>", methods=["DELETE"])
def delete_video(filename):
    if "/" in filename or ".." in filename:
        return _err("INVALID_FILENAME", "filename may not contain / or ..", 400)
    path = VIDEOS_DIR / filename
    thumb = VIDEOS_DIR / (path.stem + ".thumb.jpg")
    if not path.exists() and not thumb.exists():
        return _err("NOT_FOUND", "video does not exist", 404)
    path.unlink(missing_ok=True)
    thumb.unlink(missing_ok=True)
    return jsonify({"ok": True})


# ── Camera controls ────────────────────────────────────────────────────────────

@app.route("/api/camera_controls", methods=["GET"])
def get_cam_controls():
    with cam_ctrl_lock:
        return jsonify(dict(cam_ctrl))


@app.route("/api/camera_controls", methods=["POST"])
def set_cam_controls():
    payload = request.json or {}
    with cam_ctrl_lock:
        for k, v in payload.items():
            if k in CAM_CTRL_DEFAULTS:
                cam_ctrl[k] = v
        current = dict(cam_ctrl)
    camera.apply_isp_controls(current)
    return jsonify({"ok": True})


@app.route("/api/camera_controls/defaults")
def cam_ctrl_defaults():
    return jsonify(CAM_CTRL_DEFAULTS)


# ── Servo pan/tilt ─────────────────────────────────────────────────────────────

@app.route("/api/servo/move", methods=["POST"])
def servo_move():
    payload = request.json or {}
    servo.move(float(payload.get("pan", 0.0)), float(payload.get("tilt", 0.0)))
    return jsonify({"ok": True})


@app.route("/api/servo/stop", methods=["POST"])
def servo_stop():
    servo.stop()
    return jsonify({"ok": True})


@app.route("/api/servo/status")
def servo_status():
    return jsonify(servo.status())


# ── System stats + info ────────────────────────────────────────────────────────

@app.route("/api/stats")
def stats():
    s = get_stats()
    age = time.time() - camera.output.last_write if camera.output.last_write else None
    s["frame_age_s"] = round(age, 1) if age is not None else None
    return jsonify(s)


@app.route("/api/info")
def pi_info():
    info = get_pi_info()
    info["audio_available"] = AUDIO_AVAILABLE
    info["cam_backend"]     = CAM_BACKEND
    return jsonify(info)
