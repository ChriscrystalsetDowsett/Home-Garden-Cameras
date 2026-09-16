"""ocv_pipeline unit tests (pure functions, no hardware) + backend interface."""
import io

import numpy as np
from PIL import Image

from app.backends import ocv_pipeline, picamera2_backend, v4l2_opencv, v4l2_passthrough
from app.config import CAM_CTRL_DEFAULTS


def _neutral():
    return dict(CAM_CTRL_DEFAULTS)


def _jpeg_bytes(color=(30, 120, 40), size=(64, 48), quality=90):
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


# ── needs_ocv gate ──────────────────────────────────────────────────────────

def test_needs_ocv_false_at_defaults():
    assert not ocv_pipeline.needs_ocv(_neutral())


def test_needs_ocv_true_for_each_trigger():
    for key, val in [("tint", 20), ("hflip", True), ("vflip", True),
                     ("film_filter", "portra")]:
        s = _neutral()
        s[key] = val
        assert ocv_pipeline.needs_ocv(s), key
    s = _neutral()
    s["warmth"] = ocv_pipeline.WARMTH_NEUTRAL + 10
    assert ocv_pipeline.needs_ocv(s)


# ── apply_ocv transforms ────────────────────────────────────────────────────

def test_apply_ocv_hflip_mirrors_frame():
    arr = np.zeros((32, 32, 3), dtype=np.uint8)
    arr[:, 16:] = 200  # right half bright
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "JPEG", quality=95)
    s = _neutral()
    s["hflip"] = True
    out = np.array(Image.open(io.BytesIO(ocv_pipeline.apply_ocv(buf.getvalue(), s))))
    assert out[:, :8].mean() > 150   # bright side now left
    assert out[:, 24:].mean() < 60


def test_apply_ocv_warmth_shifts_red_up_blue_down():
    base = _jpeg_bytes(color=(100, 100, 100))
    s = _neutral()
    s["warmth"] = 100
    out = np.array(Image.open(io.BytesIO(ocv_pipeline.apply_ocv(base, s)))).astype(int)
    r, b = out[..., 0].mean(), out[..., 2].mean()
    assert r > 110, "red channel should rise"
    assert b < 90, "blue channel should fall"


def test_apply_ocv_returns_original_on_garbage_input():
    s = _neutral()
    s["hflip"] = True
    garbage = b"definitely not a jpeg"
    assert ocv_pipeline.apply_ocv(garbage, s) == garbage


def test_all_film_filters_produce_valid_changed_jpeg():
    from app.film import FILM_FILTERS
    base = _jpeg_bytes()
    for name in FILM_FILTERS:
        s = _neutral()
        s["film_filter"] = name
        out = ocv_pipeline.apply_ocv(base, s)
        assert out != base, f"{name}: apply_ocv fell through to the error path"
        Image.open(io.BytesIO(out)).verify()  # decodable JPEG


def test_film_strength_blends_towards_original():
    base = _jpeg_bytes(color=(60, 140, 200))
    s_full = _neutral()
    s_full["film_filter"] = "hp5"     # B&W — maximally far from the original
    s_half = dict(s_full, film_strength=50)

    full = np.array(Image.open(io.BytesIO(ocv_pipeline.apply_ocv(base, s_full)))).astype(int)
    half = np.array(Image.open(io.BytesIO(ocv_pipeline.apply_ocv(base, s_half)))).astype(int)
    orig = np.array(Image.open(io.BytesIO(base))).astype(int)

    dist_full = np.abs(full - orig).mean()
    dist_half = np.abs(half - orig).mean()
    assert dist_half < dist_full, "50% strength should sit closer to the original"


# ── PIL snapshot filters ────────────────────────────────────────────────────

def test_apply_pil_filter_grayscale_collapses_channels():
    img = Image.new("RGB", (16, 16), (200, 30, 60))
    out = np.array(ocv_pipeline.apply_pil_filter(img, "grayscale"))
    assert (out[..., 0] == out[..., 1]).all()
    assert (out[..., 1] == out[..., 2]).all()


def test_apply_pil_filter_unknown_name_is_passthrough():
    img = Image.new("RGB", (8, 8), (1, 2, 3))
    assert ocv_pipeline.apply_pil_filter(img, "not-a-filter") is img


# ── Backend module interface ────────────────────────────────────────────────

def test_backends_expose_run_and_apply_controls():
    for mod in (picamera2_backend, v4l2_opencv, v4l2_passthrough):
        assert callable(getattr(mod, "run")), mod.__name__
        assert callable(getattr(mod, "apply_controls")), mod.__name__


def test_v4l2_passthrough_available_is_bool():
    assert isinstance(v4l2_passthrough.available(), bool)
