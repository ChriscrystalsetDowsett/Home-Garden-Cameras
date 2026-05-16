"""
Automatic post-processing for photos and timelapse frames.

Pipeline (applied in LAB colour space):
  1. Mild chroma smoothing  — bilateral filter on A/B channels only;
                              reduces JPEG colour-block artefacts without
                              smearing colour edges or flattening hues.
                              Bilateral is edge-preserving and appropriate
                              for JPEG-compressed input; NLM is not.
  2. Output sharpening      — unsharp mask on L only; edge-localised and
                              threshold-gated to avoid lifting noise in
                              flat regions.

Removed from previous version:
  - CA correction: calibrated for IMX219, not C930e; the 0.1 % scale
    offsets are sub-pixel on this sensor and the remap's bilinear
    interpolation adds softening without correcting a real defect.
  - NLM noise reduction: wrong tool for JPEG-compressed input — the
    algorithm conflates DCT block artefacts with noise and produces
    watercolour smearing on colour channels.
  - Film grain: objectively degrades quality (adds ~1.2 dB RMS noise,
    increases re-compressed file size). Its only purpose was to mask
    over-smoothing from NLM; removing NLM removes the need for it.

Processing time at 1280×720: ~0.05 s per image.
Images are processed in-place (path is overwritten).
"""

import cv2
import numpy as np
from pathlib import Path

try:
    import piexif as _piexif
    _PIEXIF_OK = True
except ImportError:
    _PIEXIF_OK = False

# ── Tuning constants ───────────────────────────────────────────────────────────
# Chroma smoothing — bilateral filter on A/B (colour) channels only.
# d=3 is a 3×3 neighbourhood — conservative, preserves fine colour edges.
# sigmaColor=5 means only pixels within 5 levels are blended — very selective.
_CHROMA_D            = 3
_CHROMA_SIGMA_COLOR  = 5
_CHROMA_SIGMA_SPACE  = 3

# Output sharpening — unsharp mask on L (luminance) channel.
_USM_SIGMA     = 1.0   # Gaussian blur radius for the residual mask
_USM_AMOUNT    = 0.50  # sharpening weight (0.5 = 50 % of the edge residual)
_USM_THRESHOLD = 8     # minimum edge magnitude to sharpen (skips noise/flat areas)


def _unsharp_mask(l_channel: np.ndarray) -> np.ndarray:
    """Edge-localised sharpening on luminance only."""
    blurred   = cv2.GaussianBlur(l_channel, (0, 0), _USM_SIGMA)
    diff      = l_channel.astype(np.int16) - blurred.astype(np.int16)
    mask      = (np.abs(diff) > _USM_THRESHOLD).astype(np.float32)
    sharpened = l_channel.astype(np.float32) + _USM_AMOUNT * diff * mask
    return np.clip(sharpened, 0, 255).astype(np.uint8)


# ── EXIF builder ─────────────────────────────────────────────────────────────
def _build_exif_bytes(metadata: dict, width: int, height: int):
    """Build a piexif EXIF blob from a capture metadata dict. Returns None on failure."""
    if not _PIEXIF_OK or not metadata:
        return None
    try:
        def _enc(v):
            return v.encode() if isinstance(v, str) else (v or b"")

        dt    = _enc(metadata.get("datetime", ""))
        make  = _enc(metadata.get("make",  ""))
        model = _enc(metadata.get("model", ""))
        desc  = _enc(metadata.get("description", ""))

        hflip = metadata.get("hflip", False)
        vflip = metadata.get("vflip", False)
        orientation = {
            (False, False): 1,   # normal
            (True,  False): 2,   # mirror horizontal
            (False, True):  4,   # mirror vertical
            (True,  True):  3,   # rotate 180°
        }.get((bool(hflip), bool(vflip)), 1)

        zeroth = {
            _piexif.ImageIFD.Make:           make,
            _piexif.ImageIFD.Model:          model,
            _piexif.ImageIFD.Software:       b"Home Garden Cameras",
            _piexif.ImageIFD.Orientation:    orientation,
            _piexif.ImageIFD.XResolution:    (72, 1),
            _piexif.ImageIFD.YResolution:    (72, 1),
            _piexif.ImageIFD.ResolutionUnit: 2,   # inches
        }
        if dt:
            zeroth[_piexif.ImageIFD.DateTime]         = dt
        if desc:
            zeroth[_piexif.ImageIFD.ImageDescription] = desc

        exif_ifd = {
            _piexif.ExifIFD.ExifVersion:      b"0231",
            _piexif.ExifIFD.FlashPixVersion:  b"0100",
            _piexif.ExifIFD.ColorSpace:       1,            # sRGB
            _piexif.ExifIFD.PixelXDimension:  width,
            _piexif.ExifIFD.PixelYDimension:  height,
            _piexif.ExifIFD.ExposureMode:     metadata.get("exposure_mode", 0),  # 0=auto 1=manual
            _piexif.ExifIFD.WhiteBalance:     metadata.get("white_balance", 0),  # 0=auto 1=manual
            _piexif.ExifIFD.SceneCaptureType: 0,            # standard
        }
        if dt:
            exif_ifd[_piexif.ExifIFD.DateTimeOriginal]  = dt
            exif_ifd[_piexif.ExifIFD.DateTimeDigitized] = dt

        return _piexif.dump({"0th": zeroth, "Exif": exif_ifd, "GPS": {}, "1st": {}})
    except Exception:
        return None


# ── Public entry point ────────────────────────────────────────────────────────
def postprocess_jpeg(path: Path, quality: int = 92, fast: bool = False,
                     metadata: dict = None) -> None:
    """
    Load *path*, apply the post-processing pipeline, overwrite *path* in place.
    Silently no-ops on any error so it never breaks capture flow.

    fast=True skips sharpening for timelapse frames where throughput matters.
    metadata, if provided, is written as EXIF into the saved image.
    """
    try:
        path = Path(path)
        data = np.frombuffer(path.read_bytes(), dtype=np.uint8)
        bgr  = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if bgr is None:
            return

        # ── 1. Convert to LAB ────────────────────────────────────────────────
        lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
        l, a, b_ch = cv2.split(lab)

        # ── 2. Mild chroma smoothing (JPEG colour-block artefact reduction) ──
        # Bilateral on A/B only — L channel (detail) is untouched here.
        a    = cv2.bilateralFilter(a,    d=_CHROMA_D,
                                   sigmaColor=_CHROMA_SIGMA_COLOR,
                                   sigmaSpace=_CHROMA_SIGMA_SPACE)
        b_ch = cv2.bilateralFilter(b_ch, d=_CHROMA_D,
                                   sigmaColor=_CHROMA_SIGMA_COLOR,
                                   sigmaSpace=_CHROMA_SIGMA_SPACE)

        # ── 3. Output sharpening (skipped on timelapse fast path) ───────────
        if not fast:
            l = _unsharp_mask(l)

        # ── 4. Merge and save with EXIF ──────────────────────────────────────
        lab_out = cv2.merge([l, a, b_ch])
        bgr_out = cv2.cvtColor(lab_out, cv2.COLOR_LAB2BGR)
        h, w    = bgr_out.shape[:2]
        ok, buf = cv2.imencode(".jpg", bgr_out, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if ok:
            raw_bytes = buf.tobytes()
            exif_blob = _build_exif_bytes(metadata, w, h)
            if exif_blob:
                try:
                    raw_bytes = _piexif.insert(exif_blob, raw_bytes)
                except Exception:
                    pass
            path.write_bytes(raw_bytes)

    except Exception:
        pass   # never crash capture; just leave the original file
