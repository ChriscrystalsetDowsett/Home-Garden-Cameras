"""Auth guard behaviour — bearer token on mutating verbs, loopback bypass."""
from .conftest import TEST_TOKEN

REMOTE = {"REMOTE_ADDR": "192.168.4.50"}
MUTATING_URL = "/api/camera/enabled"
PAYLOAD = {"enabled": True}


def test_get_routes_open_from_remote(client):
    assert client.get("/api/health", environ_base=REMOTE).status_code == 200
    assert client.get("/api/stats", environ_base=REMOTE).status_code == 200


def test_mutating_from_remote_without_token_401(client):
    r = client.post(MUTATING_URL, json=PAYLOAD, environ_base=REMOTE)
    assert r.status_code == 401
    d = r.get_json()
    assert d["ok"] is False
    assert d["error"] == "AUTH_REQUIRED"


def test_loopback_bypass_allows_tokenless_post(client):
    # The pi4 cron path: curl -X POST http://localhost:8080/... with no token.
    r = client.post(MUTATING_URL, json=PAYLOAD)
    assert r.status_code == 200


def test_valid_bearer_token_allows_post(client):
    r = client.post(MUTATING_URL, json=PAYLOAD, environ_base=REMOTE,
                    headers={"Authorization": f"Bearer {TEST_TOKEN}"})
    assert r.status_code == 200


def test_wrong_bearer_token_401(client):
    r = client.post(MUTATING_URL, json=PAYLOAD, environ_base=REMOTE,
                    headers={"Authorization": "Bearer wrong-token"})
    assert r.status_code == 401


def test_session_cookie_allows_post(client):
    # The test client's cookie jar owns the Cookie header — set it there
    # rather than via headers=, which the jar silently overrides.
    client.set_cookie("hgc_session", TEST_TOKEN)
    r = client.post(MUTATING_URL, json=PAYLOAD, environ_base=REMOTE)
    assert r.status_code == 200
    client.delete_cookie("hgc_session")


def test_xff_spoof_from_remote_peer_denied(client):
    # Remote socket peer claiming to be loopback via X-Forwarded-For.
    r = client.post(MUTATING_URL, json=PAYLOAD, environ_base=REMOTE,
                    headers={"X-Forwarded-For": "127.0.0.1"})
    assert r.status_code == 401


def test_proxied_request_via_local_proxy_denied(client):
    # Peer is loopback (local reverse proxy) but XFF shows an external client:
    # the bypass must NOT apply — external users need the token.
    r = client.post(MUTATING_URL, json=PAYLOAD,
                    headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 401


def test_xff_spoofed_loopback_via_proxy_denied(client):
    # Attacker sends XFF: 127.0.0.1 through the proxy; proxy appends real IP.
    # Any XFF presence disables the bypass regardless of its content.
    r = client.post(MUTATING_URL, json=PAYLOAD,
                    headers={"X-Forwarded-For": "127.0.0.1, 203.0.113.9"})
    assert r.status_code == 401


def test_delete_verb_also_guarded(client):
    r = client.delete("/api/snapshot/nonexistent.jpg", environ_base=REMOTE)
    assert r.status_code == 401
