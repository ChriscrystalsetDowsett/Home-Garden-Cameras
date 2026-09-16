"""Per-frame OpenCV post-processing + PIL snapshot filters.

Pure functions with no hardware dependencies — safe to unit-test.

`apply_ocv` runs on every displayed frame at capture rate (24 fps). It is
skipped when the control dict is at its neutral values so the hot path
stays cheap on a Raspberry Pi.
`apply_pil_filter` runs once per snapshot and is not perf-sensitive.
"""
import cv2
import numpy as np
from PIL import ImageEnhance, ImageFilter, ImageOps

from ..config import CAM_CTRL_DEFAULTS
from ..film import FILM_FILTERS


# Warmth at this value → apply_ocv skipped (no change from startup default)
WARMTH_NEUTRAL = int(CAM_CTRL_DEFAULTS["warmth"])


def needs_ocv(s):
    """True when the control dict differs from the neutral defaults."""
    return (
        s["tint"]
        or s["warmth"] != WARMTH_NEUTRAL
        or s["hflip"]
        or s["vflip"]
        or s["film_filter"] != "none"
    )


def apply_ocv(buf, s):
    """Post-processing: tint shift, flip, film simulation.

    Returns the encoded JPEG bytes (either the transformed frame or the
    original buffer on any error). Never raises.
    """
    try:
        arr = np.frombuffer(buf, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if frame is None:
            return buf

        hf, vf = s.get("hflip", False), s.get("vflip", False)
        if hf and vf:
            frame = cv2.flip(frame, -1)
        elif hf:
            frame = cv2.flip(frame, 1)
        elif vf:
            frame = cv2.flip(frame, 0)

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
                    weights = fd.get("weights")
                    if weights:
                        b_f, g_f, r_f = cv2.split(frame.astype(np.float32))
                        gray = np.clip(
                            r_f * weights[0] + g_f * weights[1] + b_f * weights[2],
                            0, 255,
                        ).astype(np.uint8)
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

        # Effects re-encode at fixed quality — see JPEG_QUALITY caller-side.
        ok, enc = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return enc.tobytes() if ok else buf
    except Exception:
        return buf


def apply_pil_filter(img, name):
    """PIL snapshot filter applied post-capture (once, not per-frame)."""
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
