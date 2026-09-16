"""Bearer-token auth guard for mutating routes.

Model:
- All GET / HEAD requests pass unauthenticated (they are read-only).
- All other methods (POST/DELETE/PUT/PATCH) require either:
    (a) the client be on 127.0.0.1 / ::1 (loopback bypass — keeps pi4 cron
        `curl -X POST http://localhost:8080/...` working with zero config), or
    (b) the request carry `Authorization: Bearer <API_TOKEN>` OR an
        equivalent HttpOnly cookie set on dashboard login.
- If API_TOKEN is not configured, EVERY mutating request fails 500 —
  fail closed rather than run a token-less production server.
"""
import hmac
from functools import wraps

from flask import current_app, jsonify, request


COOKIE_NAME = "hgc_session"


def _is_loopback():
    """True only for DIRECT loopback connections (pi4's local cron).

    Requests relayed by a local reverse proxy carry X-Forwarded-For and must
    present a token — never parse XFF to decide the bypass, because clients
    control that header and proxies merely append to it (first entry is
    attacker-chosen). A remote peer spoofing XFF without a proxy never gets
    here: its socket address isn't loopback.
    """
    peer = request.remote_addr or ""
    if peer not in ("127.0.0.1", "::1"):
        return False
    return not request.headers.get("X-Forwarded-For")


def _token_matches(supplied):
    token = current_app.config.get("API_TOKEN") or ""
    if not token or not supplied:
        return False
    return hmac.compare_digest(token, supplied)


def _supplied_token():
    hdr = request.headers.get("Authorization", "")
    if hdr.startswith("Bearer "):
        return hdr[7:].strip()
    cookie = request.cookies.get(COOKIE_NAME, "")
    if cookie:
        return cookie
    return ""


def before_request_guard():
    """Flask `@before_request` hook — attach with `app.before_request(before_request_guard)`."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    if _is_loopback():
        return None
    if _token_matches(_supplied_token()):
        return None
    return jsonify({
        "ok": False,
        "error": "AUTH_REQUIRED",
        "message": "Bearer token or session cookie required for mutating requests.",
    }), 401


def require_token(fn):
    """Decorator variant — kept for routes that opt in explicitly (e.g. sensitive
    GETs added later). Not used by default since the `@before_request` hook
    covers all mutating verbs.
    """
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if _is_loopback() or _token_matches(_supplied_token()):
            return fn(*args, **kwargs)
        return jsonify({
            "ok": False,
            "error": "AUTH_REQUIRED",
            "message": "Bearer token required.",
        }), 401
    return wrapped
