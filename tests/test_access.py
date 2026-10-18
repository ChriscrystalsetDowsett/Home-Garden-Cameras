"""Signed-in devices: password sign-in, invites, revocation, tunnel rules."""
import re

import pytest

PW = "correct horse"
REMOTE = {"REMOTE_ADDR": "192.168.4.50"}
TUNNEL = {"Cf-Connecting-Ip": "203.0.113.9"}
IPHONE = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)"}


@pytest.fixture()
def acc(tmp_path, monkeypatch, flask_app):
    import app.access as access
    import app.dashboard as dash
    monkeypatch.setattr(access, "DEVICES_FILE", tmp_path / "devices.json")
    monkeypatch.setattr(access, "DASHBOARD_PASSWORD", PW)
    monkeypatch.setattr(dash, "DASHBOARD_PASSWORD", PW)
    return access


def _sign_in(client, name="Test phone", headers=None):
    return client.post("/dashboard/login", data={"password": PW, "name": name},
                       headers=headers or {})


def test_internet_needs_sign_in(acc, client):
    for path in ("/", "/dashboard", "/camera/0/"):
        r = client.get(path, headers=TUNNEL)
        assert r.status_code == 302 and r.headers["Location"].endswith("/dashboard/login"), path
    for path in ("/api/frame", "/api/gallery", "/api/audio/stream/raw", "/snapshots/x.jpg"):
        assert client.get(path, headers=TUNNEL).status_code == 401, path
    assert client.get("/dashboard/login", headers=TUNNEL).status_code == 200
    assert client.get("/static/ptz.js", headers=TUNNEL).status_code == 200


def test_lan_viewing_stays_open(acc, client):
    assert client.get("/api/stats", environ_base=REMOTE).status_code == 200


def test_wrong_password_rejected(acc, client):
    r = client.post("/dashboard/login", data={"password": "nope"}, headers=TUNNEL)
    assert r.status_code == 200 and b"Incorrect password" in r.data
    assert acc.all_devices() == []


def test_sign_in_remembers_device_for_a_year(acc, client):
    r = _sign_in(client, headers={**TUNNEL, **IPHONE})
    assert r.status_code == 302
    cookie = r.headers["Set-Cookie"]
    assert "Expires=" in cookie            # permanent, not a browser-session cookie
    assert client.get("/dashboard/devices", headers=TUNNEL).status_code == 200
    assert client.get("/api/gallery", headers=TUNNEL).status_code == 200
    devs = acc.all_devices()
    assert len(devs) == 1 and devs[0]["name"] == "Test phone"


def test_signed_in_device_can_write_from_lan(acc, client):
    assert client.post("/api/camera/enabled", json={"enabled": True},
                       environ_base=REMOTE).status_code == 401
    _sign_in(client)
    assert client.post("/api/camera/enabled", json={"enabled": True},
                       environ_base=REMOTE).status_code == 200


def test_signing_out_a_device_ends_its_session(acc, client):
    _sign_in(client)
    dev = acc.all_devices()[0]
    acc.sign_out(dev["id"])                 # e.g. from another device's Devices page
    assert client.get("/api/gallery", headers=TUNNEL).status_code == 401


def test_invite_signs_in_new_device_once(acc, flask_app):
    a = flask_app.test_client()
    _sign_in(a, name="Chris iPhone")
    r = a.post("/dashboard/devices/invite", headers=TUNNEL)
    url = r.get_json()["url"]
    code = re.search(r"/dashboard/join/([\w-]+)$", url).group(1)

    b = flask_app.test_client()
    # A link preview (GET) must not use the invite up.
    assert b"Add this device" in b.get(f"/dashboard/join/{code}", headers=TUNNEL).data
    assert b.get(f"/dashboard/join/{code}", headers=TUNNEL).status_code == 200
    r = b.post(f"/dashboard/join/{code}", data={"name": "Caroline iPhone"}, headers=TUNNEL)
    assert r.status_code == 302
    assert b.get("/api/gallery", headers=TUNNEL).status_code == 200
    assert {d["name"] for d in acc.all_devices()} == {"Chris iPhone", "Caroline iPhone"}

    c = flask_app.test_client()                 # second use fails
    r = c.post(f"/dashboard/join/{code}", data={"name": "x"}, headers=TUNNEL)
    assert b"expired" in r.data
    assert c.get("/api/gallery", headers=TUNNEL).status_code == 401


def test_expired_invite_rejected(acc, flask_app, monkeypatch):
    code, _ = acc.create_invite("Chris")
    import time as _t
    real = _t.time
    monkeypatch.setattr(acc.time, "time", lambda: real() + acc.INVITE_TTL_S + 1)
    b = flask_app.test_client()
    r = b.post(f"/dashboard/join/{code}", data={"name": "late"}, headers=TUNNEL)
    assert b"expired" in r.data


def test_legacy_session_is_adopted(acc, client):
    # A browser signed in before devices existed keeps working, once.
    with client.session_transaction() as s:
        s["dashboard_authed"] = True
    assert client.get("/api/gallery", headers=TUNNEL).status_code == 200
    assert len(acc.all_devices()) == 1
    assert client.get("/api/gallery", headers=TUNNEL).status_code == 200
    assert len(acc.all_devices()) == 1


def test_legacy_adoption_makes_one_device_for_parallel_requests(acc, flask_app):
    # Several tiles load at once from one browser, all carrying the old cookie.
    clients = [flask_app.test_client() for _ in range(3)]
    for c in clients:
        with c.session_transaction() as s:
            s["dashboard_authed"] = True
        assert c.get("/api/gallery", headers={**TUNNEL, **IPHONE}).status_code == 200
    assert len(acc.all_devices()) == 1
