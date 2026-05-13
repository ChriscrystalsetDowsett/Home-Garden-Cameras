"""Shared configuration — loads settings.yaml, derives paths and constants."""
from pathlib import Path
import yaml

PROJECT_ROOT = Path(__file__).parent.parent   # garden-monitor/

# ── Load settings ─────────────────────────────────────────────────────────────
_cfg_path = PROJECT_ROOT / "config" / "settings.yaml"
with open(_cfg_path) as _f:
    _cfg = yaml.safe_load(_f)

# ── Server ────────────────────────────────────────────────────────────────────
SERVER_HOST = _cfg["server"]["host"]
SERVER_PORT = int(_cfg["server"]["port"])

# ── Data paths ────────────────────────────────────────────────────────────────
SNAPSHOT_DIR = (PROJECT_ROOT / _cfg["paths"]["photos"]).resolve()
VIDEOS_DIR   = (PROJECT_ROOT / _cfg["paths"]["videos"]).resolve()
SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
VIDEOS_DIR.mkdir(parents=True, exist_ok=True)

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

# Bitrate for the MJPEGEncoder live feed (picamera2 backend only).
STREAM_BITRATE = max(1_000_000, int(_cfg["camera"].get("stream_bitrate", 10_000_000)))

CAM_BACKEND = _cfg["camera"].get("backend", "picamera2")   # "picamera2" | "v4l2"
V4L2_MODE   = _cfg["camera"].get("v4l2_mode", "passthrough")  # "passthrough" | "opencv"

# ── Dashboard camera list ─────────────────────────────────────────────────────
CAMERAS            = _cfg.get("cameras", [])
_dash              = _cfg.get("dashboard", {})
TILE_QUALITY       = _dash.get("tile_quality", "low")
DASHBOARD_PASSWORD = _dash.get("password", "")
SECRET_KEY         = _dash.get("secret_key", "dev-secret-key")

# ── Servo pan/tilt ────────────────────────────────────────────────────────────
_servo         = _cfg.get("servo", {})
SERVO_ENABLED  = bool(_servo.get("enabled", False))
SERVO_PAN_PIN  = int(_servo.get("pan_pin",  18))
SERVO_TILT_PIN = int(_servo.get("tilt_pin", 19))
SERVO_SPEED    = max(0.0, min(1.0, float(_servo.get("speed", 0.8))))

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
    # Pre-capture: V4L2 hardware controls (C930e)
    "exposure_time":  int(_cfg["camera"].get("exposure_time",  0)),  # 0 = auto; µs otherwise
    "analogue_gain":   0.0,     # 0 = auto; otherwise maps to V4L2 gain 0–255
    "awb_mode":       _cfg["camera"].get("awb_mode",   "auto"),  # "auto" | "manual"
    "awb_kelvin": int(_cfg["camera"].get("awb_kelvin",  5000)),  # K (2000–7500), active when awb_mode=manual
    "brightness":      0,       # −100…+100 → V4L2 0–255 (neutral 128)
    "saturation": int(_cfg["camera"].get("saturation",    0)),  # −100…+100 → V4L2 0–255 (neutral 128)
    "sharpness": float(_cfg["camera"].get("sharpness",  1.5)),  # 0–4 → V4L2 0–255 (neutral 128)
    "contrast":  float(_cfg["camera"].get("contrast",   1.0)),  # 0–4 → V4L2 0–255 (neutral 128)
    "backlight_compensation": int(_cfg["camera"].get("backlight_compensation", 0)),  # 0=off 1=on
    "noise_reduction": "off",   # kept for API compat; not applied on C930e/V4L2
    # Autofocus (picamera2 / IMX708 only)
    "af_mode":  "continuous",   # "continuous" | "auto" | "manual"
    "af_range": "normal",       # "normal" (30cm–∞) | "macro" (3–30cm) | "full" (3cm–∞)
    # Dynamic range — picamera2 / IMX708 only
    "ae_metering_mode":   0,    # 0=CentreWeighted 1=Spot 2=Matrix — full scene metering
    "ae_constraint_mode": 0,    # 0=Normal 1=Highlight 2=Shadows — protect dark areas
    "hdr_mode":           0,    # 0=Off 1=MultiExposure 2=SingleExposure — no fps hit
    # Post-capture: OpenCV per-frame
    "tint":        int(_cfg["camera"].get("tint",    0)),   # −100 (green) … +100 (magenta)
    "warmth":      int(_cfg["camera"].get("warmth",  0)),   # −100 (cool/blue) … +100 (warm/orange)
    "hflip":       bool(_cfg["camera"].get("hflip", False)),
    "vflip":       bool(_cfg["camera"].get("vflip", False)),
    "film_filter": "none",
    "film_strength": 100,     # 0–100 %; blends filtered frame with original
}
