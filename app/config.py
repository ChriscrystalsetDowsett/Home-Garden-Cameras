"""Shared configuration — loads settings.yaml, derives paths and constants.

Fails fast at import if security-critical fields are missing or use their
placeholder defaults.
"""
from pathlib import Path
import yaml

PROJECT_ROOT = Path(__file__).parent.parent   # home-garden-cameras/

# ── Load settings ─────────────────────────────────────────────────────────────
_cfg_path = PROJECT_ROOT / "config" / "settings.yaml"
with open(_cfg_path) as _f:
    _cfg = yaml.safe_load(_f)

# ── Server ────────────────────────────────────────────────────────────────────
_server        = _cfg.get("server", {})
SERVER_HOST    = _server.get("host", "0.0.0.0")
SERVER_PORT    = int(_server.get("port", 8080))
PI_NAME        = _server.get("pi_name", "unknown")
PHOTO_BASE_URL = _server.get(
    "photo_base_url",
    f"https://media.chrisdowsett.com/media/{PI_NAME}/photos",
).rstrip("/")

# Bearer-token gate + CORS whitelist (see app/auth.py, app/app.py)
API_TOKEN        = _server.get("api_token", "")
ALLOWED_ORIGINS  = list(_server.get("allowed_origins", []) or [])

# ── Data paths ────────────────────────────────────────────────────────────────
_paths       = _cfg.get("paths", {})
SNAPSHOT_DIR = (PROJECT_ROOT / _paths.get("photos", "data/photos")).resolve()
VIDEOS_DIR   = (PROJECT_ROOT / _paths.get("videos", "data/videos")).resolve()
LOG_DIR      = (PROJECT_ROOT / _paths.get("logs",   "data/logs")).resolve()
SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ── Resolution table ──────────────────────────────────────────────────────────
RESOLUTIONS = {
    "640x480":   (640, 480),
    "1280x720":  (1280, 720),
    "1920x1080": (1920, 1080),
}

DEFAULT_RESOLUTION = _cfg["camera"]["default_resolution"]
if DEFAULT_RESOLUTION not in RESOLUTIONS:
    DEFAULT_RESOLUTION = "1280x720"

# Quality for the live stream served by /api/frame (v4l2 backend only).
# picamera2 backend uses STREAM_BITRATE instead — bitrate controls quality there.
STREAM_JPEG_QUALITY = max(1, min(95, int(_cfg["camera"].get("stream_quality", 60))))
# picamera2 live feed: quality follows the network link (checked every LINK_CHECK_S).
# Wifi tops out around 5 MB/s on pi4, so higher quality there costs frame rate.
STREAM_QUALITY_WIFI     = max(1, min(95, int(_cfg["camera"].get("stream_quality_wifi", STREAM_JPEG_QUALITY))))
STREAM_QUALITY_ETHERNET = max(1, min(95, int(_cfg["camera"].get("stream_quality_ethernet", 90))))
ETHERNET_INTERFACE      = str(_cfg["camera"].get("ethernet_interface", "eth0"))
LINK_CHECK_S            = 10

# Bitrate for the MJPEGEncoder live feed (picamera2 backend only).
STREAM_BITRATE = max(1_000_000, int(_cfg["camera"].get("stream_bitrate", 10_000_000)))

CAM_BACKEND = _cfg["camera"].get("backend", "picamera2")   # "picamera2" | "v4l2"
V4L2_MODE   = _cfg["camera"].get("v4l2_mode", "passthrough")  # "passthrough" | "opencv"
CAM_USB_ID  = _cfg["camera"].get("usb_id", "046d:0843")    # vendor:product for USB reset/presence check

# ── Dashboard camera list ─────────────────────────────────────────────────────
CAMERAS            = _cfg.get("cameras", [])
_dash              = _cfg.get("dashboard", {})
TILE_QUALITY       = _dash.get("tile_quality", "low")
# Header links on the camera wall: [{label, url}, ...]
DASHBOARD_LINKS    = [l for l in (_dash.get("links") or []) if l.get("url") and l.get("label")]
DASHBOARD_PASSWORD = _dash.get("password", "")
SECRET_KEY         = _dash.get("secret_key", "")

# ── Servo pan/tilt ────────────────────────────────────────────────────────────
_servo         = _cfg.get("servo", {})
SERVO_ENABLED     = bool(_servo.get("enabled", False))
SERVO_SPEED       = max(0.0, min(1.0, float(_servo.get("speed", 0.8))))
SERVO_DRIVER_PATH = str(_servo.get("driver_path", "/home/chris/pantilt"))   # pins + limits live there
SERVO_SWIPE       = bool(_servo.get("swipe", False))          # fullscreen drag joystick (views are the main control)
SERVO_NUDGE_PAN   = float(_servo.get("nudge_pan_deg", 5))     # arrow-button step sizes
SERVO_NUDGE_TILT  = float(_servo.get("nudge_tilt_deg", 4))


# ── Camera schedule ───────────────────────────────────────────────────────────
_sched             = _cfg.get("schedule", {})
SCHEDULE_ENABLED   = bool(_sched.get("enabled", False))
SCHEDULE_OFF       = _sched.get("camera_off", "22:00")  # HH:MM local time
SCHEDULE_ON        = _sched.get("camera_on",  "06:00")  # HH:MM local time

# ── libcamera constants ───────────────────────────────────────────────────────
AWB_MODES = {
    "auto": 0, "incandescent": 1, "tungsten": 2, "fluorescent": 3,
    "indoor": 4, "daylight": 5, "cloudy": 6,
}

NOISE_MODES = {"off": 0, "fast": 1, "high_quality": 2}

# ── Camera control defaults ───────────────────────────────────────────────────
# hflip/vflip seed from settings.yaml so the Pi can be mounted any way you like.
CAM_CTRL_DEFAULTS = {
    "exposure_time":  int(_cfg["camera"].get("exposure_time",  0)),
    "analogue_gain":   0.0,
    "awb_mode":       _cfg["camera"].get("awb_mode",   "auto"),
    "awb_kelvin": int(_cfg["camera"].get("awb_kelvin",  5000)),
    "brightness":      0,
    "saturation": int(_cfg["camera"].get("saturation",    0)),
    "sharpness": float(_cfg["camera"].get("sharpness",  1.5)),
    "contrast":  float(_cfg["camera"].get("contrast",   1.0)),
    "backlight_compensation": int(_cfg["camera"].get("backlight_compensation", 0)),
    "noise_reduction": "off",
    "af_mode":  "continuous",
    "af_range": "normal",
    "ae_metering_mode":   0,
    "ae_constraint_mode": 0,
    "hdr_mode":           0,
    "tint":        int(_cfg["camera"].get("tint",    0)),
    "warmth":      int(_cfg["camera"].get("warmth",  0)),
    "hflip":       bool(_cfg["camera"].get("hflip", False)),
    "vflip":       bool(_cfg["camera"].get("vflip", False)),
    "film_filter": "none",
    "film_strength": 100,
}


# ── Fail-fast validation ─────────────────────────────────────────────────────
# Placeholder tokens that must never make it to production.
_PLACEHOLDER_SECRETS = {"", "dev-secret-key", "changeme", "changeme-please"}


def _validate():
    """Raise RuntimeError if security-critical fields are missing/placeholder.

    Skipped when running under pytest so unit tests don't need a live token,
    or when HGC_SKIP_SECRET_CHECK=1 (for one-off scripts).
    """
    import os
    if "PYTEST_CURRENT_TEST" in os.environ or os.environ.get("HGC_SKIP_SECRET_CHECK") == "1":
        return
    errs = []
    if SECRET_KEY in _PLACEHOLDER_SECRETS:
        errs.append(
            "dashboard.secret_key missing or a placeholder. Generate with:\n"
            "  python3 -c \"import secrets; print(secrets.token_hex(32))\""
        )
    if API_TOKEN in _PLACEHOLDER_SECRETS:
        errs.append(
            "server.api_token missing or a placeholder. Generate with:\n"
            "  python3 -c \"import secrets; print(secrets.token_hex(32))\""
        )
    if errs:
        raise RuntimeError(
            "config/settings.yaml is not production-safe:\n  - "
            + "\n  - ".join(errs)
        )


_validate()
