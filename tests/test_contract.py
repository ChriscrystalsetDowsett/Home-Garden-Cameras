"""Greenhouse-pipeline contract tests — see the plan's Hard Invariants.

Any change that breaks one of these breaks the journal-v2 ingestion on bigpc
and is a hard revert:
  #1  POST /api/snapshot {"label": "GH"} -> 200 {"ok": true, "filename": ...}
  #2  filename format <prefix>_YYYY-MM-DD_HH-MM-SS.jpg + .thumb.jpg sidecar
  #8  response fields never renamed/removed (adding is fine)
  #9  GET / returns 200 (watchdog target)
"""
import re

GH_FILENAME_RE = re.compile(r"^GH_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.jpg$")


def test_snapshot_gh_label_shape(client, mock_capture):
    r = client.post("/api/snapshot", json={"label": "GH"})
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] is True
    assert GH_FILENAME_RE.match(d["filename"]), d["filename"]


def test_snapshot_writes_primary_and_thumb(client, mock_capture, snapshot_dir):
    d = client.post("/api/snapshot", json={"label": "GH"}).get_json()
    primary = snapshot_dir / d["filename"]
    thumb = snapshot_dir / (primary.stem + ".thumb.jpg")
    assert primary.is_file()
    assert thumb.is_file()


def test_snapshot_label_prefix_passthrough(client, mock_capture):
    d = client.post("/api/snapshot", json={"label": "T"}).get_json()
    assert d["filename"].startswith("T_")


def test_snapshot_default_label(client, mock_capture):
    d = client.post("/api/snapshot", json={}).get_json()
    assert d["filename"].startswith("Photo_")


def test_root_returns_200(client):
    assert client.get("/").status_code == 200


def test_health_shape(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    d = r.get_json()
    assert d["status"] == "ok"
    for field in ("camera", "backend", "resolution", "frame_age_s", "disk_free_pct"):
        assert field in d, field


def test_capture_failure_returns_500_with_error_code(client, monkeypatch, snapshot_dir):
    from app.camera import camera
    monkeypatch.setattr(camera, "capture",
                        lambda prefix="Photo", filter_name="none", quality=85: None)
    r = client.post("/api/snapshot", json={"label": "GH"})
    assert r.status_code == 500
    d = r.get_json()
    assert d["ok"] is False
    assert d["error"] == "CAPTURE_FAILED"


def test_disk_full_returns_507(client, monkeypatch, mock_capture):
    import app.app as app_module
    monkeypatch.setattr(app_module, "_disk_free_pct", lambda p: 2.0)
    r = client.post("/api/snapshot", json={"label": "GH"})
    assert r.status_code == 507
    assert r.get_json()["error"] == "DISK_FULL"


def test_gallery_shape(client, mock_capture, snapshot_dir):
    import app.app as app_module
    import os
    d = client.post("/api/snapshot", json={"label": "GH"}).get_json()
    entries = client.get("/api/gallery").get_json()
    assert isinstance(entries, list)
    match = [e for e in entries if e["filename"] == d["filename"]]
    assert match, "captured file missing from gallery"
    for field in ("filename", "size", "has_thumb", "local"):
        assert field in match[0]


def test_delete_snapshot_path_traversal_rejected(client):
    r = client.delete("/api/snapshot/..%2F..%2Fetc%2Fpasswd")
    assert r.status_code in (400, 404)
