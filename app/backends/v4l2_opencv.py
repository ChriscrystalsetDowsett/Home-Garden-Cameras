"""V4L2 fallback via OpenCV — used when the mmap passthrough path is rejected.

Higher CPU cost (decode → encode round trip) but works when the driver refuses
MJPEG or the v4l2 python bindings are unavailable.
"""
import logging
import subprocess
import time

import cv2

from .v4l2_passthrough import V4L2_DEVICE, FPS


JPEG_QUALITY = 85


def _open(resolution):
    w, h = resolution
    cap = cv2.VideoCapture(V4L2_DEVICE, cv2.CAP_V4L2)
    if not cap.isOpened():
        cap.release()
        return None
    mjpg = cv2.VideoWriter_fourcc(*"MJPG")
    cap.set(cv2.CAP_PROP_FOURCC, mjpg)
    actual = int(cap.get(cv2.CAP_PROP_FOURCC))
    if actual != mjpg:
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"YUYV"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
    cap.set(cv2.CAP_PROP_FPS, FPS)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    subprocess.run(
        ["v4l2-ctl", "-d", V4L2_DEVICE, "--set-ctrl=exposure_dynamic_framerate=0"],
        capture_output=True, check=False,
    )
    return cap


def apply_controls(cap, c):
    """OpenCV property setters + v4l2-ctl for white balance."""
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
    except Exception as e:
        logging.debug("v4l2 opencv apply_controls failed: %s", e)


def run(manager):
    backoff = 1
    while not manager._stop.is_set():
        manager._restart.clear()
        cap = _open(manager.resolution)
        if cap is None:
            if manager._stop.wait(backoff):
                break
            backoff = min(backoff * 2, 30)
            continue
        backoff = 1
        manager.model = "C930e"
        manager.set_handle(cap)
        with manager.cam_ctrl_lock:
            apply_controls(cap, dict(manager.cam_ctrl))
        failures = 0
        while not manager._stop.is_set() and not manager._restart.is_set():
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
                manager.output.write(buf.tobytes())
        cap.release()
        manager.clear_handle(cap)
        if not manager._stop.is_set() and not manager._restart.is_set():
            if manager._stop.wait(backoff):
                break
            backoff = min(backoff * 2, 30)
