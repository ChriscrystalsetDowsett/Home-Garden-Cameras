# Transferring Home Garden Cameras to a New Pi

This document gives the exact commands to copy the project from one Pi to
another over SSH, skipping photos, videos, and other Pi-specific generated files.

---

## What gets transferred

Everything in the project **except** the paths listed in `.transferignore`:

- `data/` (photos and videos) — too large and device-specific
- `__pycache__/`, `*.pyc` — rebuilt on the target
- `*.log`, `*.mjpeg` — temporary/runtime files
- `.env` — if you add secrets in future

---

## Prerequisites

Both Pis must be on the same network (or reachable via IP/hostname).
SSH must be enabled on the **target** Pi.

---

## The rsync command

Run this on the **source** Pi (the one you're copying *from*):

```bash
rsync -av --progress \
  --filter=': .transferignore' \
  --exclude='.transferignore' \
  ~/home-garden-cameras/ \
  pi@<TARGET_IP>:~/home-garden-cameras/
```

Replace `<TARGET_IP>` with the IP address or hostname of the new Pi.
Replace `pi` with the username on the target Pi if different.

### What the flags do

| Flag | Meaning |
|---|---|
| `-a` | Archive mode — preserves permissions, symlinks, timestamps |
| `-v` | Verbose — prints each file as it's copied |
| `--progress` | Shows transfer speed and ETA |
| `--filter=': .transferignore'` | Reads exclusion rules from `.transferignore` |
| `--exclude='.transferignore'` | Don't copy the ignore file itself |

---

## After transfer — first-time setup on the target Pi

SSH into the **target** Pi and run:

```bash
cd ~/home-garden-cameras

# 1. Install all dependencies
bash scripts/install.sh

# 2. Edit config if this Pi has a different port, resolution, or flip settings
nano config/settings.yaml

# 3. Start the app
bash scripts/start.sh
```

---

## Dry run (preview what would be transferred)

Add `-n` to rsync to simulate without copying anything:

```bash
rsync -avn --progress \
  --filter=': .transferignore' \
  --exclude='.transferignore' \
  ~/home-garden-cameras/ \
  pi@<TARGET_IP>:~/home-garden-cameras/
```

---

## Copying media selectively

If you *do* want to copy photos or videos to the new Pi:

```bash
# Copy photos only
rsync -av --progress ~/home-garden-cameras/data/photos/ pi@<TARGET_IP>:~/home-garden-cameras/data/photos/

# Copy videos only
rsync -av --progress ~/home-garden-cameras/data/videos/ pi@<TARGET_IP>:~/home-garden-cameras/data/videos/
```
