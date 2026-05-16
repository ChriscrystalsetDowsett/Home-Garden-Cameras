# Home Garden Cameras

A self-contained Raspberry Pi camera server with a mobile-friendly web UI.
Stream live video, capture photos, record video, and run timelapses — all from
a browser on your phone or laptop.

---

## What it does

- **Live MJPEG stream** at up to 1080p
- **Photo capture** with automatic invisible post-processing (noise reduction,
  CA correction, output sharpening, subtle film grain)
- **Video recording** to H.264 MP4
- **Timelapse** with configurable interval and duration, compiled to MP4 by ffmpeg
- **Film emulation** — Portra, Velvia, HP5, Cinestill, Tri-X, Provia, Ektar, Agfa
- **Camera controls** — exposure, gain, white balance, sharpness, contrast, NR
- **System stats** — CPU, RAM, disk, temperature, network, FPS gauge

---

## Hardware required

| Part | Notes |
|---|---|
| Raspberry Pi 4 (2 GB+ RAM) | Pi 3B+ works but is slower for post-processing |
| Raspberry Pi Camera Module v2 (IMX219) | Other libcamera-compatible sensors also work |
| MicroSD card (16 GB+) | Class 10 / A1 or better |
| Power supply | Official Pi USB-C adapter recommended |

---

## Installation (fresh Pi OS Lite)

1. **Flash Raspberry Pi OS Lite (64-bit Bookworm)** onto a microSD card using
   Raspberry Pi Imager. Enable SSH and set your username/password in the imager.

2. **Boot, SSH in**, then clone or copy the project:

   ```bash
   # Option A — copy from another Pi (see docs/TRANSFER.md)
   # Option B — clone from GitHub (if published)
   git clone https://github.com/yourname/home-garden-cameras.git ~/home-garden-cameras
   ```

3. **Enable the camera** in raspi-config:

   ```bash
   sudo raspi-config   # Interface Options → Camera → Enable
   sudo reboot
   ```

4. **Run the installer:**

   ```bash
   cd ~/home-garden-cameras
   bash scripts/install.sh
   ```

   The installer will:
   - Install system packages (`ffmpeg`, `python3-picamera2`, etc.)
   - Install Python packages from `requirements.txt`
   - Create `data/photos/` and `data/videos/` directories
   - Optionally register a systemd service that starts on boot

5. **Start the app:**

   ```bash
   bash scripts/start.sh
   ```

6. **Open the web UI** on any device on the same network:

   ```
   http://<pi-ip-address>:8080
   ```

   Find your Pi's IP with `hostname -I`.

---

## Configuration

All tunable settings are in **`config/settings.yaml`**:

```yaml
server:
  host: "0.0.0.0"      # listen on all interfaces
  port: 8080

camera:
  default_resolution: "1280x720"   # 640x480 | 1280x720 | 1920x1080
  hflip: false                     # flip image horizontally at startup
  vflip: false                     # flip image vertically at startup

paths:
  photos: "data/photos"   # relative to project root
  videos: "data/videos"
```

Restart the app after changing this file.

---

## Project structure

```
home-garden-cameras/
  app/          Server-side Python package (Flask, camera, timelapse, etc.)
  static/       Frontend assets (currently inline; reserved for future use)
  templates/    Jinja2 HTML templates
  config/       settings.yaml — all runtime configuration
  scripts/      install.sh, start.sh, stop.sh
  data/
    photos/     Captured still images  ← not transferred to a new Pi
    videos/     Recorded/compiled MP4s ← not transferred to a new Pi
  docs/         README.md, TRANSFER.md
  run.py        Entry point
  requirements.txt
  .transferignore
```

---

## Stopping the app

```bash
bash scripts/stop.sh
```

Or, if running via systemd:

```bash
sudo systemctl stop home-garden-cameras
```

---

## Transferring to a new Pi

See **[docs/TRANSFER.md](TRANSFER.md)** for the exact rsync command.

---

## Post-processing pipeline

Every photo and timelapse frame is automatically processed after capture:

1. **Chromatic aberration correction** — laterally aligns R/B channels to G
2. **Chroma noise reduction** — heavy NLM on colour channels, kills colour speckle
3. **Luminance noise reduction** — light NLM preserves texture detail
4. **Output sharpening** — unsharp mask on luminance only, threshold-gated
5. **Film grain** — subtle midtone-weighted grain so NR doesn't look plastic

Processing runs in background threads for timelapse frames (non-blocking) and
inline for photos (~8–12 s at 1280×720). The original EXIF metadata is preserved.

---

## License

MIT — see LICENSE file (add one if publishing).
