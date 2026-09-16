"""Shared fixtures.

The Flask app is imported once per session with the real settings.yaml.
Camera hardware is absent on the dev box: the capture thread exits cleanly
(guarded imports) and tests monkeypatch `camera.capture` where needed.
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Fixed token so auth tests don't depend on the local settings.yaml secret.
TEST_TOKEN = "test-token-0123456789abcdef0123456789abcdef"


@pytest.fixture(scope="session")
def flask_app():
    from app.app import app
    app.config.update(TESTING=True, API_TOKEN=TEST_TOKEN)
    return app


@pytest.fixture()
def client(flask_app):
    # Werkzeug's test client uses REMOTE_ADDR=127.0.0.1 by default, which
    # matches the pi4 cron perspective (loopback bypass active).
    return flask_app.test_client()


@pytest.fixture()
def snapshot_dir(tmp_path, monkeypatch):
    """Redirect the snapshot routes at a throwaway directory."""
    import app.app as app_module
    d = tmp_path / "photos"
    d.mkdir()
    monkeypatch.setattr(app_module, "SNAPSHOT_DIR", d)
    return d


@pytest.fixture()
def mock_capture(monkeypatch, snapshot_dir):
    """Stand-in for CameraManager.capture: writes a real JPEG using the
    production filename format and returns the filename, so the route's
    thumbnail + response logic runs for real."""
    from app.camera import camera

    def fake_capture(prefix="Photo", filter_name="none", quality=85):
        filename = f"{prefix}_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.jpg"
        Image.new("RGB", (64, 48), (20, 90, 20)).save(
            snapshot_dir / filename, "JPEG", quality=quality)
        return filename

    monkeypatch.setattr(camera, "capture", fake_capture)
    return fake_capture
