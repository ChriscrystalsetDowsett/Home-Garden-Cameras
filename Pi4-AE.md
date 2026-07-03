# Pi 4 — Auto-Exposure Incident (2026-05-22)

## Symptom

Camera auto-exposure stopped tracking changing light levels. Image blew out progressively as the greenhouse brightened through the morning. The camera appeared stuck in manual exposure mode.

---

## Environment

- **Pi:** Pi 4 (192.168.4.186)
- **Camera:** Logitech C930e USB webcam
- **Backend:** V4L2 passthrough (`backend: "v4l2"`, `v4l2_mode: "passthrough"`)
- **Service:** `garden-monitor.service` on port 8080

---

## Investigation

### Git history at time of incident

```
312e22b fix: load server defaults on page load; warmth/tint/exposure from settings.yaml
3a88fc9 feat: Pi Camera v3 picamera2 rework + IMX708 HDR toggle
9f8eec0 v4l2: backlight comp, hide noise reduction, audio in fullscreen
```

### What commit `312e22b` changed

**`app/config.py`** — `warmth` and `tint` changed from hardcoded values to reading from `settings.yaml`:

```diff
- "exposure_time":   0,
+ "exposure_time":  int(_cfg["camera"].get("exposure_time",  0)),

- "tint":        0,
- "warmth":      0,
+ "tint":        int(_cfg["camera"].get("tint",    0)),
+ "warmth":      int(_cfg["camera"].get("warmth",  0)),
```

`settings.yaml` has `warmth: 40`, so this commit caused warmth to go from 0 (no OCV processing) to 40 (warm colour boost applied on every passthrough frame).

**`static/index.js`** — added `loadCtrlDefaults()` on DOMContentLoaded, which fetches `/api/camera_controls/defaults` and syncs the UI. Does **not** call `sendCtrl()`, so it does not push controls to the camera on page load.

### What was confirmed via v4l2-ctl

```
auto_exposure: 3 (Aperture Priority Mode)
```

Camera was already in auto mode at the time of checking. `exposure_time: 0` confirmed in live API response.

### False lead

Initial suspicion was that `warmth: 40` (newly applied via settings.yaml) was making bright highlights look blown out due to warm/red channel boosting. This was **incorrect** — the user confirmed warmth and saturation are intentional and the image was desirable once AE was working.

The actual problem was the V4L2 driver getting into a stuck state where auto-exposure was not actively tracking scene brightness, even though `auto_exposure=3` was nominally set.

---

## What Fixed It

Running this command (as part of investigation, not intentionally as a fix):

```bash
v4l2-ctl --get-ctrl=auto_exposure
```

The act of querying the V4L2 control via ioctl appears to have joggled the C930e's UVC driver out of its stuck state, causing auto-exposure to resume tracking. This is a known quirk of some UVC webcam drivers on Linux — a read ioctl can re-sync driver state.

---

## Why the Code Is Not the Root Cause

The V4L2 passthrough init code (camera.py ~line 371) already correctly re-asserts `auto_exposure=3` on every stream restart when `exposure_time == 0`:

```python
exp = int(ctrl.get("exposure_time", 0))
if exp > 0:
    subprocess.run(["v4l2-ctl", ..., "--set-ctrl=auto_exposure=1,exposure_time_absolute=..."])
else:
    subprocess.run(["v4l2-ctl", ..., "--set-ctrl=auto_exposure=3"])
```

`_isp_v4l2_passthrough()` (~line 799) applies the same logic on every control change. The code was correct — the driver was the failure point.

---

## Proposed Hardening (Rejected)

Making `auto_exposure=3` truly unconditional was considered but rejected because:

1. The existing code already re-asserts it on every stream restart and control change — making it unconditional adds nothing when `exposure_time=0`.
2. Unconditionally overriding to auto would break intentional manual exposure (UI allows setting `exposure_time > 0`).
3. Periodic re-assertion would be new logic, and the stuck state appears to be a rare one-off driver glitch.

---

## If It Happens Again

Run this to confirm the camera's current exposure mode:

```bash
v4l2-ctl --get-ctrl=auto_exposure
# 3 = Aperture Priority (auto) ← correct
# 1 = Manual ← stuck
```

If stuck (`auto_exposure: 1`), force it back to auto:

```bash
v4l2-ctl --set-ctrl=auto_exposure=3
```

Or simply restart the service — the init code will re-assert auto mode:

```bash
sudo systemctl restart garden-monitor
```

The `--get-ctrl` query alone was sufficient to fix the stuck state in this incident, suggesting the UVC driver responds to ioctl activity even on reads.

---

## Second Incident — 2026-05-22 (same day, later session)

### Symptom

Image overexposed and **progressively worsening** through the morning as greenhouse light increased. Camera reported `auto_exposure: 3 (Aperture Priority Mode)` — unlike the first incident, the mode register looked correct. But both `gain` and `exposure_time_absolute` were completely frozen across repeated polling.

```
10:19:40 — gain: 63 | auto_exposure: 3 | exposure_time_absolute: 83
10:19:41 — gain: 63 | auto_exposure: 3 | exposure_time_absolute: 83
... (identical across 10 consecutive 1-second samples)
```

A live AE algorithm updates both values continuously. Static values in auto mode = AE not running.

### Key difference from Incident 1

| | Incident 1 | Incident 2 |
|---|---|---|
| `auto_exposure` register | Stuck at **1** (manual) | Shows **3** (auto) — misleading |
| AE activity | Not running | Not running |
| Fix | `--get-ctrl` ioctl joggled it | Not yet resolved |

### What was tried (and didn't work)

1. **Gain ceiling via `analogue_gain: 4` in settings.yaml** — had no effect because `config.py` hardcodes `analogue_gain: 0.0` and never reads that key. The camera never received a changed gain value.

2. **Fixed `config.py` to read `analogue_gain` from settings.yaml** — gain DID change to 63 in hardware (confirmed via v4l2-ctl). Image unchanged. The AE compensated by extending shutter time internally (exposure register stale/inactive in auto mode and doesn't reflect live AE-chosen value).

3. **Reverted both changes** and restarted service. Gain remained at 63 after restart.

### Critical finding: C930e persists V4L2 control values across fd close/open

After reverting `analogue_gain` to `0.0` and restarting the service (which closes and reopens `/dev/video0`), `gain` stayed at **63** — the value we had set manually earlier. The C930e firmware retains V4L2 user control values in onboard memory across driver-level resets. **A service restart alone is not sufficient to clear manually-set V4L2 control values.**

This has two implications:
- Any control set via `v4l2-ctl` during debugging persists until explicitly cleared or until a USB power cycle / hardware reset.
- The code's gain-setting logic (`--set-ctrl=gain=<n>`) permanently affects the camera until reversed.

### To truly reset V4L2 control state

**Option A — USB reset (soft, no replug needed):**
```bash
# The app already has _usb_reset_camera() for this. Trigger it by stopping the service
# and running the reset directly:
sudo systemctl stop garden-monitor
python3 -c "
import fcntl, os
USBDEVFS_RESET = 0x5514
for entry in os.scandir('/sys/bus/usb/devices'):
    try:
        v = open(entry.path+'/idVendor').read().strip()
        p = open(entry.path+'/idProduct').read().strip()
    except OSError:
        continue
    if v == '046d' and p == '0843':
        bus = int(open(entry.path+'/busnum').read())
        dev = int(open(entry.path+'/devnum').read())
        path = f'/dev/bus/usb/{bus:03d}/{dev:03d}'
        fd = os.open(path, os.O_WRONLY)
        fcntl.ioctl(fd, USBDEVFS_RESET, 0)
        os.close(fd)
        print('Reset sent to', path)
"
sudo systemctl start garden-monitor
```

**Option B — Manually clear the stuck gain:**
```bash
v4l2-ctl -d /dev/video0 --set-ctrl=gain=0
```
`gain=0` is the C930e's firmware default. Set this before restarting the service if gain was manually overridden during debugging.

**Option C — Physical USB replug** (guaranteed full firmware reset, last resort).

### Diagnosing frozen AE vs mode-stuck AE

```bash
# Poll gain and exposure 5 times, 1 second apart
for i in 1 2 3 4 5; do
  v4l2-ctl -d /dev/video0 --get-ctrl=gain,exposure_time_absolute
  sleep 1
done
```

- **Values changing** → AE is running normally.
- **Values frozen AND `auto_exposure=3`** → AE frozen (this incident's failure mode). Try USB reset or replug.
- **Values frozen AND `auto_exposure=1`** → Mode stuck (Incident 1 failure mode). Run `v4l2-ctl --set-ctrl=auto_exposure=3` or restart.

### What NOT to do when debugging AE issues

- **Do not cap `analogue_gain`** via settings.yaml or the UI while AE is in auto mode. The AE compensates by extending shutter time, achieving the same total exposure. Net effect: zero change to image brightness, but lower gain + longer shutter time (potential motion blur, confused AE state).
- **Do not restart the service repeatedly** while investigating. Each restart resets AE convergence. Allow 2–3 minutes of stable running before concluding the AE is misbehaving.
- **Do not trust `exposure_time_absolute` as a live readback** in auto mode. The register has `flags=inactive` and may show a stale value from the last manual-mode session. Only `gain` reliably reflects the current state (and only if not manually overridden).

### Suspected root cause

The C930e's AE algorithm appears to freeze when `gain` is externally overridden while `auto_exposure=3` is active. Setting gain manually (even once) may put the AE into a state where it treats gain as locked and only manages shutter — but if the shutter register also stops updating its V4L2 readback, the AE appears completely frozen from the outside even though it may be running internally. A USB-level hardware reset is the only reliable recovery.
