"""CameraManager — orchestrates the backend, still-capture handshake, and
snapshot I/O. Public interface preserved from the v1 monolith so app.py,
timelapse.py, and stats.py need no changes.
"""
import io
import logging
import threading
import time
from datetime import datetime

import numpy as np
import cv2
from PIL import Image

from .config import (
    SNAPSHOT_DIR, RESOLUTIONS, CAM_CTRL_DEFAULTS,
    DEFAULT_RESOLUTION, CAM_BACKEND, V4L2_MODE, STREAM_JPEG_QUALITY,
    STREAM_QUALITY_WIFI, STREAM_QUALITY_ETHERNET, ETHERNET_INTERFACE, LINK_CHECK_S,
)
from .backends import ocv_pipeline, picamera2_backend, v4l2_opencv, v4l2_passthrough
from .postprocess import postprocess_jpeg
from .stream import StreamOutput


# JPEG re-encode quality for effects + v4l2 snapshots (baseline).
JPEG_QUALITY = 85
STILL_CAPTURE_TIMEOUT_S = 5   # was 15 — cron retries in 3 min so fail fast


def _link_type(iface):
    """"ethernet" when the cable is in and the port has an IPv4 address, else "wifi"."""
    try:
        with open(f"/sys/class/net/{iface}/carrier") as f:
            if f.read().strip() != "1":
                return "wifi"
        import fcntl, socket, struct
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            fcntl.ioctl(sock.fileno(), 0x8915, struct.pack("256s", iface.encode()[:15]))  # SIOCGIFADDR
        return "ethernet"
    except OSError:
        return "wifi"


def _quality_for(link):
    return STREAM_QUALITY_ETHERNET if link == "ethernet" else STREAM_QUALITY_WIFI


# Module-level shared state — kept at this scope for backward compatibility
# with `from .camera import camera, cam_ctrl, cam_ctrl_lock`.
cam_ctrl = dict(CAM_CTRL_DEFAULTS)
cam_ctrl_lock = threading.Lock()


class CameraManager:
    def __init__(self):
        self.cam_ctrl = cam_ctrl
        self.cam_ctrl_lock = cam_ctrl_lock

        self.res_key = DEFAULT_RESOLUTION
        self.resolution = RESOLUTIONS[self.res_key]
        self.model = "Unknown"
        self.enabled = True
        self.output = StreamOutput(cam_ctrl, cam_ctrl_lock)

        self._stop = threading.Event()
        self._restart = threading.Event()

        # Backend-owned handle (Picamera2 instance or cv2.VideoCapture).
        self._handle = None
        self._handle_lock = threading.Lock()

        # picamera2 still-capture handshake: Flask thread sets _still_request,
        # loop thread executes the capture and sets _still_done.
        # _still_lock serialises concurrent snapshot requests.
        self._still_lock = threading.Lock()
        self._still_request = threading.Event()
        self._still_done = threading.Event()
        self._still_img = None

        # Timelapse focus lock: while True the picamera2 backend keeps the lens
        # in manual (AfMode 0) even when it re-applies the user's controls after
        # each still, which used to switch continuous AF back on after frame 1.
        self.af_locked = False

        # Live-feed JPEG quality follows the network link: higher on ethernet.
        self.link = _link_type(ETHERNET_INTERFACE)
        self.stream_quality = _quality_for(self.link)

        threading.Thread(target=self._capture_loop, daemon=True, name="capture").start()
        if CAM_BACKEND == "picamera2":
            threading.Thread(target=self._watch_link, daemon=True, name="link-watch").start()

    def _watch_link(self):
        """Re-pick the stream quality when ethernet is plugged in or pulled."""
        while not self._stop.wait(LINK_CHECK_S):
            link = _link_type(ETHERNET_INTERFACE)
            if link == self.link:
                continue
            q = _quality_for(link)
            logging.getLogger(__name__).warning(
                "network link %s -> %s: live-feed JPEG quality %d -> %d",
                self.link, link, self.stream_quality, q)
            changed = q != self.stream_quality
            self.link, self.stream_quality = link, q
            if changed:
                self._restart.set()     # the capture loop rebuilds the encoder (~1 s blip)

    # ── Handle helpers (backends call these) ───────────────────────────────
    def set_handle(self, handle):
        with self._handle_lock:
            self._handle = handle

    def clear_handle(self, handle=None):
        with self._handle_lock:
            if handle is None or self._handle is handle:
                self._handle = None

    def get_handle(self):
        with self._handle_lock:
            return self._handle

    # ── Enable / disable stream serving (hardware keeps running) ───────────
    def set_enabled(self, value):
        self.enabled = bool(value)

    # ── Resolution change ──────────────────────────────────────────────────
    def set_resolution(self, res_key):
        if res_key not in RESOLUTIONS or res_key == self.res_key:
            return False
        with self._handle_lock:
            self.res_key = res_key
            self.resolution = RESOLUTIONS[res_key]
        self._restart.set()
        return True

    # ── Live-stream frame (Flask reads this) ───────────────────────────────
    def get_stream_frame(self):
        with self.output.condition:
            self.output.condition.wait(timeout=2)
            frame = self.output.frame
        if not frame:
            return frame
        if (CAM_BACKEND == "picamera2"
                or STREAM_JPEG_QUALITY >= JPEG_QUALITY
                or V4L2_MODE == "passthrough"):
            return frame
        arr = np.frombuffer(frame, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return frame
        ok, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, STREAM_JPEG_QUALITY])
        return enc.tobytes() if ok else frame

    # ── Snapshot ───────────────────────────────────────────────────────────
    def capture(self, prefix="Photo", filter_name="none", quality=85):
        now = datetime.now()
        ts = now.strftime("%Y-%m-%d_%H-%M-%S")
        exif_dt = now.strftime("%Y:%m:%d %H:%M:%S")
        filename = f"{prefix}_{ts}.jpg"
        path = SNAPSHOT_DIR / filename

        if CAM_BACKEND == "picamera2":
            with self._still_lock:
                if self.get_handle() is None:
                    return None
                self._still_img = None
                self._still_done.clear()
                self._still_request.set()
                if not self._still_done.wait(timeout=STILL_CAPTURE_TIMEOUT_S):
                    logging.warning("capture: still-capture timeout — loop wedged")
                    return None
                img = self._still_img
            if img is None:
                return None
            # Full-sensor still: always save at maximum JPEG quality.
            save_quality = 95
        else:
            with self.output.condition:
                self.output.condition.wait(timeout=3)
                frame = self.output.frame
                # If the capture loop is dead, self.frame is whatever was last
                # written — possibly days old. The v1 monolith learned this
                # from the 2026-06 C930e freeze that produced two weeks of
                # byte-identical "snapshots". Refuse rather than write stale.
                stale = (time.time() - self.output.last_write) > 10
            if not frame or stale:
                logging.error("capture: stream stalled (no fresh frame in 10s) — snapshot refused")
                return None
            img = Image.open(io.BytesIO(frame))
            save_quality = quality

        if filter_name and filter_name != "none":
            img = ocv_pipeline.apply_pil_filter(img, filter_name)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=save_quality)
        path.write_bytes(buf.getvalue())

        with self.cam_ctrl_lock:
            ctrl = dict(self.cam_ctrl)
        meta = {
            "datetime": exif_dt,
            "make": "Raspberry Pi" if CAM_BACKEND == "picamera2" else "Logitech",
            "model": self.model,
            "description": "Home Garden Cameras Timelapse Frame" if prefix == "tl"
                          else "Home Garden Cameras Photo",
            "hflip": ctrl.get("hflip", False),
            "vflip": ctrl.get("vflip", False),
            "exposure_mode": 1 if ctrl.get("exposure_time", 0) > 0 else 0,
            "white_balance": 1 if ctrl.get("awb_mode", "auto") != "auto" else 0,
        }

        if prefix == "tl":
            threading.Thread(
                target=postprocess_jpeg, args=(path, save_quality),
                kwargs={"fast": True, "metadata": meta}, daemon=True,
            ).start()
        else:
            postprocess_jpeg(path, save_quality, metadata=meta)
        return filename

    # ── ISP controls (routed to the active backend) ────────────────────────
    def apply_isp_controls(self, c):
        handle = self.get_handle()
        if handle is None:
            if CAM_BACKEND == "v4l2" and V4L2_MODE == "passthrough":
                v4l2_passthrough.apply_controls(None, c)
            return
        if CAM_BACKEND == "picamera2":
            picamera2_backend.apply_controls(handle, c, af_locked=self.af_locked)
        else:
            v4l2_opencv.apply_controls(handle, c)

    # ── Backend selection ──────────────────────────────────────────────────
    def _capture_loop(self):
        if CAM_BACKEND == "picamera2":
            picamera2_backend.run(self)
        elif V4L2_MODE == "passthrough" and v4l2_passthrough.available():
            if not v4l2_passthrough.run(self):
                logging.info("v4l2 passthrough unavailable — falling back to opencv")
                v4l2_opencv.run(self)
        else:
            v4l2_opencv.run(self)

    # ── Shutdown ───────────────────────────────────────────────────────────
    def stop(self):
        self._stop.set()
        self._restart.set()
        with self._handle_lock:
            handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if CAM_BACKEND == "picamera2":
                try:
                    handle.stop_recording()
                except Exception:
                    pass
                handle.close()
            else:
                handle.release()
        except Exception:
            pass


camera = CameraManager()
