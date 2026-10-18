"""Signed-in devices: sign in once per phone or iPad, then stay signed in.

How access works (2026-10-18):
- A device signs in once, with the dashboard password or an invite link from a
  device that is already signed in. Its session cookie lasts a year and renews
  on every visit, so in practice it never asks again.
- Every signed-in device is listed in data/devices.json (name, first and last
  seen). Signing a device out on the Devices page removes it there, which ends
  its session at once, even if its cookie lives on.
- Invite links are single use and expire after INVITE_TTL_S. Opening one only
  shows a page; the device is signed in when someone taps the button, so a
  link preview in Messages cannot use the invite up.
- Requests that come through the Cloudflare tunnel (they carry
  Cf-Connecting-Ip) need a signed-in device for everything except the sign-in
  and invite pages and static files. On the home network, viewing stays open
  as before; changing anything needs a signed-in device or the fleet token.
"""
import fcntl
import json
import os
import secrets
import threading
import time
from pathlib import Path

from flask import jsonify, redirect, request, session

from .config import DASHBOARD_PASSWORD, PROJECT_ROOT

DEVICES_FILE   = PROJECT_ROOT / "data" / "devices.json"
INVITE_TTL_S   = 15 * 60
TOUCH_EVERY_S  = 10 * 60    # write last_seen at most this often per device
SESSION_KEY    = "device"

_lock = threading.Lock()


# ── Store ──────────────────────────────────────────────────────────────────────

def _load():
    try:
        with open(DEVICES_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data.setdefault("devices", {})
    data.setdefault("invites", {})
    return data


def _save(data):
    DEVICES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DEVICES_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.chmod(tmp, 0o600)
    tmp.replace(DEVICES_FILE)


class _Locked:
    """Load, change, save under a thread lock and a file lock."""
    def __enter__(self):
        _lock.acquire()
        DEVICES_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(str(DEVICES_FILE) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        self.data = _load()
        return self.data

    def __exit__(self, exc_type, *_):
        try:
            if exc_type is None:
                _save(self.data)
        finally:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            _lock.release()


def device_name_from_ua(ua):
    ua = ua or ""
    for key, name in (("iPad", "iPad"), ("iPhone", "iPhone"), ("Android", "Android phone"),
                      ("Macintosh", "Mac"), ("Windows", "Windows PC"), ("Linux", "Linux PC")):
        if key in ua:
            return name
    return "Browser"


# ── Devices ────────────────────────────────────────────────────────────────────

def current_device():
    """The signed-in device record for this request, or None."""
    dev_id = session.get(SESSION_KEY)
    if not dev_id:
        # Sessions from before signed-in devices existed: keep them signed in
        # and give them a device record, so nobody has to type the password again.
        if session.get("dashboard_authed") is True:
            return _adopt_legacy_session()
        return None
    data = _load()
    dev = data["devices"].get(dev_id)
    if not dev:
        session.pop(SESSION_KEY, None)
        return None
    if time.time() - dev.get("last_seen", 0) > TOUCH_EVERY_S:
        with _Locked() as d:
            if dev_id in d["devices"]:
                d["devices"][dev_id]["last_seen"] = int(time.time())
                d["devices"][dev_id]["ip"] = _client_ip()
    return dict(dev, id=dev_id)


def _adopt_legacy_session():
    dev = sign_in(device_name_from_ua(request.headers.get("User-Agent")) + " (signed in before 18 Oct)")
    session.pop("dashboard_authed", None)
    return dev


def sign_in(name=None):
    """Register this browser as a signed-in device and remember it for a year."""
    dev_id = secrets.token_urlsafe(16)
    now = int(time.time())
    name = (name or "").strip()[:60] or device_name_from_ua(request.headers.get("User-Agent"))
    rec = {"name": name, "created": now, "last_seen": now, "ip": _client_ip()}
    with _Locked() as d:
        d["devices"][dev_id] = rec
    session.permanent = True
    session[SESSION_KEY] = dev_id
    return dict(rec, id=dev_id)


def sign_out(dev_id):
    with _Locked() as d:
        return d["devices"].pop(dev_id, None) is not None


def rename(dev_id, name):
    name = (name or "").strip()[:60]
    if not name:
        return False
    with _Locked() as d:
        if dev_id not in d["devices"]:
            return False
        d["devices"][dev_id]["name"] = name
        return True


def all_devices():
    devs = [dict(v, id=k) for k, v in _load()["devices"].items()]
    return sorted(devs, key=lambda v: v.get("last_seen", 0), reverse=True)


# ── Invites ────────────────────────────────────────────────────────────────────

def create_invite(by_name):
    code = secrets.token_urlsafe(18)
    now = int(time.time())
    with _Locked() as d:
        d["invites"] = {c: v for c, v in d["invites"].items() if v["expires"] > now}
        d["invites"][code] = {"by": by_name, "created": now, "expires": now + INVITE_TTL_S}
    return code, now + INVITE_TTL_S


def invite_valid(code):
    inv = _load()["invites"].get(code or "")
    return bool(inv) and inv["expires"] > time.time()


def redeem_invite(code, name=None):
    """Use an invite once: sign this device in. Returns the device or None."""
    with _Locked() as d:
        inv = d["invites"].pop(code or "", None)
    if not inv or inv["expires"] <= time.time():
        return None
    return sign_in(name)


# ── Request rules ──────────────────────────────────────────────────────────────

def _client_ip():
    return request.headers.get("Cf-Connecting-Ip") or request.remote_addr or ""


def via_tunnel():
    """True for requests that came in through the Cloudflare tunnel (the internet)."""
    return bool(request.headers.get("Cf-Connecting-Ip"))


def signed_in():
    return not DASHBOARD_PASSWORD or current_device() is not None


_OPEN_PATHS = ("/dashboard/login", "/dashboard/join/", "/static/")


def tunnel_guard():
    """before_request: from the internet, everything needs a signed-in device."""
    if not via_tunnel():
        return None
    p = request.path
    if p.startswith(_OPEN_PATHS) or signed_in():
        return None
    if request.method == "GET" and (p == "/" or p.startswith(("/dashboard", "/camera/"))):
        return redirect("/dashboard/login")
    return jsonify({"ok": False, "error": "AUTH_REQUIRED", "message": "Sign in first."}), 401
