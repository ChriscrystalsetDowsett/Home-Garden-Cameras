"""picamera2 (CSI) backend: the live MJPEG stream plus everything that needs the
camera itself — full-sensor stills, quick frames from the running video, and
hardware H.264 recording.

picamera2 must only be touched from one thread. The capture loop owns it and
runs jobs that other threads queue with `manager.camera_job(fn)`; each job is
called as `fn(picam2, ctx)` and its return value goes back to the caller.

Only imported when settings.yaml → camera.backend == "picamera2".
"""
import logging
import queue
import time

try:
    from picamera2 import Picamera2
    from picamera2.encoders import JpegEncoder, H264Encoder, Quality
    from picamera2.outputs import FileOutput, PyavOutput
except ImportError:
    Picamera2 = None
    JpegEncoder = H264Encoder = Quality = None
    FileOutput = PyavOutput = None


FPS = 24
RECORD_BITRATE = 16_000_000     # hardware H.264, 1080p24: ~16 Mbit/s is visually clean


def _video_config(picam2, resolution):
    w, h = resolution
    return picam2.create_video_configuration(
        main={"size": (w, h)},
        encode="main",
        controls={"FrameRate": float(FPS)},
        buffer_count=6,
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
    except Exception as e:
        logging.warning("picamera2 open failed: %s", e)
        return None, "Unknown"


class _Ctx:
    """What a job may need besides picam2: the stream encoder and the manager."""
    def __init__(self, manager, encoder):
        self.manager = manager
        self.encoder = encoder
        self.recorder = None        # (H264Encoder, output) while a hardware recording runs

    def restart_stream(self, picam2):
        picam2.configure(_video_config(picam2, self.manager.resolution))
        picam2.start_recording(self.encoder, FileOutput(self.manager.output))
        with self.manager.cam_ctrl_lock:
            apply_controls(picam2, dict(self.manager.cam_ctrl), af_locked=self.manager.af_locked,
                           awb_gains=self.manager.awb_locked_gains, lens_pos=self.manager.lens_position)


# ── Jobs (run on the capture thread) ───────────────────────────────────────────

def job_still(picam2, ctx):
    """Full-sensor still. Stops the video briefly (~0.8 s of frozen preview).

    Returns (PIL image, metadata dict). While a hardware recording runs the
    sensor mode can't change, so the still comes from the video frame instead."""
    if ctx.recorder is not None:
        return job_frame(picam2, ctx)
    img, md = None, {}
    try:
        picam2.stop_recording()
        picam2.configure(picam2.create_still_configuration(main={"size": picam2.sensor_resolution}))
        picam2.start()
        with ctx.manager.cam_ctrl_lock:
            apply_controls(picam2, dict(ctx.manager.cam_ctrl), af_locked=ctx.manager.af_locked,
                           awb_gains=ctx.manager.awb_locked_gains, lens_pos=ctx.manager.lens_position)
        time.sleep(0.3)             # a couple of frames for AE/AWB after the mode change
        req = picam2.capture_request()
        try:
            img = req.make_image("main").convert("RGB")
            md = req.get_metadata()
        finally:
            req.release()
        picam2.stop()
    finally:
        try:
            ctx.restart_stream(picam2)
        except Exception as e:
            logging.warning("picamera2 restart after still failed: %s", e)
    return img, md


def job_frame(picam2, ctx):
    """The next frame of the running video at full quality (no pause in the preview)."""
    req = picam2.capture_request()
    try:
        return req.make_image("main").convert("RGB"), req.get_metadata()
    finally:
        req.release()


def job_metadata(picam2, ctx):
    return picam2.capture_metadata()


def job_record_start(path):
    def run(picam2, ctx):
        if ctx.recorder is not None:
            raise RuntimeError("already recording")
        enc = H264Encoder(bitrate=RECORD_BITRATE, repeat=True, iperiod=FPS, framerate=FPS, profile="high")
        out = PyavOutput(str(path), format="mp4")
        picam2.start_encoder(enc, out, name="main")
        ctx.recorder = (enc, out)
        return True
    return run


def job_record_stop(picam2, ctx):
    if ctx.recorder is None:
        return False
    enc, _ = ctx.recorder
    ctx.recorder = None
    picam2.stop_encoder(enc)
    return True


# ── Controls ───────────────────────────────────────────────────────────────────

def apply_controls(picam2, c, af_locked=False, awb_gains=None, lens_pos=None):
    """picamera2 ISP: Brightness -1..1, Saturation 0..2, Sharpness 0..16, Contrast 0..32.

    af_locked keeps the lens where a timelapse fixed it (AfMode 0); awb_gains
    holds white balance fixed for a timelapse so the colour doesn't drift."""
    try:
        controls = {}
        exp = int(c.get("exposure_time", 0))
        controls["AeEnable"] = exp <= 0
        if exp > 0:
            controls["ExposureTime"] = exp
        gain = float(c.get("analogue_gain", 0.0))
        if gain > 0:
            controls["AnalogueGain"] = gain
        if awb_gains:
            controls["AwbEnable"] = False
            controls["ColourGains"] = tuple(awb_gains)
        elif c.get("awb_mode", "auto") == "auto":
            controls["AwbEnable"] = True
        else:
            controls["AwbEnable"] = False
            controls["ColourTemperature"] = max(2000, min(7500, int(c.get("awb_kelvin", 5600))))
        controls["Brightness"] = max(-1.0, min(1.0, int(c.get("brightness", 0)) / 100.0))
        controls["Saturation"] = max(0.0, min(2.0, 1.0 + int(c.get("saturation", 0)) / 100.0))
        controls["Sharpness"] = max(0.0, min(16.0, float(c.get("sharpness", 1.0))))
        controls["Contrast"] = max(0.0, min(32.0, float(c.get("contrast", 1.0))))
        # Autofocus (IMX708 / Camera Module 3 only)
        if "AfMode" in picam2.camera_controls:
            _af_mode = {"continuous": 2, "auto": 1, "manual": 0}
            _af_range = {"normal": 0, "macro": 1, "full": 2}
            if af_locked:
                controls["AfMode"] = 0
                if lens_pos is not None:
                    controls["LensPosition"] = float(lens_pos)
            else:
                controls["AfMode"] = _af_mode.get(c.get("af_mode", "continuous"), 2)
                controls["AfRange"] = _af_range.get(c.get("af_range", "normal"), 0)
        controls["AeMeteringMode"] = int(c.get("ae_metering_mode", 2))
        controls["AeConstraintMode"] = int(c.get("ae_constraint_mode", 2))
        if "HdrMode" in picam2.camera_controls:
            controls["HdrMode"] = int(c.get("hdr_mode", 2))
        crop_max = picam2.camera_properties.get("ScalerCropMaximum")
        if crop_max:
            controls["ScalerCrop"] = crop_max
        picam2.set_controls(controls)
    except Exception as e:
        logging.warning("picamera2 apply_controls failed: %s", e)


# ── Capture loop ───────────────────────────────────────────────────────────────

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
        ctx = _Ctx(manager, encoder)
        manager.set_handle(picam2)
        with manager.cam_ctrl_lock:
            apply_controls(picam2, dict(manager.cam_ctrl), af_locked=manager.af_locked,
                           awb_gains=manager.awb_locked_gains, lens_pos=manager.lens_position)
        try:
            picam2.start_recording(encoder, FileOutput(manager.output))
            while not manager._stop.is_set() and not manager._restart.is_set():
                try:
                    fn, box = manager._jobs.get(timeout=0.1)
                except queue.Empty:
                    continue
                try:
                    box["result"] = fn(picam2, ctx)
                except Exception as e:
                    logging.warning("camera job %s failed: %s", getattr(fn, "__name__", fn), e)
                    box["error"] = e
                finally:
                    box["done"].set()
        except Exception as e:
            logging.warning("picamera2 loop error: %s", e)
        finally:
            if ctx.recorder is not None:
                try:
                    job_record_stop(picam2, ctx)
                except Exception:
                    pass
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
