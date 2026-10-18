#!/usr/bin/env python3
"""Check every camera's health and report problems once, and again when they clear.

Runs from cron on the wall Pi (pi5) every 5 minutes:
    */5 * * * * python3 ~/dev/apps/home-garden-cameras/scripts/fleet-alerts.py

Problems: camera unreachable, camera stalled (not asleep), backup failing or
older than BACKUP_MAX_AGE_H, temperature over TEMP_MAX_C, disk free under
DISK_MIN_FREE_PCT. A problem must be seen on two runs in a row (~10 min)
before it is reported, so a restart or a deploy doesn't page anyone.

Every report goes to ~/fleet-alerts.log. When settings.yaml has
    alerts:
      ntfy_url: "https://ntfy.sh/<long-random-topic>"
it is also pushed there (the ntfy phone app shows it). Empty = log only.
"""
import json
import time
from datetime import datetime
from pathlib import Path

import requests
import yaml

APP = Path(__file__).resolve().parent.parent
STATE = Path.home() / ".fleet-alerts.json"
LOG = Path.home() / "fleet-alerts.log"
BACKUP_MAX_AGE_H = 36
TEMP_MAX_C = 75
DISK_MIN_FREE_PCT = 15


def problems_for(cam, h):
    name = cam["name"]
    if h is None:
        return {f"{name}:down": f"{name} is not answering"}
    out = {}
    if h.get("camera") != "live" and not h.get("sleeping"):
        out[f"{name}:stalled"] = f"{name}: camera stalled (no new frame for {h.get('frame_age_s')} s)"
    sync = h.get("sync") or {}
    last_ok = sync.get("last_ok")
    age_h = None
    if last_ok:
        age_h = (time.time() - datetime.fromisoformat(last_ok).timestamp()) / 3600
    if sync and (sync.get("rc") != 0 or age_h is None or age_h > BACKUP_MAX_AGE_H):
        out[f"{name}:backup"] = (f"{name}: backup to bigPC failing"
                                 + (f" (last good {age_h:.0f} h ago)" if age_h is not None else ""))
    t = h.get("temperature_c")
    if t is not None and t > TEMP_MAX_C:
        out[f"{name}:hot"] = f"{name}: running hot ({t:.0f} °C)"
    d = h.get("disk_free_pct")
    if d is not None and d < DISK_MIN_FREE_PCT:
        out[f"{name}:disk"] = f"{name}: disk nearly full ({d:.0f} % free)"
    return out


def notify(cfg, title, lines):
    stamp = time.strftime("%F %T")
    with open(LOG, "a") as f:
        for line in lines:
            f.write(f"{stamp} {title}: {line}\n")
    url = ((cfg.get("alerts") or {}).get("ntfy_url") or "").strip()
    if url:
        try:
            requests.post(url, data="\n".join(lines).encode(), timeout=10,
                          headers={"Title": f"Cameras: {title}", "Tags": "camera"})
        except requests.RequestException as e:
            with open(LOG, "a") as f:
                f.write(f"{stamp} push failed: {e}\n")


def main():
    cfg = yaml.safe_load(open(APP / "config" / "settings.yaml"))
    seen = {}
    for cam in cfg.get("cameras", []):
        try:
            r = requests.get(f"http://{cam['host']}:{cam['port']}/api/health", timeout=5)
            h = r.json() if r.ok else None
        except (requests.RequestException, ValueError):
            h = None
        seen.update(problems_for(cam, h))

    try:
        state = json.loads(STATE.read_text())
    except (OSError, ValueError):
        state = {"pending": {}, "reported": {}}
    pending, reported = state["pending"], state["reported"]

    new = [msg for key, msg in seen.items() if key in pending and key not in reported]
    cleared = [msg for key, msg in reported.items() if key not in seen]
    reported = {k: v for k, v in reported.items() if k in seen}
    reported.update({k: seen[k] for k in seen if k in pending})
    pending = seen

    if new:
        notify(cfg, "problem", new)
    if cleared:
        notify(cfg, "fixed", ["OK again: " + m for m in cleared])
    STATE.write_text(json.dumps({"pending": pending, "reported": reported,
                                 "checked": time.strftime("%F %T")}, indent=1))


if __name__ == "__main__":
    main()
