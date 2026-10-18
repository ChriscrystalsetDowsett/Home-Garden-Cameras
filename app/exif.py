"""EXIF for photos and timelapse frames, so every capture carries when and how it
was taken (camera, lens, exposure) into Lightroom and the Media Browser."""
import logging
from fractions import Fraction

try:
    import piexif
except ImportError:          # photos are still saved, just without EXIF
    piexif = None

# Lens facts the sensor can't report. Camera Module 3 (IMX708) standard lens;
# Camera Module 2 (IMX219).
LENSES = {
    "IMX708": {"f": 1.8, "mm": 4.74, "mm35": 27, "lens": "Raspberry Pi Camera Module 3"},
    "IMX708_WIDE": {"f": 2.2, "mm": 2.75, "mm35": 14, "lens": "Raspberry Pi Camera Module 3 Wide"},
    "IMX219": {"f": 2.0, "mm": 3.04, "mm35": 29, "lens": "Raspberry Pi Camera Module 2"},
}


def _rational(x, limit=1_000_000):
    f = Fraction(x).limit_denominator(limit)
    return (f.numerator, f.denominator)


def build(*, when, make, model, description, width, height, md=None, orientation=1,
          pi_name="", manual_exposure=False, manual_wb=False):
    """EXIF bytes for a JPEG, or None. `md` is picamera2 request metadata (optional)."""
    if piexif is None:
        return None
    md = md or {}
    try:
        dt = when.strftime("%Y:%m:%d %H:%M:%S").encode()
        zeroth = {
            piexif.ImageIFD.Make: make.encode(),
            piexif.ImageIFD.Model: model.encode(),
            piexif.ImageIFD.Software: b"Home Garden Cameras",
            piexif.ImageIFD.Orientation: orientation,
            piexif.ImageIFD.DateTime: dt,
            piexif.ImageIFD.ImageDescription: description.encode(),
            piexif.ImageIFD.XResolution: (72, 1),
            piexif.ImageIFD.YResolution: (72, 1),
            piexif.ImageIFD.ResolutionUnit: 2,
        }
        if pi_name:
            zeroth[piexif.ImageIFD.Artist] = f"Home camera {pi_name}".encode()
        exif = {
            piexif.ExifIFD.ExifVersion: b"0232",
            piexif.ExifIFD.FlashpixVersion: b"0100",
            piexif.ExifIFD.ColorSpace: 1,
            piexif.ExifIFD.PixelXDimension: width,
            piexif.ExifIFD.PixelYDimension: height,
            piexif.ExifIFD.DateTimeOriginal: dt,
            piexif.ExifIFD.DateTimeDigitized: dt,
            piexif.ExifIFD.OffsetTimeOriginal: when.astimezone().strftime("%z")[:3].encode()
                                               + b":" + when.astimezone().strftime("%z")[3:].encode(),
            piexif.ExifIFD.ExposureMode: 1 if manual_exposure else 0,
            piexif.ExifIFD.WhiteBalance: 1 if manual_wb else 0,
            piexif.ExifIFD.SceneCaptureType: 0,
            piexif.ExifIFD.Flash: 0,
        }
        if md.get("ExposureTime"):
            exif[piexif.ExifIFD.ExposureTime] = _rational(md["ExposureTime"] / 1e6)
        gain = (md.get("AnalogueGain") or 0) * (md.get("DigitalGain") or 1)
        if gain:
            exif[piexif.ExifIFD.ISOSpeedRatings] = int(round(gain * 100))
            exif[piexif.ExifIFD.SensitivityType] = 2
        lens = LENSES.get(model.upper())
        if lens:
            exif[piexif.ExifIFD.FNumber] = _rational(lens["f"], 100)
            exif[piexif.ExifIFD.FocalLength] = _rational(lens["mm"], 100)
            exif[piexif.ExifIFD.FocalLengthIn35mmFilm] = lens["mm35"]
            exif[piexif.ExifIFD.LensModel] = lens["lens"].encode()
            exif[piexif.ExifIFD.LensMake] = b"Raspberry Pi"
        if md.get("ColourTemperature"):
            exif[piexif.ExifIFD.UserComment] = (
                b"ASCII\x00\x00\x00" + f"WB {md['ColourTemperature']} K".encode())
        return piexif.dump({"0th": zeroth, "Exif": exif, "GPS": {}, "1st": {}})
    except Exception as e:
        logging.warning("EXIF build failed: %s", e)
        return None


ORIENTATION = {(False, False): 1, (True, False): 2, (False, True): 4, (True, True): 3}
