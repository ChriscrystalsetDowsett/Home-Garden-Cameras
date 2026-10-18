"""picamera2 (CSI) backend — captures MJPEG for the live stream and handles
the video-mode → still-mode handshake for full-sensor snapshots.

Only imported when settings.yaml → camera.backend == "picamera2".
"""
import logging
import time

try:
    from picamera2 import Picamera2
    from picamera2.encoders import JpegEncoder
    from picamera2.outputs import FileOutput
except ImportError:
    Picamera2 = None
    JpegEncoder = None
    FileOutput = None


FPS = 24


def _video_config(picam2, resolution):
    w, h = resolution
    return picam2.create_video_configuration(
        main={"size": (w, h)},
        encode="main",
        controls={"FrameRate": float(FPS)},
        buffer_count=4,
    )


def _open(resolution):
    """Open + configure the camera at `resolution`. Returns (picam2, model) or (None, "Unknown")."""
    if Picamera2 is None:
        return None, "Unknown"
    try:
        picam2 = Picamera2()
        model = picam2.camera_properties.get("Model", "Unknown").upper()
        picam2.configure(_video_config(picam2, resolution))
        return picam2, model
    except Exception:
        return None, "Unknown"


def _do_still_capture(picam2, encoder, manager):
    """Stop recording, grab a full-sensor still, restart recording.

    Called exclusively from the capture loop thread so picamera2 is always
    touched from a single thread. Sets `manager._still_img` and signals
    `manager._still_done` when complete.
    """
    img = None
    try:
        picam2.stop_recording()
        still_cfg = picam2.create_still_configuration(main={"size": picam2.sensor_resolution})
        picam2.configure(still_cfg)
        picam2.start()
        # Brief settle — sensor mode change warrants ~2 frames to stabilise.
        time.sleep(0.3)
        img = picam2.capture_image("main")
        if img.mode != "RGB":
            img = img.convert("RGB")
        picam2.stop()
    except Exception as e:
        logging.warning("picamera2 still capture failed: %s", e)
        img = None
    finally:
        try:
            picam2.configure(_video_config(picam2, manager.resolution))
            picam2.start_recording(encoder, FileOutput(manager.output))
            with manager.cam_ctrl_lock:
                apply_controls(picam2, dict(manager.cam_ctrl), af_locked=manager.af_locked)
        except Exception as e:
            logging.warning("picamera2 restart after still failed: %s", e)
        manager._still_img = img
        manager._still_done.set()


def apply_controls(picam2, c, af_locked=False):
    """picamera2 ISP: Brightness -1..1, Saturation 0..2, Sharpness 0..16, Contrast 0..32.

    af_locked keeps the lens where a timelapse fixed it (AfMode 0)."""
    try:
        controls = {}
        exp = int(c.get("exposure_time", 0))
        controls["AeEnable"] = exp <= 0
        if exp > 0:
            controls["ExposureTime"] = exp
        gain = float(c.get("analogue_gain", 0.0))
        if gain > 0:
            controls["AnalogueGain"] = gain
        if c.get("awb_mode", "auto") == "auto":
            controls["AwbEnable"] = True
        else:
            controls["AwbEnable"] = False
            controls["ColourTemperature"] = max(2000, min(7500, int(c.get("awb_kelvin", 5600))))
        controls["Brightness"] = max(-1.0, min(1.0, int(c.get("brightness", 0)) / 100.0))
        controls["Saturation"] = max(0.0, min(2.0, 1.0 + int(c.get("saturation", 0)) / 100.0))
        controls["Sharpness"] = max(0.0, min(16.0, float(c.get("sharpness", 1.0))))
        controls["Contrast"] = max(0.0, min(32.0, float(c.get("contrast", 1.0))))
        # Autofocus (IMX708 / Camera Module 3 only — silently ignored on other sensors)
        _af_mode = {"continuous": 2, "auto": 1, "manual": 0}
        _af_range = {"normal": 0, "macro": 1, "full": 2}
        if af_locked:
            controls["AfMode"] = 0
        else:
            controls["AfMode"] = _af_mode.get(c.get("af_mode", "continuous"), 2)
            controls["AfRange"] = _af_range.get(c.get("af_range", "normal"), 0)
        controls["AeMeteringMode"] = int(c.get("ae_metering_mode", 2))
        controls["AeConstraintMode"] = int(c.get("ae_constraint_mode", 2))
        controls["HdrMode"] = int(c.get("hdr_mode", 2))
        crop_max = picam2.camera_properties.get("ScalerCropMaximum")
        if crop_max:
            controls["ScalerCrop"] = crop_max
        picam2.set_controls(controls)
    except Exception as e:
        logging.debug("picamera2 apply_controls failed: %s", e)


def run(manager):
    """Blocking capture loop — drives StreamOutput until manager._stop is set."""
    backoff = 1
    while not manager._stop.is_set():
        manager._restart.clear()
        picam2, model = _open(manager.resolution)
        if picam2 is None:
            if manager._stop.wait(backoff):
                break
            backoff = min(backoff * 2, 30)
            continue
        backoff = 1
        manager.model = model
        encoder = JpegEncoder(q=manager.stream_quality, num_threads=2)
        manager.set_handle(picam2)
        with manager.cam_ctrl_lock:
            apply_controls(picam2, dict(manager.cam_ctrl), af_locked=manager.af_locked)
        try:
            picam2.start_recording(encoder, FileOutput(manager.output))
            while not manager._stop.is_set() and not manager._restart.is_set():
                if manager._still_request.wait(timeout=0.1):
                    manager._still_request.clear()
                    _do_still_capture(picam2, encoder, manager)
        except Exception as e:
            logging.warning("picamera2 loop error: %s", e)
        finally:
            try:
                picam2.stop_recording()
            except Exception:
                pass
            try:
                picam2.close()
            except Exception:
                pass
        manager.clear_handle(picam2)
        if not manager._stop.is_set() and not manager._restart.is_set():
            if manager._stop.wait(backoff):
                break
            backoff = min(backoff * 2, 30)
