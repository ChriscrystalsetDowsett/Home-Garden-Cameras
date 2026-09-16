"""V4L2 MJPEG passthrough — mmap driver buffers straight into StreamOutput,
skipping the cv2.VideoCapture decode→encode round trip.

Used for the Logitech C930e which delivers hardware-compressed MJPEG. Reduces
CPU by ~75% of a core vs the OpenCV fallback. Includes content-freeze
detection (byte-identical frames → USB reset) because the C930e can hang
without disconnecting.
"""
import ctypes
import fcntl
import hashlib
import logging
import mmap
import os
import select
import subprocess
import time

from ..config import CAM_USB_ID
from .v4l2_helpers import find_usb_device, usb_reset


try:
    import v4l2 as _v4l2
except ImportError:
    _v4l2 = None


FPS = 24
V4L2_DEVICE = "/dev/video0"
NUM_BUFS = 4
LOG_EVERY = 50
SELECT_TIMEOUT_LIMIT = 5   # seconds of no frames → camera is hung
CONTENT_FREEZE_LIMIT = 60  # consecutive identical frames (~2.5 s at 24 fps)


def available():
    return _v4l2 is not None


def _pre_open_hw_controls(ctrl):
    """Apply hardware controls via v4l2-ctl before opening the device fd.

    auto_exposure=3 = aperture priority (auto); without this the camera may
    default to manual exposure and appear overexposed.
    """
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


def apply_controls(_handle, c):
    """Apply hardware V4L2 controls via v4l2-ctl. `_handle` unused (this path has no cv2 cap)."""
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

        b = int(c.get("brightness", 0))
        sat = int(c.get("saturation", 0))
        sharp = float(c.get("sharpness", 1.0))
        sv = int(sharp * 128) if sharp <= 1.0 else int(128 + (sharp - 1.0) / 3.0 * 127)
        ctr = float(c.get("contrast", 1.0))
        cv_ = int(ctr * 128) if ctr <= 1.0 else int(128 + (ctr - 1.0) / 3.0 * 127)
        bc = max(0, min(1, int(c.get("backlight_compensation", 0))))
        subprocess.run(
            ["v4l2-ctl", "-d", V4L2_DEVICE,
             f"--set-ctrl=brightness={max(0, min(255, 128 + b * 127 // 100))},"
             f"saturation={max(0, min(255, 128 + sat * 127 // 100))},"
             f"sharpness={max(0, min(255, sv))},"
             f"contrast={max(0, min(255, cv_))},"
             f"backlight_compensation={bc},"
             f"exposure_dynamic_framerate=0"],
            capture_output=True, check=False,
        )
    except Exception as e:
        logging.debug("v4l2 passthrough apply_controls failed: %s", e)


def run(manager):
    """Blocking capture loop — mmap the C930e's MJPEG stream.

    Returns False if the driver ultimately rejects MJPG so the manager can
    fall back to the OpenCV path.
    """
    backoff = 1
    invalid_run = 0

    while not manager._stop.is_set():
        manager._restart.clear()
        fd = None
        mmaps = []
        try:
            # Verify USB presence before opening /dev/video0 (better error than the ioctl).
            if find_usb_device(CAM_USB_ID) is None:
                raise OSError(f"{CAM_USB_ID} not found in USB sysfs — camera absent")

            with manager.cam_ctrl_lock:
                ctrl = dict(manager.cam_ctrl)
            _pre_open_hw_controls(ctrl)

            fd = os.open(V4L2_DEVICE, os.O_RDWR)
            w, h = manager.resolution

            # ── Set frame interval BEFORE format (UVC requires this order) ─
            parm = _v4l2.v4l2_streamparm()
            parm.type = _v4l2.V4L2_BUF_TYPE_VIDEO_CAPTURE
            parm.parm.capture.timeperframe.numerator = 1
            parm.parm.capture.timeperframe.denominator = FPS
            try:
                fcntl.ioctl(fd, _v4l2.VIDIOC_S_PARM, parm)
                n = parm.parm.capture.timeperframe.numerator
                d = parm.parm.capture.timeperframe.denominator
                actual_fps = d / max(n, 1)
                if d and abs(actual_fps - FPS) > 0.5:
                    logging.info(
                        "v4l2 passthrough: fps negotiated to %.1f (requested %d)",
                        actual_fps, FPS,
                    )
            except OSError:
                pass

            # ── Set MJPEG format ───────────────────────────────────────────
            fmt = _v4l2.v4l2_format()
            fmt.type = _v4l2.V4L2_BUF_TYPE_VIDEO_CAPTURE
            fmt.fmt.pix.width = w
            fmt.fmt.pix.height = h
            fmt.fmt.pix.pixelformat = _v4l2.V4L2_PIX_FMT_MJPEG
            fcntl.ioctl(fd, _v4l2.VIDIOC_S_FMT, fmt)

            # VIDIOC_S_FMT resets exposure_time_absolute to 250 (~25ms).
            # Seed manual 60 (~6ms) so AE converges up from near-correct
            # rather than down from 250, eliminating the overexposure window.
            subprocess.run(
                ["v4l2-ctl", f"--device={V4L2_DEVICE}",
                 "--set-ctrl=auto_exposure=1",
                 "--set-ctrl=exposure_time_absolute=60"],
                capture_output=True,
            )

            if fmt.fmt.pix.pixelformat != _v4l2.V4L2_PIX_FMT_MJPEG:
                logging.error("v4l2 passthrough: camera rejected MJPG — falling back to opencv")
                return False

            actual_w = fmt.fmt.pix.width
            actual_h = fmt.fmt.pix.height
            if (actual_w, actual_h) != (w, h):
                logging.warning(
                    "v4l2 passthrough: resolution negotiated to %dx%d (requested %dx%d)",
                    actual_w, actual_h, w, h,
                )
                manager.resolution = (actual_w, actual_h)

            # ── Allocate mmap buffers ──────────────────────────────────────
            req = _v4l2.v4l2_requestbuffers()
            req.count = NUM_BUFS
            req.type = _v4l2.V4L2_BUF_TYPE_VIDEO_CAPTURE
            req.memory = _v4l2.V4L2_MEMORY_MMAP
            fcntl.ioctl(fd, _v4l2.VIDIOC_REQBUFS, req)

            for i in range(req.count):
                qbuf = _v4l2.v4l2_buffer()
                qbuf.type = _v4l2.V4L2_BUF_TYPE_VIDEO_CAPTURE
                qbuf.memory = _v4l2.V4L2_MEMORY_MMAP
                qbuf.index = i
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
            manager.model = "C930e"
            logging.info(
                "v4l2 passthrough (mmap) on %s @ %dx%d %dfps MJPEG",
                V4L2_DEVICE, actual_w, actual_h, FPS,
            )
            time.sleep(2.0)  # hold manual seed while camera initialises
            with manager.cam_ctrl_lock:
                apply_controls(None, dict(manager.cam_ctrl))

            # ── Capture loop ───────────────────────────────────────────────
            buf = _v4l2.v4l2_buffer()
            buf.type = _v4l2.V4L2_BUF_TYPE_VIDEO_CAPTURE
            buf.memory = _v4l2.V4L2_MEMORY_MMAP

            select_timeouts = 0
            freeze_hash = None
            freeze_count = 0
            while not manager._stop.is_set() and not manager._restart.is_set():
                ready, _, _ = select.select([fd], [], [], 1.0)
                if not ready:
                    select_timeouts += 1
                    if select_timeouts >= SELECT_TIMEOUT_LIMIT:
                        raise OSError(f"no frames for {select_timeouts}s — camera firmware hung")
                    continue
                select_timeouts = 0
                fcntl.ioctl(fd, _v4l2.VIDIOC_DQBUF, buf)
                bytesused = buf.bytesused
                frame = bytes(mmaps[buf.index][:bytesused])
                fcntl.ioctl(fd, _v4l2.VIDIOC_QBUF, buf)

                eoi = frame.rfind(b'\xff\xd9')
                if bytesused < 4 or frame[:2] != b'\xff\xd8' or eoi < 0:
                    invalid_run += 1
                    if invalid_run % LOG_EVERY == 1:
                        logging.warning(
                            "v4l2 passthrough: %d invalid MJPEG frame(s) — dropped", invalid_run,
                        )
                    continue
                invalid_run = 0
                manager.output.write(frame[:eoi + 2])

                # Content-freeze detection: byte-identical frames indicate the
                # sensor stream stalled without disconnecting. Sensor noise
                # guarantees genuine frames are never byte-identical.
                fh = hashlib.md5(frame[:eoi + 2]).hexdigest()
                if fh == freeze_hash:
                    freeze_count += 1
                    if freeze_count >= CONTENT_FREEZE_LIMIT:
                        logging.warning(
                            "v4l2 passthrough: content freeze — %d identical frames, resetting camera",
                            freeze_count,
                        )
                        usb_reset(CAM_USB_ID)
                        freeze_hash = None
                        freeze_count = 0
                        break
                else:
                    freeze_hash = fh
                    freeze_count = 0

        except OSError as e:
            logging.error("v4l2 passthrough error: %s", e)
            usb_reset(CAM_USB_ID)
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

        if not manager._stop.is_set() and not manager._restart.is_set():
            if manager._stop.wait(backoff):
                break
            backoff = min(backoff * 2, 30)

    return True
