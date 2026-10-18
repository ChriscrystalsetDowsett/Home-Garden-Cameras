"""fleet-alerts.py: which health readings count as problems."""
import importlib.util
from datetime import datetime, timedelta
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "fleet_alerts", Path(__file__).resolve().parent.parent / "scripts" / "fleet-alerts.py")
fa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fa)
CAM = {"name": "Kitchen"}


def ok_health(**kw):
    h = {"camera": "live", "sleeping": False, "temperature_c": 50, "disk_free_pct": 80,
         "sync": {"rc": 0, "last_ok": datetime.now().astimezone().isoformat()}}
    h.update(kw)
    return h


def test_healthy_camera_has_no_problems():
    assert fa.problems_for(CAM, ok_health()) == {}


def test_sleeping_camera_is_not_stalled():
    assert fa.problems_for(CAM, ok_health(camera="stalled", sleeping=True)) == {}


def test_each_problem_is_reported():
    old = (datetime.now().astimezone() - timedelta(hours=40)).isoformat()
    p = fa.problems_for(CAM, ok_health(camera="stalled", temperature_c=80, disk_free_pct=5,
                                        sync={"rc": 0, "last_ok": old}))
    assert set(p) == {"Kitchen:stalled", "Kitchen:hot", "Kitchen:disk", "Kitchen:backup"}
    assert fa.problems_for(CAM, None) == {"Kitchen:down": "Kitchen is not answering"}
