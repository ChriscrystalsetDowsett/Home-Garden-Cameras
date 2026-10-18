"""Pan/tilt control: saved views (presets), nudges, and the swipe joystick.

The hardware driver lives outside this repo, in the pantilt project
(``servo.driver_path``, default /home/chris/pantilt; see its README for the
wiring, limits and tuning).  Saved views live there too (presets.json and
thumbs/), so its command line shares them.  This module adapts the web UI to
it.  No GPIO is touched until ``servo.enabled = true`` in settings.yaml.

pi4 rig (2026-10): pan = 28BYJ-48 stepper on GPIO17/27/22/23 (ULN2003),
tilt = SG90 servo on GPIO18.  Pins and limits are set in pantilt/config.yaml,
not here.

Moves run on a worker thread so HTTP calls return at once; the newest request
wins (tapping another view mid-move just changes the destination).  The UI
polls /api/ptz for progress.

Swipe coordinate convention (velocities from the joystick):
    pan  -1.0 = full speed left,  +1.0 = full speed right
    tilt -1.0 = full speed down,  +1.0 = full speed up
"""

import io
import logging
import sys
import threading
import time

from .config import (SERVO_ENABLED, SERVO_SPEED, SERVO_DRIVER_PATH, SERVO_SWIPE,
                     SERVO_NUDGE_PAN, SERVO_NUDGE_TILT)

log = logging.getLogger(__name__)

_THUMB_WIDTH   = 480
_THUMB_DELAY_S = 1.2    # wait after arriving so the stream shows the new view
_AT_VIEW_DEG   = 1.0    # within this of a view on both axes = "at" that view


class ServoController:
    """Pan/tilt for the web UI: views, nudges and the velocity joystick.

    In stub mode (enabled=False, or the driver failed to load) all calls are
    logged at DEBUG level and no GPIO is imported or touched.
    """

    def __init__(self, enabled: bool, speed: float, driver_path: str):
        self.speed = max(0.0, min(1.0, speed))
        self._rig = None
        self._presets = None
        self._vel = (0.0, 0.0)
        self._seq = None
        self.error = None

        self._frame_source = None        # callable -> current JPEG bytes (set by attach)
        self._timelapse_running = None   # callable -> bool
        self._job = None                 # (pan, tilt, view_id) waiting to run
        self._job_lock = threading.Lock()
        self._job_wake = threading.Event()
        self._moving_to = None           # view id or "nudge" while a move runs
        self._dest = None                # (pan, tilt) of the move in progress
        self._last_error = None

        if enabled:
            try:
                if driver_path not in sys.path:
                    sys.path.insert(0, driver_path)
                from pantilt import PanTilt
                from presets import PresetStore
                self._rig = PanTilt()
                self._presets = PresetStore()
                threading.Thread(target=self._worker, daemon=True, name="ptz-moves").start()
                log.info("Pan/tilt rig ready (driver %s, speed=%.1f)", driver_path, self.speed)
            except Exception as e:      # camera must keep working without the rig
                self.error = str(e)
                self._rig = None
                log.error("Pan/tilt driver failed to load from %s: %s (stub mode)", driver_path, e)
        else:
            log.info("Servo controller in stub mode — set servo.enabled=true in settings.yaml to activate")

    def attach(self, frame_source=None, timelapse_running=None):
        """Give the controller access to the live frame (for thumbnails) and timelapse state."""
        self._frame_source = frame_source
        self._timelapse_running = timelapse_running

    @property
    def enabled(self) -> bool:
        return self._rig is not None

    # ── Views (presets) ────────────────────────────────────────────────────────

    def ptz_state(self) -> dict:
        """Everything the views UI needs in one call."""
        if self._rig is None:
            return {"enabled": False, **({"error": self.error} if self.error else {})}
        r = self._rig.status()
        moving = self._moving_to is not None or self._job is not None or r["moving"]
        views, active = [], None
        for p in self._presets.all():
            thumb = self._presets.thumb_path(p["id"])
            views.append({**p, "thumb": int(thumb.stat().st_mtime) if thumb.exists() else 0})
            if (not moving and active is None and abs(p["pan"] - r["pan_deg"]) <= _AT_VIEW_DEG
                    and abs(p["tilt"] - r["tilt_deg"]) <= _AT_VIEW_DEG):
                active = p["id"]
        return {
            "enabled": True,
            "swipe": SERVO_SWIPE,
            "pan": r["pan_deg"], "tilt": r["tilt_deg"],
            "pan_limit": r["pan_limit_deg"], "tilt_limits": r["tilt_limits_deg"],
            "nudge": {"pan": SERVO_NUDGE_PAN, "tilt": SERVO_NUDGE_TILT},
            "moving": moving,
            "moving_to": self._moving_to if self._moving_to != "nudge" else None,
            "active": active,
            "views": views,
            "timelapse": bool(self._timelapse_running and self._timelapse_running()),
            "error": self._last_error,
        }

    def goto(self, view_id: str) -> dict:
        p = self._require().get(view_id)
        if not p:
            raise KeyError(view_id)
        self._queue(p["pan"], p["tilt"], p["id"])
        return p

    def nudge(self, dpan: float, dtilt: float) -> None:
        """Move by a few degrees from wherever the camera is (or is heading)."""
        self._require()
        with self._job_lock:
            base = self._job or self._dest
        if base is None:
            r = self._rig.status()
            base = (r["pan_deg"], r["tilt_deg"])
        lo, hi = self._rig.tilt_limits
        lim = self._rig.pan_limit
        pan = max(-lim, min(lim, base[0] + float(dpan)))
        tilt = max(lo, min(hi, base[1] + float(dtilt)))
        self._queue(pan, tilt, "nudge")

    def add_view(self, name: str) -> dict:
        """Save the current camera position as a new view."""
        self._wait_idle()
        r = self._require_rig_status()
        p = self._presets.add(name, r["pan_deg"], r["tilt_deg"])
        self._save_thumb(p["id"])
        return p

    def update_view(self, view_id: str, name=None, save_view=False) -> dict:
        """Rename a view and/or re-aim it at the current camera position."""
        self._require()
        pan = tilt = None
        if save_view:
            self._wait_idle()
            r = self._rig.status()
            pan, tilt = r["pan_deg"], r["tilt_deg"]
        p = self._presets.update(view_id, name=name, pan=pan, tilt=tilt)
        if not p:
            raise KeyError(view_id)
        if save_view:
            self._save_thumb(view_id)
        return p

    def delete_view(self, view_id: str) -> bool:
        return self._require().delete(view_id)

    def reorder_views(self, ids) -> list:
        return self._require().reorder(list(ids))

    def thumb_path(self, view_id: str):
        return self._require().thumb_path(view_id)

    # ── Swipe joystick ─────────────────────────────────────────────────────────

    def _stale(self, seq) -> bool:
        """True for a command that arrived after a newer one (requests can overtake each other).

        seq is the page's send time in ms.  Only a reorder within 2 s counts, so
        another device with a slightly different clock is never locked out.
        """
        if seq is None:
            return False
        seq = float(seq)
        if self._seq is not None and 0 < self._seq - seq < 2000:
            return True
        self._seq = seq
        return False

    def move(self, pan: float, tilt: float, seq=None) -> None:
        """Set pan/tilt velocity.  Values clamped to -1.0..1.0; 0 = hold."""
        if self._stale(seq):
            return
        pan  = max(-1.0, min(1.0, float(pan)))
        tilt = max(-1.0, min(1.0, float(tilt)))
        self._vel = (pan, tilt)
        if self._rig is None:
            log.debug("servo.move  pan=%.2f  tilt=%.2f  [stub]", pan, tilt)
            return
        self._rig.set_velocity(pan * self.speed, tilt * self.speed)

    def stop(self, seq=None) -> None:
        """Stop all movement; the rig holds its current position."""
        if self._stale(seq):
            return
        self._vel = (0.0, 0.0)
        if self._rig is None:
            log.debug("servo.stop  [stub]")
            return
        self._rig.stop()

    def status(self) -> dict:
        s = {
            "enabled":  self.enabled,
            "pan_vel":  round(self._vel[0], 3),
            "tilt_vel": round(self._vel[1], 3),
            "pan_pos":  0.0,
            "tilt_pos": 0.0,
        }
        if self.error:
            s["error"] = self.error
        if self._rig is not None:
            r = self._rig.status()
            lo, hi = r["tilt_limits_deg"]
            s.update(r)
            s["pan_pos"]  = round(r["pan_deg"] / r["pan_limit_deg"], 3)
            s["tilt_pos"] = round(r["tilt_deg"] / (hi if r["tilt_deg"] >= 0 else -lo), 3)
        return s

    # ── Internals ──────────────────────────────────────────────────────────────

    def _require(self):
        if self._rig is None:
            raise RuntimeError("no pan/tilt rig on this camera")
        return self._presets

    def _require_rig_status(self):
        self._require()
        return self._rig.status()

    def _queue(self, pan, tilt, view_id):
        with self._job_lock:
            self._job = (pan, tilt, view_id)
        self._last_error = None
        self._job_wake.set()

    def _wait_idle(self, timeout=10.0):
        end = time.monotonic() + timeout
        while (self._job is not None or self._moving_to is not None) and time.monotonic() < end:
            time.sleep(0.05)

    def _worker(self):
        while True:
            self._job_wake.wait()
            self._job_wake.clear()
            with self._job_lock:
                job, self._job = self._job, None
            if job is None:
                continue
            pan, tilt, view_id = job
            self._moving_to, self._dest = view_id, (pan, tilt)
            try:
                self._rig.move_to(pan, tilt)
            except Exception as e:
                self._last_error = str(e)
                log.warning("pan/tilt move failed: %s", e)
            finally:
                self._moving_to = self._dest = None
            # Refresh the view's picture once the camera has settled there,
            # unless another move has already been requested.
            if view_id != "nudge" and not self._last_error:
                time.sleep(_THUMB_DELAY_S)
                if self._job is None:
                    self._save_thumb(view_id)

    def _save_thumb(self, view_id):
        if not self._frame_source:
            return
        try:
            time.sleep(0.2)
            data = self._frame_source()
            if not data:
                return          # camera paused (e.g. night schedule)
            from PIL import Image
            img = Image.open(io.BytesIO(data))
            img.thumbnail((_THUMB_WIDTH, _THUMB_WIDTH))
            path = self._presets.thumb_path(view_id)
            path.parent.mkdir(exist_ok=True)
            tmp = path.with_suffix(".tmp")
            img.save(tmp, "JPEG", quality=78)
            tmp.replace(path)
        except Exception as e:
            log.warning("view thumbnail for %s failed: %s", view_id, e)


servo = ServoController(
    enabled     = SERVO_ENABLED,
    speed       = SERVO_SPEED,
    driver_path = SERVO_DRIVER_PATH,
)
