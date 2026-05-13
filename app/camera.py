"""Camera hardware management — supports picamera2 (CSI) and V4L2/OpenCV (USB)."""
import ctypes, fcntl, io, logging, mmap, os, select, time, threading, subprocess
from datetime import datetime

import cv2
import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from .config import SNAPSHOT_DIR, RESOLUTIONS, CAM_CTRL_DEFAULTS, DEFAULT_RESOLUTION, CAM_BACKEND, STREAM_JPEG_QUALITY, V4L2_MODE
from .film import FILM_FILTERS
from .postprocess import postprocess_jpeg

FPS          = 30
JPEG_QUALITY = 85
V4L2_DEVICE  = "/dev/video0"

# Optional picamera2 import — only needed on CSI camera Pis.
if CAM_BACKEND == "picamera2":
    from picamera2 import Picamera2

# Optional v4l2 ctypes bindings — needed only for the MJPEG passthrough path.
if CAM_BACKEND == "v4l2":
    try:
        import v4l2 as _v4l2
    except ImportError:
        _v4l2 = None

# ── Live camera controls (per-frame effects) ───────────────────────────────────
cam_ctrl      = dict(CAM_CTRL_DEFAULTS)
cam_ctrl_lock = threading.Lock()


# ── OpenCV post-processing (shared by both backends) ──────────────────────────
def _apply_ocv(buf, s):
    """Post-processing: tint shift, flip, film simulation."""
    try:
        arr   = np.frombuffer(buf, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if frame is None:
            return buf

        hf, vf = s.get("hflip", False), s.get("vflip", False)
        if hf and vf:   frame = cv2.flip(frame, -1)
        elif hf:        frame = cv2.flip(frame, 1)
        elif vf:        frame = cv2.flip(frame, 0)

        t = s.get("tint", 0)
        if t:
            strength = abs(t) * 40 // 100
            ch = list(cv2.split(frame.astype(np.int16)))
            if t > 0:
                ch[2] = np.clip(ch[2] + strength, 0, 255)
                ch[0] = np.clip(ch[0] + strength // 2, 0, 255)
                ch[1] = np.clip(ch[1] - strength, 0, 255)
            else:
                ch[1] = np.clip(ch[1] + strength, 0, 255)
                ch[2] = np.clip(ch[2] - strength, 0, 255)
                ch[0] = np.clip(ch[0] - strength // 2, 0, 255)
            frame = cv2.merge([c.astype(np.uint8) for c in ch])

        w = s.get("warmth", 0)
        if w:
            # Warm: boost R, cut B. Cool: boost B, cut R. Max ±30 levels at ±100.
            strength = abs(w) * 30 // 100
            ch = list(cv2.split(frame.astype(np.int16)))
            if w > 0:
                ch[2] = np.clip(ch[2] + strength, 0, 255)   # R up
                ch[0] = np.clip(ch[0] - strength, 0, 255)   # B down
            else:
                ch[2] = np.clip(ch[2] - strength, 0, 255)   # R down
                ch[0] = np.clip(ch[0] + strength, 0, 255)   # B up
            frame = cv2.merge([c.astype(np.uint8) for c in ch])

        ff = s.get("film_filter", "none")
        if ff and ff != "none":
            fd = FILM_FILTERS.get(ff)
            if fd:
                original = frame.copy()
                if fd.get("bw"):
                    w = fd.get("weights")
                    if w:
                        # Weighted channel mix mimicking spectral sensitivity
                        b_f, g_f, r_f = cv2.split(frame.astype(np.float32))
                        gray = np.clip(r_f * w[0] + g_f * w[1] + b_f * w[2],
                                       0, 255).astype(np.uint8)
                    else:
                        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                    gray = fd["curve"][gray]
                    frame = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
                else:
                    b_ch, g_ch, r_ch = cv2.split(frame)
                    frame = cv2.merge([fd["b"][b_ch], fd["g"][g_ch], fd["r"][r_ch]])
                    sm = fd.get("sat", 1.0)
                    if sm != 1.0:
                        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV).astype(np.float32)
                        hsv[:, :, 1] = np.clip(hsv[:, :, 1] * sm, 0, 255)
                        frame = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)
                strength = float(s.get("film_strength", 100)) / 100.0
                if strength < 0.99:
                    alpha = max(0.0, strength)
                    frame = cv2.addWeighted(original, 1.0 - alpha, frame, alpha, 0)

        ok, enc = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        return enc.tobytes() if ok else buf
    except Exception:
        return buf


# ── PIL snapshot filter (shared by both backends) ─────────────────────────────
def _apply_filter(img, name):
    if name == "grayscale":
        return ImageOps.grayscale(img).convert("RGB")
    if name == "sepia":
        gray = ImageOps.grayscale(img)
        return ImageOps.colorize(gray, (100, 55, 10), (255, 235, 170))
    if name == "vivid":
        img = ImageEnhance.Color(img).enhance(1.9)
        img = ImageEnhance.Contrast(img).enhance(1.35)
        return ImageEnhance.Brightness(img).enhance(1.05)
    if name == "soft":
        img = img.filter(ImageFilter.GaussianBlur(radius=2.0))
        img = ImageEnhance.Contrast(img).enhance(0.72)
        return ImageEnhance.Brightness(img).enhance(1.12)
    if name == "sharp":
        img = ImageEnhance.Sharpness(img).enhance(4.0)
        return ImageEnhance.Contrast(img).enhance(1.15)
    return img


# ── Stream output (shared by both backends) ────────────────────────────────────
class StreamOutput(io.BufferedIOBase):
    def __init__(self):
        self.frame      = None
        self.condition  = threading.Condition()
        self.recorder   = None
        self._fps_lock  = threading.Lock()
        self._fps_count = 0
        self._fps_ts    = time.time()
        self.fps        = 0.0

    def write(self, buf):
        with cam_ctrl_lock:
            s = {k: cam_ctrl[k] for k in ("tint", "warmth", "hflip", "vflip", "film_filter", "film_strength")}
        displayed = _apply_ocv(buf, s) if (
            s["tint"] or s["warmth"] or s["hflip"] or s["vflip"] or s["film_filter"] != "none"
        ) else buf

        with self.condition:
            self.frame = displayed
            self.condition.notify_all()
        if self.recorder:
            self.recorder.write(displayed)

        with self._fps_lock:
            self._fps_count += 1
            now = time.time()
            dt  = now - self._fps_ts
            if dt >= 2.0:
                self.fps        = round(self._fps_count / dt, 1)
                self._fps_count = 0
                self._fps_ts    = now


# ── Camera manager ─────────────────────────────────────────────────────────────
class CameraManager:
    def __init__(self):
        self.lock       = threading.Lock()
        self.output     = StreamOutput()
        self.res_key    = DEFAULT_RESOLUTION
        self.resolution = RESOLUTIONS[self.res_key]
        self.model      = "Unknown"
        self._stop      = threading.Event()
        self._restart   = threading.Event()
        # Backend-specific handle (cv2.VideoCapture or Picamera2 instance)
        self._handle    = None
        # Stream enable/disable — hardware keeps running, frames just aren't served
        self.enabled    = True
        threading.Thread(target=self._capture_loop, daemon=True, name="capture").start()

    def set_enabled(self, value: bool) -> None:
        self.enabled = bool(value)

    # ── picamera2 backend ──────────────────────────────────────────────────────

    def _open_picamera2(self):
        w, h = self.resolution
        try:
            picam2 = Picamera2()
            self.model = picam2.camera_properties.get("Model", "Unknown").upper()
            config = picam2.create_video_configuration(
                main={"format": "RGB888", "size": (w, h)},
                controls={"FrameRate": float(FPS)},
                buffer_count=2,
            )
            picam2.configure(config)
            picam2.start()
            return picam2
        except Exception:
            return None

    def _loop_picamera2(self):
        backoff = 1
        while not self._stop.is_set():
            self._restart.clear()
            picam2 = self._open_picamera2()
            if picam2 is None:
                if self._stop.wait(backoff):
                    break
                backoff = min(backoff * 2, 30)
                continue
            backoff = 1
            with self.lock:
                self._handle = picam2
            with cam_ctrl_lock:
                self.apply_isp_controls(dict(cam_ctrl))
            failures = 0
            while not self._stop.is_set() and not self._restart.is_set():
                try:
                    frame = picam2.capture_array("main")
                except Exception:
                    failures += 1
                    if failures >= 5:
                        break
                    time.sleep(0.05)
                    continue
                failures = 0
                ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
                if ok:
                    self.output.write(buf.tobytes())
            try:
                picam2.stop()
                picam2.close()
            except Exception:
                pass
            with self.lock:
                if self._handle is picam2:
                    self._handle = None
            if not self._stop.is_set() and not self._restart.is_set():
                if self._stop.wait(backoff):
                    break
                backoff = min(backoff * 2, 30)

    # ── V4L2 MJPEG passthrough (no decode/encode) ─────────────────────────────

    def _usb_reset_camera(self):
        """Reset the C930e via USBDEVFS_RESET to recover from firmware hangs.

        The C930e can silently stop sending frames after STREAMOFF without
        disconnecting from USB. A soft reset restores it without a physical
        replug. Requires plugdev write access to /dev/bus/usb (set by udev rule
        in /etc/udev/rules.d/99-c930e-power.rules).
        """
        USBDEVFS_RESET = 0x5514
        try:
            for entry in os.scandir("/sys/bus/usb/devices"):
                try:
                    vendor  = open(os.path.join(entry.path, "idVendor")).read().strip()
                    product = open(os.path.join(entry.path, "idProduct")).read().strip()
                except OSError:
                    continue
                if vendor == "046d" and product == "0843":
                    busnum = int(open(os.path.join(entry.path, "busnum")).read().strip())
                    devnum = int(open(os.path.join(entry.path, "devnum")).read().strip())
                    dev_path = f"/dev/bus/usb/{busnum:03d}/{devnum:03d}"
                    fd = os.open(dev_path, os.O_WRONLY)
                    try:
                        fcntl.ioctl(fd, USBDEVFS_RESET, 0)
                        logging.info("v4l2 passthrough: USB reset sent to %s", dev_path)
                    finally:
                        os.close(fd)
                    time.sleep(3)
                    return
            logging.warning("v4l2 passthrough: C930e not found in sysfs for USB reset")
        except OSError as e:
            logging.warning("v4l2 passthrough: USB reset failed: %s", e)

    def _loop_v4l2_passthrough(self):
        """Capture raw MJPEG bytes via V4L2 mmap streaming — no decode/encode.

        The C930e sends hardware-compressed MJPEG over USB. This path maps the
        driver buffers directly and copies bytes into StreamOutput, bypassing
        the cv2.VideoCapture decode→encode round trip that costs ~75% of a core.
        Returns False only when MJPG format is permanently rejected by the driver.
        """
        NUM_BUFS    = 4
        backoff     = 1
        invalid_run = 0
        LOG_EVERY   = 50
        SELECT_TIMEOUT_LIMIT = 5   # seconds of no frames → camera is hung

        while not self._stop.is_set():
            self._restart.clear()
            fd    = None
            mmaps = []
            try:
                # Apply hardware controls via subprocess before opening the fd.
                # auto_exposure=3 = aperture priority (auto); without this the
                # camera may default to manual exposure and appear overexposed.
                with cam_ctrl_lock:
                    ctrl = dict(cam_ctrl)
                exp = int(ctrl.get("exposure_time", 0))
                if exp > 0:
                    subprocess.run(
                        ["v4l2-ctl", "-d", V4L2_DEVICE,
                         f"--set-ctrl=auto_exposure=1,exposure_time_absolute={max(3, exp // 100)}"],
                        capture_output=True, check=False,
                    )
                else:
                    subprocess.run(
                        ["v4l2-ctl", "-d", V4L2_DEVICE, "--set-ctrl=auto_exposure=3"],
                        capture_output=True, check=False,
                    )
                subprocess.run(
                    ["v4l2-ctl", "-d", V4L2_DEVICE, "--set-ctrl=exposure_dynamic_framerate=0"],
                    capture_output=True, check=False,
                )
                if ctrl.get("awb_mode", "auto") == "auto":
                    subprocess.run(
                        ["v4l2-ctl", "-d", V4L2_DEVICE, "--set-ctrl=white_balance_automatic=1"],
                        capture_output=True, check=False,
                    )
                else:
                    kelvin = max(2000, min(7500, int(ctrl.get("awb_kelvin", 5600))))
                    subprocess.run(
                        ["v4l2-ctl", "-d", V4L2_DEVICE,
                         f"--set-ctrl=white_balance_automatic=0,white_balance_temperature={kelvin}"],
                        capture_output=True, check=False,
                    )

                fd = os.open(V4L2_DEVICE, os.O_RDWR)
                w, h = self.resolution

                # ── Set frame interval BEFORE format (UVC requires this order) ─
                # VIDIOC_S_PARM before S_FMT so the kernel embeds the frame
                # interval in the UVC probe-commit negotiation at VIDIOC_REQBUFS.
                parm = _v4l2.v4l2_streamparm()
                parm.type = _v4l2.V4L2_BUF_TYPE_VIDEO_CAPTURE
                parm.parm.capture.timeperframe.numerator   = 1
                parm.parm.capture.timeperframe.denominator = FPS
                try:
                    fcntl.ioctl(fd, _v4l2.VIDIOC_S_PARM, parm)
                    n = parm.parm.capture.timeperframe.numerator
                    d = parm.parm.capture.timeperframe.denominator
                    actual_fps = d / max(n, 1)
                    if d and abs(actual_fps - FPS) > 0.5:
                        logging.info(
                            "v4l2 passthrough: fps negotiated to %.1f (requested %d)", actual_fps, FPS,
                        )
                except OSError:
                    pass

                # ── Set MJPEG format ───────────────────────────────────────────
                fmt = _v4l2.v4l2_format()
                fmt.type = _v4l2.V4L2_BUF_TYPE_VIDEO_CAPTURE
                fmt.fmt.pix.width       = w
                fmt.fmt.pix.height      = h
                fmt.fmt.pix.pixelformat = _v4l2.V4L2_PIX_FMT_MJPEG
                fcntl.ioctl(fd, _v4l2.VIDIOC_S_FMT, fmt)

                if fmt.fmt.pix.pixelformat != _v4l2.V4L2_PIX_FMT_MJPEG:
                    logging.error("v4l2 passthrough: camera rejected MJPG format — falling back to opencv")
                    return False

                actual_w = fmt.fmt.pix.width
                actual_h = fmt.fmt.pix.height
                if (actual_w, actual_h) != (w, h):
                    logging.warning(
                        "v4l2 passthrough: resolution negotiated to %dx%d (requested %dx%d)",
                        actual_w, actual_h, w, h,
                    )
                    self.resolution = (actual_w, actual_h)

                # ── Allocate mmap buffers ──────────────────────────────────────
                req = _v4l2.v4l2_requestbuffers()
                req.count  = NUM_BUFS
                req.type   = _v4l2.V4L2_BUF_TYPE_VIDEO_CAPTURE
                req.memory = _v4l2.V4L2_MEMORY_MMAP
                fcntl.ioctl(fd, _v4l2.VIDIOC_REQBUFS, req)

                for i in range(req.count):
                    qbuf = _v4l2.v4l2_buffer()
                    qbuf.type   = _v4l2.V4L2_BUF_TYPE_VIDEO_CAPTURE
                    qbuf.memory = _v4l2.V4L2_MEMORY_MMAP
                    qbuf.index  = i
                    fcntl.ioctl(fd, _v4l2.VIDIOC_QUERYBUF, qbuf)
                    m = mmap.mmap(
                        fd, qbuf.length,
                        flags=mmap.MAP_SHARED,
                        prot=mmap.PROT_READ,
                        offset=qbuf.m.offset,
                    )
                    mmaps.append(m)
                    fcntl.ioctl(fd, _v4l2.VIDIOC_QBUF, qbuf)

                # ── Start streaming ────────────────────────────────────────────
                buf_type = ctypes.c_uint32(_v4l2.V4L2_BUF_TYPE_VIDEO_CAPTURE)
                fcntl.ioctl(fd, _v4l2.VIDIOC_STREAMON, buf_type)

                backoff = 1
                print(
                    f"Camera capture: v4l2 passthrough (mmap), {V4L2_DEVICE}, "
                    f"{actual_w}x{actual_h} @ {FPS}fps MJPEG",
                    flush=True,
                )
                with cam_ctrl_lock:
                    self.apply_isp_controls(dict(cam_ctrl))

                # ── Capture loop ───────────────────────────────────────────────
                buf = _v4l2.v4l2_buffer()
                buf.type   = _v4l2.V4L2_BUF_TYPE_VIDEO_CAPTURE
                buf.memory = _v4l2.V4L2_MEMORY_MMAP

                select_timeouts = 0
                while not self._stop.is_set() and not self._restart.is_set():
                    ready, _, _ = select.select([fd], [], [], 1.0)
                    if not ready:
                        select_timeouts += 1
                        if select_timeouts >= SELECT_TIMEOUT_LIMIT:
                            raise OSError(
                                f"no frames for {select_timeouts}s — camera firmware hung"
                            )
                        continue
                    select_timeouts = 0
                    fcntl.ioctl(fd, _v4l2.VIDIOC_DQBUF, buf)
                    # Capture bytesused and index NOW — VIDIOC_QBUF zeroes bytesused.
                    bytesused = buf.bytesused
                    frame     = bytes(mmaps[buf.index][:bytesused])
                    fcntl.ioctl(fd, _v4l2.VIDIOC_QBUF, buf)

                    if bytesused < 4 or frame[:2] != b'\xff\xd8' or frame[-2:] != b'\xff\xd9':
                        invalid_run += 1
                        if invalid_run % LOG_EVERY == 1:
                            logging.warning(
                                "v4l2 passthrough: %d invalid MJPEG frame(s) — dropped", invalid_run,
                            )
                        continue
                    invalid_run = 0
                    self.output.write(frame)

            except OSError as e:
                logging.error("v4l2 passthrough error: %s", e)
                self._usb_reset_camera()
            finally:
                if fd is not None:
                    try:
                        fcntl.ioctl(fd, _v4l2.VIDIOC_STREAMOFF,
                                    ctypes.c_uint32(_v4l2.V4L2_BUF_TYPE_VIDEO_CAPTURE))
                    except OSError:
                        pass
                    for m in mmaps:
                        try:
                            m.close()
                        except Exception:
                            pass
                    try:
                        os.close(fd)
                    except OSError:
                        pass

            if not self._stop.is_set() and not self._restart.is_set():
                if self._stop.wait(backoff):
                    break
                backoff = min(backoff * 2, 30)

        return True

    # ── V4L2 / OpenCV backend ──────────────────────────────────────────────────

    def _open_v4l2(self):
        w, h = self.resolution
        cap = cv2.VideoCapture(V4L2_DEVICE, cv2.CAP_V4L2)
        if not cap.isOpened():
            cap.release()
            return None
        # Try MJPEG first (lower CPU); fall back to YUYV if the device no longer
        # advertises it (e.g. after a firmware/kernel update on the C930e).
        mjpg = cv2.VideoWriter_fourcc(*"MJPG")
        cap.set(cv2.CAP_PROP_FOURCC, mjpg)
        actual = int(cap.get(cv2.CAP_PROP_FOURCC))
        if actual != mjpg:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"YUYV"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        cap.set(cv2.CAP_PROP_FPS,          FPS)
        cap.set(cv2.CAP_PROP_BUFFERSIZE,   1)
        subprocess.run(
            ["v4l2-ctl", "-d", V4L2_DEVICE, "--set-ctrl=exposure_dynamic_framerate=0"],
            capture_output=True, check=False,
        )
        self.model = "C930e"
        return cap

    def _loop_v4l2(self):
        backoff = 1
        while not self._stop.is_set():
            self._restart.clear()
            cap = self._open_v4l2()
            if cap is None:
                if self._stop.wait(backoff):
                    break
                backoff = min(backoff * 2, 30)
                continue
            backoff = 1
            with self.lock:
                self._handle = cap
            with cam_ctrl_lock:
                self.apply_isp_controls(dict(cam_ctrl))
            failures = 0
            while not self._stop.is_set() and not self._restart.is_set():
                ret, frame = cap.read()
                if not ret or frame is None:
                    failures += 1
                    if failures >= 5:
                        break
                    time.sleep(0.05)
                    continue
                failures = 0
                ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
                if ok:
                    self.output.write(buf.tobytes())
            cap.release()
            with self.lock:
                if self._handle is cap:
                    self._handle = None
            if not self._stop.is_set() and not self._restart.is_set():
                if self._stop.wait(backoff):
                    break
                backoff = min(backoff * 2, 30)

    # ── Shared entry point ─────────────────────────────────────────────────────

    def _capture_loop(self):
        if CAM_BACKEND == "picamera2":
            self._loop_picamera2()
        elif V4L2_MODE == "passthrough" and _v4l2 is not None:
            if not self._loop_v4l2_passthrough():
                print("Camera: MJPG passthrough unavailable, falling back to opencv", flush=True)
                self._loop_v4l2()
        else:
            self._loop_v4l2()

    # ── Resolution change ──────────────────────────────────────────────────────

    def set_resolution(self, res_key):
        if res_key not in RESOLUTIONS or res_key == self.res_key:
            return False
        with self.lock:
            self.res_key    = res_key
            self.resolution = RESOLUTIONS[res_key]
        self._restart.set()
        return True

    # ── Live-stream frame (reduced quality for bandwidth) ─────────────────────

    def get_stream_frame(self):
        """Return the current frame, optionally re-encoded at STREAM_JPEG_QUALITY.

        In V4L2 passthrough mode the frame is raw camera MJPEG; decoding and
        re-encoding it here would defeat the passthrough and add ~75ms of latency
        per request, so we skip the quality reduction and serve the native MJPEG.
        In OpenCV mode (and picamera2) the encode already happens in the capture
        loop, so re-encoding at a lower quality is cheap enough to be worthwhile.

        Photos, video, and timelapse all read self.output.frame directly and
        are unaffected — they always get the full JPEG_QUALITY (85) buffer.
        """
        with self.output.condition:
            self.output.condition.wait(timeout=2)
            frame = self.output.frame
        if not frame or STREAM_JPEG_QUALITY >= JPEG_QUALITY or V4L2_MODE == "passthrough":
            return frame
        arr = np.frombuffer(frame, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return frame
        ok, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, STREAM_JPEG_QUALITY])
        return enc.tobytes() if ok else frame

    # ── Snapshot ───────────────────────────────────────────────────────────────

    def capture(self, prefix="Photo", filter_name="none", quality=85):
        now     = datetime.now()
        ts      = now.strftime("%Y-%m-%d_%H-%M-%S")
        exif_dt = now.strftime("%Y:%m:%d %H:%M:%S")
        filename = f"{prefix}_{ts}.jpg"
        path = SNAPSHOT_DIR / filename
        with self.output.condition:
            self.output.condition.wait(timeout=3)
            frame = self.output.frame
        if not frame:
            return None
        img = Image.open(io.BytesIO(frame))
        if filter_name and filter_name != "none":
            img = _apply_filter(img, filter_name)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        path.write_bytes(buf.getvalue())

        with cam_ctrl_lock:
            ctrl = dict(cam_ctrl)
        meta = {
            "datetime":     exif_dt,
            "make":         "Raspberry Pi" if CAM_BACKEND == "picamera2" else "Logitech",
            "model":        self.model,
            "description":  "Garden Monitor Timelapse Frame" if prefix == "tl"
                            else "Garden Monitor Photo",
            "hflip":        ctrl.get("hflip", False),
            "vflip":        ctrl.get("vflip", False),
            "exposure_mode": 1 if ctrl.get("exposure_time", 0) > 0 else 0,
            "white_balance": 1 if ctrl.get("awb_mode", "auto") != "auto" else 0,
        }

        if prefix == "tl":
            threading.Thread(
                target=postprocess_jpeg, args=(path, quality),
                kwargs={"fast": True, "metadata": meta}, daemon=True
            ).start()
        else:
            postprocess_jpeg(path, quality, metadata=meta)
        return filename

    # ── ISP controls ───────────────────────────────────────────────────────────

    def apply_isp_controls(self, c):
        with self.lock:
            handle = self._handle
        if handle is None:
            if CAM_BACKEND == "v4l2" and V4L2_MODE == "passthrough":
                self._isp_v4l2_passthrough(c)
            return

        if CAM_BACKEND == "picamera2":
            self._isp_picamera2(handle, c)
        else:
            self._isp_v4l2(handle, c)

    def _isp_picamera2(self, picam2, c):
        """picamera2 ISP: Brightness −1…1, Saturation 0…2, Sharpness 0…16, Contrast 0…32."""
        try:
            controls = {}
            exp = int(c.get("exposure_time", 0))
            controls["AeEnable"]    = exp <= 0
            if exp > 0:
                controls["ExposureTime"] = exp
            gain = float(c.get("analogue_gain", 0.0))
            if gain > 0:
                controls["AnalogueGain"] = gain
            if c.get("awb_mode", "auto") == "auto":
                controls["AwbEnable"] = True
            else:
                controls["AwbEnable"]        = False
                controls["ColourTemperature"] = max(2000, min(7500, int(c.get("awb_kelvin", 5600))))
            controls["Brightness"] = max(-1.0, min(1.0,  int(c.get("brightness", 0)) / 100.0))
            controls["Saturation"] = max( 0.0, min(2.0,  1.0 + int(c.get("saturation", 0)) / 100.0))
            controls["Sharpness"]  = max( 0.0, min(16.0, float(c.get("sharpness", 1.0))))
            controls["Contrast"]   = max( 0.0, min(32.0, float(c.get("contrast",  1.0))))
            # Autofocus (IMX708 / Camera Module 3 only — silently ignored on other sensors)
            _af_mode  = {"continuous": 2, "auto": 1, "manual": 0}
            _af_range = {"normal": 0, "macro": 1, "full": 2}
            controls["AfMode"]  = _af_mode.get( c.get("af_mode",  "continuous"), 2)
            controls["AfRange"] = _af_range.get(c.get("af_range", "normal"),     0)
            picam2.set_controls(controls)
        except Exception:
            pass

    def _isp_v4l2(self, cap, c):
        """V4L2 ISP via OpenCV properties and v4l2-ctl for white balance."""
        try:
            exp = int(c.get("exposure_time", 0))
            if exp > 0:
                cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1)
                cap.set(cv2.CAP_PROP_EXPOSURE, max(3, exp // 100))
            else:
                cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 3)

            gain = float(c.get("analogue_gain", 0.0))
            if gain > 0:
                cap.set(cv2.CAP_PROP_GAIN, int(min(255, max(0, gain / 16.0 * 255))))

            if c.get("awb_mode", "auto") == "auto":
                subprocess.run(
                    ["v4l2-ctl", "-d", V4L2_DEVICE, "--set-ctrl=white_balance_automatic=1"],
                    capture_output=True, check=False,
                )
            else:
                kelvin = max(2000, min(7500, int(c.get("awb_kelvin", 5600))))
                subprocess.run(
                    ["v4l2-ctl", "-d", V4L2_DEVICE,
                     f"--set-ctrl=white_balance_automatic=0,white_balance_temperature={kelvin}"],
                    capture_output=True, check=False,
                )

            b = int(c.get("brightness", 0))
            cap.set(cv2.CAP_PROP_BRIGHTNESS, max(0, min(255, 128 + b * 127 // 100)))
            sat = int(c.get("saturation", 0))
            cap.set(cv2.CAP_PROP_SATURATION, max(0, min(255, 128 + sat * 127 // 100)))
            sharp = float(c.get("sharpness", 1.0))
            sv = int(sharp * 128) if sharp <= 1.0 else int(128 + (sharp - 1.0) / 3.0 * 127)
            cap.set(cv2.CAP_PROP_SHARPNESS, max(0, min(255, sv)))
            contrast = float(c.get("contrast", 1.0))
            cv_val = int(contrast * 128) if contrast <= 1.0 else int(128 + (contrast - 1.0) / 3.0 * 127)
            cap.set(cv2.CAP_PROP_CONTRAST, max(0, min(255, cv_val)))
        except Exception:
            pass

    def _isp_v4l2_passthrough(self, c):
        """Apply hardware V4L2 controls via v4l2-ctl for the MJPEG passthrough path.

        The passthrough loop has no cv2.VideoCapture handle, so we must use
        v4l2-ctl subprocess calls instead of cap.set().  Uses the same value
        mapping as _isp_v4l2() so behaviour is identical to the OpenCV path.
        """
        try:
            exp = int(c.get("exposure_time", 0))
            if exp > 0:
                subprocess.run(
                    ["v4l2-ctl", "-d", V4L2_DEVICE,
                     f"--set-ctrl=auto_exposure=1,exposure_time_absolute={max(3, exp // 100)}"],
                    capture_output=True, check=False,
                )
            else:
                subprocess.run(
                    ["v4l2-ctl", "-d", V4L2_DEVICE, "--set-ctrl=auto_exposure=3"],
                    capture_output=True, check=False,
                )

            gain = float(c.get("analogue_gain", 0.0))
            if gain > 0:
                gain_v4l2 = int(min(255, max(0, gain / 16.0 * 255)))
                subprocess.run(
                    ["v4l2-ctl", "-d", V4L2_DEVICE, f"--set-ctrl=gain={gain_v4l2}"],
                    capture_output=True, check=False,
                )

            if c.get("awb_mode", "auto") == "auto":
                subprocess.run(
                    ["v4l2-ctl", "-d", V4L2_DEVICE, "--set-ctrl=white_balance_automatic=1"],
                    capture_output=True, check=False,
                )
            else:
                kelvin = max(2000, min(7500, int(c.get("awb_kelvin", 5600))))
                subprocess.run(
                    ["v4l2-ctl", "-d", V4L2_DEVICE,
                     f"--set-ctrl=white_balance_automatic=0,white_balance_temperature={kelvin}"],
                    capture_output=True, check=False,
                )

            b     = int(c.get("brightness", 0))
            sat   = int(c.get("saturation", 0))
            sharp = float(c.get("sharpness", 1.0))
            sv    = int(sharp * 128) if sharp <= 1.0 else int(128 + (sharp - 1.0) / 3.0 * 127)
            ctr   = float(c.get("contrast", 1.0))
            cv    = int(ctr * 128) if ctr <= 1.0 else int(128 + (ctr - 1.0) / 3.0 * 127)
            bc    = max(0, min(1, int(c.get("backlight_compensation", 0))))
            subprocess.run(
                ["v4l2-ctl", "-d", V4L2_DEVICE,
                 f"--set-ctrl=brightness={max(0, min(255, 128 + b * 127 // 100))},"
                 f"saturation={max(0, min(255, 128 + sat * 127 // 100))},"
                 f"sharpness={max(0, min(255, sv))},"
                 f"contrast={max(0, min(255, cv))},"
                 f"backlight_compensation={bc}"],
                capture_output=True, check=False,
            )
        except Exception:
            pass

    # ── Shutdown ───────────────────────────────────────────────────────────────

    def stop(self):
        self._stop.set()
        self._restart.set()
        with self.lock:
            handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if CAM_BACKEND == "picamera2":
                handle.stop()
                handle.close()
            else:
                handle.release()
        except Exception:
            pass


camera = CameraManager()
