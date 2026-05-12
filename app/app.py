"""Flask application — all HTTP routes."""
import os
from pathlib import Path

from flask import Flask, Response, render_template, make_response, jsonify, request, send_from_directory, after_this_request

from .config import SNAPSHOT_DIR, VIDEOS_DIR, CAM_CTRL_DEFAULTS, SECRET_KEY
from .camera import camera, cam_ctrl, cam_ctrl_lock
from .timelapse import timelapse, get_compile_status
from .recorder import video_recorder, AUDIO_AVAILABLE, audio_streamer
from .stats import get_stats, get_pi_info
from .dashboard import dashboard as dashboard_bp
from .servo import servo
from . import scheduler

# Wire the recorder into the camera stream so it captures what the user sees
camera.output.recorder = video_recorder
scheduler.start(camera)

app = Flask(
    __name__,
    template_folder=str(Path(__file__).parent.parent / "templates"),
    static_folder=str(Path(__file__).parent.parent / "static"),
)
app.secret_key = SECRET_KEY
app.register_blueprint(dashboard_bp)


@app.after_request
def _cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, DELETE, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response


# ── Stream ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    resp = make_response(render_template("index.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


@app.route("/api/frame")
def api_frame():
    """Return the current JPEG frame (pull-based streaming).

    Served at STREAM_JPEG_QUALITY (see settings.yaml) so the live feed uses
    less bandwidth.  Photos, video, and timelapse use the full-quality buffer.
    """
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
    res = request.json.get("resolution")
    ok = camera.set_resolution(res)
    return jsonify({"ok": ok, "resolution": camera.res_key})


# ── Snapshot ───────────────────────────────────────────────────────────────────

@app.route("/api/snapshot", methods=["POST"])
def snapshot():
    data        = request.json or {}
    filter_name = data.get("filter", "none")
    quality     = int(data.get("quality", 85))
    filename    = camera.capture(filter_name=filter_name, quality=quality)
    if filename:
        return jsonify({"ok": True, "filename": filename})
    return jsonify({"ok": False}), 500


@app.route("/snapshots/<filename>")
def serve_snapshot(filename):
    return send_from_directory(SNAPSHOT_DIR, filename)


@app.route("/api/snapshot/<filename>", methods=["DELETE"])
def delete_snapshot(filename):
    if "/" in filename or ".." in filename:
        return jsonify({"ok": False}), 400
    path = SNAPSHOT_DIR / filename
    if not path.exists():
        return jsonify({"ok": False}), 404
    path.unlink()
    return jsonify({"ok": True})


# ── Gallery ────────────────────────────────────────────────────────────────────

@app.route("/api/gallery")
def gallery():
    entries = []
    with os.scandir(SNAPSHOT_DIR) as it:
        for entry in it:
            if entry.name.endswith(".jpg"):
                st = entry.stat()
                entries.append((st.st_mtime, entry.name, st.st_size))
    entries.sort(key=lambda x: x[0], reverse=True)
    return jsonify([{"filename": n, "size": s} for _, n, s in entries[:100]])


# ── Timelapse ──────────────────────────────────────────────────────────────────

@app.route("/api/timelapse/start", methods=["POST"])
def tl_start():
    interval = request.json.get("interval", 5)
    duration = request.json.get("duration", 0)
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
    """Stream live audio as AAC/ADTS — the Safari-compatible fallback format.

    ADTS is self-synchronising and supported natively by Safari's <audio> element.
    Chrome and Firefox also support it, so this endpoint works everywhere.
    """
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
    """Stream live audio as raw s16le PCM at 16 kHz for Web Audio API playback.

    No container overhead — data flows within milliseconds of capture,
    giving far lower latency than the Ogg endpoint.
    """
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
    data  = request.json or {}
    crf   = int(data.get("quality", 23))
    audio = bool(data.get("audio", False))
    ok    = video_recorder.start(crf=crf, audio=audio)
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
            if entry.name.endswith(".mp4"):
                st = entry.stat()
                entries.append((st.st_mtime, entry.name, st.st_size))
    entries.sort(key=lambda x: x[0], reverse=True)
    return jsonify([
        {"filename": n, "size": s,
         "has_thumb": (VIDEOS_DIR / n.replace(".mp4", ".thumb.jpg")).exists()}
        for _, n, s in entries[:50]
    ])


@app.route("/videos/<filename>")
def serve_video(filename):
    return send_from_directory(VIDEOS_DIR, filename)


@app.route("/api/videos/<filename>", methods=["DELETE"])
def delete_video(filename):
    if "/" in filename or ".." in filename:
        return jsonify({"ok": False}), 400
    path = VIDEOS_DIR / filename
    if not path.exists():
        return jsonify({"ok": False}), 404
    path.unlink()
    return jsonify({"ok": True})


# ── Camera controls ────────────────────────────────────────────────────────────

@app.route("/api/camera_controls", methods=["POST"])
def set_cam_controls():
    data = request.json or {}
    with cam_ctrl_lock:
        for k, v in data.items():
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
    data = request.json or {}
    servo.move(float(data.get("pan", 0.0)), float(data.get("tilt", 0.0)))
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
    return jsonify(get_stats())


@app.route("/api/info")
def pi_info():
    info = get_pi_info()
    info["audio_available"] = AUDIO_AVAILABLE
    return jsonify(info)
