"""StreamOutput — thread-safe frame buffer with FPS accounting.

Both camera backends write JPEG frames into a single StreamOutput. The
Flask thread reads via `condition.wait()` in `get_stream_frame()`.
"""
import io
import threading
import time

from .backends.ocv_pipeline import apply_ocv, needs_ocv


class StreamOutput(io.BufferedIOBase):
    def __init__(self, cam_ctrl, cam_ctrl_lock):
        self._cam_ctrl = cam_ctrl
        self._cam_ctrl_lock = cam_ctrl_lock
        self.frame = None
        self.last_write = 0.0
        self.condition = threading.Condition()
        self.recorder = None
        self._fps_lock = threading.Lock()
        self._fps_count = 0
        self._fps_ts = time.time()
        self.fps = 0.0

    def write(self, buf):
        # Copy controls under lock, then release BEFORE the CPU-heavy ocv step
        # so Flask threads reading cam_ctrl don't block on image processing.
        with self._cam_ctrl_lock:
            s = {
                k: self._cam_ctrl[k]
                for k in ("tint", "warmth", "hflip", "vflip", "film_filter", "film_strength")
            }
        displayed = apply_ocv(buf, s) if needs_ocv(s) else buf

        with self.condition:
            self.frame = displayed
            self.last_write = time.time()
            self.condition.notify_all()
        if self.recorder:
            self.recorder.write(displayed)

        with self._fps_lock:
            self._fps_count += 1
            now = time.time()
            dt = now - self._fps_ts
            if dt >= 2.0:
                self.fps = round(self._fps_count / dt, 1)
                self._fps_count = 0
                self._fps_ts = now
        return len(buf)
