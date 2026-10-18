"""Timelapses: still and moving (pan/tilt), captured on a schedule and built
into a video any phone can play.

A session is a folder of frames in data/timelapse/<name>/ plus session.json.
When it ends, the frames become an MP4 in data/videos/ and the folder gets a
DONE marker; the nightly sync then moves the frames to bigPC (they are full
quality and worth keeping for regrading).

Frame sources (CSI cameras):
  "4k"  full-sensor stills, video scaled to 3840x2160. Each frame pauses the
        live view for under a second, so it needs an interval of 4 s or more.
  "hd"  frames from the running 1080p video: any interval, no pause.
  USB cameras always use the stream frame (1080p).

For the whole shot, focus and white balance are held (a timelapse that
refocuses or shifts colour between frames flickers), and the build applies
a deflicker filter for exposure changes.

Motion (cameras with a rig): the camera moves from one pose to another over the
shot, one small step before each frame, easing in and out ("smooth") or at a
constant rate ("linear"). The gearbox slack is taken up before the first frame
so every step moves the same way. While it runs, the views can't be used.
"""
import json
import logging
import math
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from .config import VIDEOS_DIR, CAM_BACKEND, PROJECT_ROOT
from .recorder import _extract_thumbnail
from .camera import camera, cam_ctrl, cam_ctrl_lock

log = logging.getLogger(__name__)

SESSIONS_DIR = PROJECT_ROOT / "data" / "timelapse"
OUTPUT_FPS = 24
MIN_4K_INTERVAL_S = 4.0
MOTION_SETTLE_S = 0.5           # let the rig stop shaking before the frame
HYPERFOCAL_DIOPTRES = 0.33      # IMX708 at f/1.8: ~3 m, sharp from ~1.5 m to infinity

# ── Presets (the camera page and the wall show these) ──────────────────────────
PRESETS = [
    {"id": "tl30", "name": "30 min timelapse", "detail": "a frame every 10 s · 4K",
     "interval": 10, "duration": 1800, "quality": "auto"},
    {"id": "tl2h", "name": "2 hour timelapse", "detail": "a frame every 30 s · 4K",
     "interval": 30, "duration": 7200, "quality": "auto"},
    {"id": "tlday", "name": "Whole day", "detail": "a frame every 2 min until it's stopped",
     "interval": 120, "duration": 0, "quality": "auto"},
]
MOTION_PRESETS = [
    {"id": "glide", "name": "Glide", "detail": "drift from one view to another · 30 min",
     "interval": 10, "duration": 1800, "kind": "views", "ease": "smooth"},
    {"id": "sweep", "name": "Room sweep", "detail": "pan the whole room, left to right · 1 h",
     "interval": 10, "duration": 3600, "kind": "pan", "ease": "smooth"},
    {"id": "rise", "name": "Rise", "detail": "tilt up from the floor to the current view · 20 min",
     "interval": 6, "duration": 1200, "kind": "tilt", "ease": "smooth"},
]


def motion_for_preset(preset_id, frm=None, to=None):
    """Concrete motion for a motion preset: 'views' uses frm/to (view ids);
    'pan' sweeps the whole pan range at the current tilt; 'tilt' rises from
    near the floor to the current pose."""
    from .servo import servo
    p = next((m for m in MOTION_PRESETS if m["id"] == preset_id), None)
    if p is None:
        raise ValueError("unknown motion preset")
    lim = servo.limits()
    pan, tilt = servo.position()
    if p["kind"] == "views":
        if not frm or not to or frm == to:
            raise ValueError("pick two different views")
        return {"from": frm, "to": to, "ease": p["ease"], "label": p["name"]}
    if p["kind"] == "pan":
        edge = lim["pan"] - 5
        return {"from": {"pan": -edge, "tilt": tilt}, "to": {"pan": edge, "tilt": tilt},
                "ease": p["ease"], "label": p["name"]}
    lo = lim["tilt"][0] + 5
    return {"from": {"pan": pan, "tilt": lo}, "to": {"pan": pan, "tilt": max(tilt, lo + 10)},
            "ease": p["ease"], "label": p["name"]}


def _ease(u, kind):
    u = max(0.0, min(1.0, u))
    return u if kind == "linear" else (1 - math.cos(math.pi * u)) / 2


# ── Build the video ────────────────────────────────────────────────────────────
_compile_lock = threading.Lock()
_compile_status = {"running": False, "output": None, "error": None, "count": 0}


def get_compile_status():
    with _compile_lock:
        return dict(_compile_status)


def compile_session(folder):
    """Frames in `folder` → MP4 in data/videos. Returns the video filename or None."""
    folder = Path(folder)
    frames = sorted(folder.glob("tl_*.jpg"))
    meta = {}
    try:
        meta = json.loads((folder / "session.json").read_text())
    except (OSError, ValueError):
        pass
    if not frames:
        (folder / "DONE").write_text("no frames\n")
        return None
    hd = meta.get("quality") == "hd" or CAM_BACKEND != "picamera2"
    w, h = (1920, 1080) if hd else (3840, 2160)
    out_name = folder.name + ".mp4"
    out_path = VIDEOS_DIR / out_name
    tmp_path = VIDEOS_DIR / (folder.name + ".building.mp4")
    list_path = folder / "frames.txt"
    list_path.write_text("".join(f"file '{f.name}'\nduration {1 / OUTPUT_FPS:.6f}\n" for f in frames))
    start = meta.get("started_iso") or datetime.now().isoformat(timespec="seconds")
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos,"
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1")
    if len(frames) >= 10:
        vf += ",deflicker=size=7:mode=pm"
    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path),
           "-vf", vf, "-r", str(OUTPUT_FPS), "-c:v", "libx264", "-preset", "medium", "-crf", "18",
           "-profile:v", "high", "-level", "5.1" if not hd else "4.1", "-pix_fmt", "yuv420p",
           "-movflags", "+faststart",
           "-metadata", f"creation_time={start}", "-metadata", f"title={meta.get('title', folder.name)}",
           "-metadata", "comment=Home Garden Cameras timelapse", str(tmp_path)]
    with _compile_lock:
        _compile_status.update({"running": True, "output": None, "error": None, "count": len(frames)})
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=4 * 3600)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode(errors="replace")[-400:])
        tmp_path.replace(out_path)
        _extract_thumbnail(out_path)
        (folder / "DONE").write_text(out_name + "\n")
        with _compile_lock:
            _compile_status.update({"running": False, "output": out_name, "error": None})
        log.info("timelapse %s: %d frames -> %s", folder.name, len(frames), out_name)
        return out_name
    except Exception as e:
        tmp_path.unlink(missing_ok=True)
        log.warning("timelapse build %s failed: %s", folder.name, e)
        with _compile_lock:
            _compile_status.update({"running": False, "output": None, "error": str(e)})
        return None
    finally:
        list_path.unlink(missing_ok=True)


# ── Locks held for the length of a shot (picamera2) ────────────────────────────

def _hold_focus_and_colour(motion):
    """Fix focus and white balance where they are now; returns a short note."""
    if CAM_BACKEND != "picamera2":
        return ""
    notes = []
    try:
        md = camera.camera_job(lambda p, c: p.capture_metadata())
        gains = md.get("ColourGains")
        if gains:
            camera.awb_locked_gains = tuple(gains)
            notes.append("white balance held")

        def focus(p, c):
            if "AfMode" not in p.camera_controls:
                return False
            if motion:
                # A moving shot crosses near and far things: focus at the
                # hyperfocal distance keeps everything from ~1.5 m sharp.
                p.set_controls({"AfMode": 0, "LensPosition": HYPERFOCAL_DIOPTRES})
            else:
                p.set_controls({"AfMode": 1, "AfTrigger": 0})
            return True
        if camera.camera_job(focus):
            if motion:
                camera.lens_position = HYPERFOCAL_DIOPTRES
            else:
                time.sleep(1.5)                 # let the one-shot AF land
                camera.camera_job(lambda p, c: p.set_controls({"AfMode": 0}))
                camera.lens_position = camera.camera_job(
                    lambda p, c: p.capture_metadata()).get("LensPosition")
            camera.af_locked = True
            notes.append("focus held")
        with cam_ctrl_lock:
            c = dict(cam_ctrl)
        camera.apply_isp_controls(c)
    except RuntimeError as e:
        log.warning("timelapse: could not hold focus/colour: %s", e)
    return ", ".join(notes)


def _release_focus_and_colour():
    if CAM_BACKEND != "picamera2":
        return
    camera.af_locked = False
    camera.awb_locked_gains = None
    camera.lens_position = None
    with cam_ctrl_lock:
        c = dict(cam_ctrl)
    camera.apply_isp_controls(c)


# ── Sessions ───────────────────────────────────────────────────────────────────

class TimeLapseManager:
    def __init__(self):
        self.running = False
        self.interval = 10.0
        self.duration = 0
        self.count = 0
        self.expected = 0
        self.missed = 0
        self.quality = "hd"
        self.motion = None
        self.note = ""
        self.title = ""
        self.start_time = None
        self.next_at = None
        self.folder = None
        self.error = None
        self._stop_event = threading.Event()
        self._thread = None

    # ---- public ------------------------------------------------------------

    def start(self, interval, duration=0, quality="auto", motion=None, title=None):
        """Start a timelapse. Raises ValueError/RuntimeError with a readable reason.

        motion: None, or {"from": <pose>, "to": <pose>, "ease": "smooth"|"linear"}
        where a pose is a saved view id, {"pan": .., "tilt": ..}, or None (here).
        """
        if self.running:
            raise RuntimeError("a timelapse is already running")
        interval = max(0.5, float(interval))
        duration = max(0.0, float(duration or 0))
        if quality == "auto":
            quality = "4k" if (CAM_BACKEND == "picamera2" and interval >= MIN_4K_INTERVAL_S) else "hd"
        if quality == "4k" and interval < MIN_4K_INTERVAL_S:
            raise ValueError(f"4K frames need an interval of at least {MIN_4K_INTERVAL_S:.0f} s")

        plan = None
        if motion:
            from .servo import servo
            if not servo.enabled:
                raise ValueError("this camera has no pan/tilt rig")
            if not duration:
                raise ValueError("a moving timelapse needs a length")
            a = servo.pose_of(motion.get("from"))
            b = servo.pose_of(motion.get("to"))
            plan = {"from": a, "to": b, "ease": motion.get("ease", "smooth"),
                    "label": motion.get("label", "")}

        stamp = datetime.now()
        name = "Timelapse_" + stamp.strftime("%Y-%m-%d_%H-%M-%S")
        folder = SESSIONS_DIR / name
        folder.mkdir(parents=True, exist_ok=True)
        self.title = title or ("Moving timelapse" if plan else "Timelapse")
        (folder / "session.json").write_text(json.dumps({
            "title": self.title, "interval": interval, "duration": duration, "quality": quality,
            "motion": plan, "started_iso": stamp.isoformat(timespec="seconds"),
        }, indent=1))

        self.interval, self.duration, self.quality, self.motion = interval, duration, quality, plan
        self.expected = int(math.floor(duration / interval + 1e-6)) + 1 if duration else 0
        self.count = self.missed = 0
        self.folder, self.error, self.note = folder, None, ""
        self.start_time = time.time()
        self.next_at = self.start_time
        self.running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="timelapse")
        self._thread.start()
        return self.status()

    def stop(self):
        self._stop_event.set()

    def status(self):
        now = time.time()
        elapsed = now - self.start_time if self.start_time and self.running else 0
        return {
            "running": self.running,
            "interval": self.interval,
            "duration": self.duration,
            "count": self.count,
            "expected": self.expected,
            "missed": self.missed,
            "elapsed": elapsed,
            "next_in": max(0.0, self.next_at - now) if self.running and self.next_at else None,
            "quality": self.quality,
            "motion": self.motion,
            "title": self.title,
            "note": self.note,
            "error": self.error,
            "session": self.folder.name if self.folder else None,
        }

    # ---- capture loop ------------------------------------------------------

    def _pose_at(self, k):
        a, b = self.motion["from"], self.motion["to"]
        n = max(1, self.expected - 1)
        e = _ease(k / n, self.motion["ease"])
        return (a[0] + (b[0] - a[0]) * e, a[1] + (b[1] - a[1]) * e)

    def _prepare_motion(self):
        """Go to the start pose with the gearbox slack taken up in the direction of travel."""
        from .servo import servo
        a, b = self.motion["from"], self.motion["to"]
        servo.begin_motion({"title": self.title, "from": a, "to": b})
        backlash = 7.0
        lim = servo.limits()
        if b[0] < a[0]:                     # moving left: arrive at the start going left
            pre = min(lim["pan"], a[0] + backlash)
            servo.move_for_motion(pre, a[1], direct=False)
        else:                               # moving right (or tilt only): move_to already ends going right
            servo.move_for_motion(a[0], a[1], direct=False)
        servo.move_for_motion(a[0], a[1], direct=True)
        time.sleep(MOTION_SETTLE_S)

    def _run(self):
        from .servo import servo
        try:
            if self.motion:
                self._prepare_motion()
            self.note = _hold_focus_and_colour(bool(self.motion))
            self.start_time = time.time()           # the shot starts once everything is set
            k = 0
            while not self._stop_event.is_set():
                if self.expected and k >= self.expected:
                    break
                slot = self.start_time + k * self.interval
                wait = slot - time.time()
                self.next_at = slot
                if wait > 0 and self._stop_event.wait(wait):
                    break
                if time.time() - slot > self.interval * 0.9:    # too late for this slot: skip it
                    self.missed += 1
                    k += 1
                    continue
                if self.motion:
                    pan, tilt = self._pose_at(k)
                    servo.move_for_motion(pan, tilt, direct=True)
                    time.sleep(MOTION_SETTLE_S)
                fn = camera.capture(prefix=f"tl_{k:05d}", full_res=(self.quality == "4k"),
                                    dest_dir=self.folder, description=self.title)
                if fn:
                    self.count += 1
                else:
                    self.missed += 1
                k += 1
        except Exception as e:
            self.error = str(e)
            log.warning("timelapse stopped by an error: %s", e)
        finally:
            if self.motion:
                servo.end_motion()
            _release_focus_and_colour()
            self.running = False
            self.next_at = None
            folder = self.folder
            threading.Thread(target=compile_session, args=(folder,), daemon=True,
                             name="timelapse-build").start()


timelapse = TimeLapseManager()


def preview_motion(motion, seconds=8.0):
    """Run a motion path quickly (no capture) so the framing can be checked."""
    from .servo import servo
    if timelapse.running:
        raise RuntimeError("a timelapse is running")
    a = servo.pose_of(motion.get("from"))
    b = servo.pose_of(motion.get("to"))
    ease = motion.get("ease", "smooth")

    def run():
        servo.begin_motion({"title": "Preview", "from": a, "to": b, "preview": True})
        try:
            servo.move_for_motion(a[0], a[1], direct=False)
            time.sleep(0.4)
            steps = 12
            for i in range(1, steps + 1):
                e = _ease(i / steps, ease)
                servo.move_for_motion(a[0] + (b[0] - a[0]) * e, a[1] + (b[1] - a[1]) * e, direct=True)
        finally:
            servo.end_motion()
    threading.Thread(target=run, daemon=True, name="motion-preview").start()
    return {"from": a, "to": b}


def _resume_unfinished():
    """Build any session left unbuilt by a restart (frames kept, video made now)."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    for folder in sorted(SESSIONS_DIR.iterdir()):
        if folder.is_dir() and not (folder / "DONE").exists() and any(folder.glob("tl_*.jpg")):
            compile_session(folder)
    # Frames from the old engine (tl_*.jpg straight in the photos folder)
    from .config import SNAPSHOT_DIR
    old = sorted(SNAPSHOT_DIR.glob("tl_*.jpg"))
    if old:
        folder = SESSIONS_DIR / ("Timelapse_" + datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + "_recovered")
        folder.mkdir(parents=True, exist_ok=True)
        for f in old:
            shutil.move(str(f), folder / f.name)
        compile_session(folder)


threading.Thread(target=_resume_unfinished, daemon=True, name="timelapse-resume").start()
