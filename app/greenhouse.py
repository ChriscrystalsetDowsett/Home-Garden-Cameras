"""Greenhouse monitor proxy — serves /greenhouse page and forwards API calls to pi4."""
import requests as _requests
from flask import Blueprint, Response, make_response, jsonify, render_template, request, \
                  stream_with_context

from .config import GREENHOUSE_HOST, GREENHOUSE_PORT

greenhouse = Blueprint("greenhouse", __name__)

_BASE    = f"http://{GREENHOUSE_HOST}:{GREENHOUSE_PORT}"
_TIMEOUT = 8   # seconds for API calls


# ── Page ───────────────────────────────────────────────────────────────────────

@greenhouse.route("/greenhouse")
@greenhouse.route("/greenhouse/")
def index():
    resp = make_response(render_template("greenhouse.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


# ── API proxy ──────────────────────────────────────────────────────────────────

@greenhouse.route("/greenhouse/api/<path:api_path>", methods=["GET", "POST"])
def api_proxy(api_path):
    """Forward GET/POST requests to pi4's greenhouse-monitor API."""
    url = f"{_BASE}/api/{api_path}"

    fwd_headers = {}
    if request.content_type:
        fwd_headers["Content-Type"] = request.content_type

    try:
        resp = _requests.request(
            method=request.method,
            url=url,
            data=request.get_data(),
            headers=fwd_headers,
            params=request.args,
            timeout=_TIMEOUT,
        )
        return Response(
            resp.content,
            status=resp.status_code,
            content_type=resp.headers.get("Content-Type", "application/json"),
        )
    except _requests.exceptions.ConnectionError:
        return jsonify({"error": "pi4 offline"}), 502
    except _requests.exceptions.Timeout:
        return jsonify({"error": "pi4 timeout"}), 504
