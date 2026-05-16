"""Timelapse capture manager and ffmpeg compilation."""
import threading, time, subprocess
from datetime import datetime
from pathlib import Path

from .config import SNAPSHOT_DIR, VIDEOS_DIR, CAM_BACKEND
from .recorder import _extract_thumbnail
from .camera import camera, cam_ctrl, cam_ctrl_lock

# ── Compile status (shared with app routes) ───────────────────────────────────
_compile_lock   = threading.Lock()
_compile_status = {"running": False, "output": None, "error": None, "count": 0}


def compile_timelapse_to_video(files, output_name=None):
    """Compile a list of JPEG paths into a 24 fps MP4; delete originals on success."""
    files = sorted(str(f) for f in files)
    if not files:
        return None
    now       = datetime.now()
    ts        = now.strftime("%Y-%m-%d_%H-%M-%S")
    iso_ts    = now.strftime("%Y-%m-%dT%H:%M:%S")
    out_name  = output_name or f"Timelapse_{ts}.mp4"
    out_stem  = out_name.removesuffix(".mp4")
    out_path  = VIDEOS_DIR / out_name
    list_path = VIDEOS_DIR / f"_tl_list_{ts}.txt"
    with _compile_lock:
        _compile_status.update({"running": True, "output": None, "error": None, "count": len(files)})
    try:
        with open(list_path, "w") as fh:
            for fp in files:
                fh.write(f"file '{fp}'\n")
        result = subprocess.run(
            ["ffmpeg", "-y",
             "-f", "concat", "-safe", "0", "-i", str(list_path),
             "-r", "24", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "23",
             "-movflags", "+faststart",
             "-metadata", f"creation_time={iso_ts}",
             "-metadata", f"title={out_stem}",
             "-metadata", "comment=Home Garden Cameras Timelapse",
             "-metadata", "encoder=Home Garden Cameras",
             str(out_path)],
            capture_output=True, timeout=7200,
        )
        if result.returncode == 0:
            for fp in files:
                try:
                    Path(fp).unlink()
                except Exception:
                    pass
            _extract_thumbnail(out_path)
            with _compile_lock:
                _compile_status.update({"running": False, "output": out_name, "error": None})
            return out_name
        err = result.stderr.decode(errors="replace")[-500:]
        out_path.unlink(missing_ok=True)
        with _compile_lock:
            _compile_status.update({"running": False, "output": None, "error": err})
        return None
    except Exception as e:
        out_path.unlink(missing_ok=True)
        with _compile_lock:
            _compile_status.update({"running": False, "output": None, "error": str(e)})
        return None
    finally:
        list_path.unlink(missing_ok=True)


def get_compile_status():
    with _compile_lock:
        return dict(_compile_status)


# ── Timelapse manager ──────────────────────────────────────────────────────────
class TimeLapseManager:
    def __init__(self):
        self.running = False
        self.interval = 5.0
        self.duration = 0      # 0 = unlimited
        self.count = 0
        self.thread = None
        self.start_time = None
        self._files = []
        self._stop_event = threading.Event()

    def start(self, interval, duration=0):
        if self.running:
            return
        self.interval   = max(0.5, float(interval))
        self.duration   = max(0, float(duration))
        self.running    = True
        self.count      = 0
        self._files     = []
        self.start_time = time.time()
        self._stop_event.clear()
        self.thread     = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        self._stop_event.set()

    def status(self):
        elapsed = time.time() - self.start_time if self.start_time else 0
        return {
            "running":  self.running,
            "interval": self.interval,
            "duration": self.duration,
            "count":    self.count,
            "elapsed":  elapsed,
        }

    def _run(self):
        _lock_focus()   # one-shot AF then freeze — no-op on non-AF sensors

        while self.running:
            if self.duration > 0:
                if time.time() - self.start_time >= self.duration:
                    self.running = False
                    break
            filename = camera.capture(prefix="tl")
            if filename:
                self._files.append(SNAPSHOT_DIR / filename)
                self.count += 1
            # Sleep for interval, but wake early if stopped or duration expires
            sleep_for = self.interval
            if self.duration > 0:
                remaining = self.duration - (time.time() - self.start_time)
                sleep_for = min(sleep_for, max(0.1, remaining))
            self._stop_event.wait(timeout=sleep_for)
            self._stop_event.clear()

        _unlock_focus() # restore user's AF mode before (possibly long) compile

        if self._files:
            files = list(self._files)
            self._files = []
            threading.Thread(
                target=compile_timelapse_to_video, args=(files,), daemon=True
            ).start()


timelapse = TimeLapseManager()


# ── Focus lock helpers (picamera2 / IMX708 only) ───────────────────────────────

def _lock_focus():
    """Trigger one-shot AF, wait ~1.5 s for it to settle, then freeze to manual.

    Called at the start of every timelapse capture loop so that focus drift
    cannot ruin long sessions.  Silently no-ops on V4L2 cameras or sensors
    that do not support AF controls (e.g. fixed-focus IMX219).
    If the user has already set af_mode='manual' we honour that and skip.
    """
    if CAM_BACKEND != "picamera2":
        return
    with cam_ctrl_lock:
        if cam_ctrl.get("af_mode", "continuous") == "manual":
            return
    try:
        with camera.lock:
            handle = camera._handle
        if handle is None:
            return
        # One-shot AF trigger, then lock the lens position
        handle.set_controls({"AfMode": 1, "AfTrigger": 0})
        time.sleep(1.5)          # allow the lens to reach focus
        handle.set_controls({"AfMode": 0})
    except Exception:
        pass


def _unlock_focus():
    """Restore the user's configured AF mode once capture is done."""
    if CAM_BACKEND != "picamera2":
        return
    try:
        with cam_ctrl_lock:
            c = dict(cam_ctrl)
        camera.apply_isp_controls(c)
    except Exception:
        pass


# Compile any timelapse images left over from a previous session
def _compile_existing_timelapse():
    existing = sorted(SNAPSHOT_DIR.glob("tl_*.jpg"))
    if existing:
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        compile_timelapse_to_video(existing, f"Timelapse_{ts}.mp4")


threading.Thread(target=_compile_existing_timelapse, daemon=True).start()
