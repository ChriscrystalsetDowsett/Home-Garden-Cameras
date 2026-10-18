"""Timelapse engine: schedule, frame count, motion path, presets (fake camera + rig)."""
import time

import pytest


@pytest.fixture()
def tl(tmp_path, monkeypatch):
    import app.timelapse as T
    from app.camera import camera
    monkeypatch.setattr(T, "SESSIONS_DIR", tmp_path / "sessions")
    built = []
    monkeypatch.setattr(T, "compile_session", lambda folder: built.append(folder))
    shots = []

    def fake_capture(prefix="Photo", full_res=True, dest_dir=None, description=None, **kw):
        shots.append((time.time(), prefix, full_res))
        (dest_dir / f"{prefix}.jpg").write_bytes(b"x")
        return f"{prefix}.jpg"
    monkeypatch.setattr(camera, "capture", fake_capture)
    mgr = T.TimeLapseManager()
    return T, mgr, shots, built


def _wait(mgr, timeout=10):
    end = time.time() + timeout
    while mgr.running and time.time() < end:
        time.sleep(0.05)


def test_frames_follow_the_schedule_not_capture_time(tl):
    T, mgr, shots, built = tl
    mgr.start(0.5, 2.0, quality="hd")
    _wait(mgr)
    assert mgr.count == 5 and mgr.missed == 0           # 0, .5, 1, 1.5, 2 s
    gaps = [b[0] - a[0] for a, b in zip(shots, shots[1:])]
    assert all(abs(g - 0.5) < 0.15 for g in gaps), gaps
    assert [s[1] for s in shots] == [f"tl_{k:05d}" for k in range(5)]
    assert len(built) == 1


def test_4k_needs_a_long_enough_interval(tl):
    T, mgr, *_ = tl
    with pytest.raises(ValueError):
        mgr.start(2, 60, quality="4k")


def test_motion_needs_a_rig(tl, monkeypatch):
    T, mgr, *_ = tl
    from app.servo import servo
    monkeypatch.setattr(type(servo), "enabled", property(lambda self: False))
    with pytest.raises(ValueError):
        mgr.start(1, 10, motion={"from": None, "to": None})


def test_motion_path_eases_between_poses(tl, monkeypatch):
    T, mgr, shots, built = tl
    from app.servo import servo
    moves = []
    monkeypatch.setattr(type(servo), "enabled", property(lambda self: True))
    monkeypatch.setattr(servo, "pose_of", lambda spec: (spec["pan"], spec["tilt"]))
    monkeypatch.setattr(servo, "limits", lambda: {"pan": 60, "tilt": (-40, 30)})
    monkeypatch.setattr(servo, "begin_motion", lambda info: None)
    monkeypatch.setattr(servo, "end_motion", lambda: None)
    monkeypatch.setattr(servo, "move_for_motion", lambda p, t, direct=True: moves.append((round(p, 2), round(t, 2), direct)))
    monkeypatch.setattr(T, "MOTION_SETTLE_S", 0)
    mgr.start(0.5, 2.0, quality="hd", motion={"from": {"pan": -30, "tilt": 0}, "to": {"pan": 30, "tilt": -10}})
    _wait(mgr)
    frames = [m for m in moves if m[2]][1:]       # direct moves after the start pose
    assert (mgr.count, mgr.missed, len(frames)) == (5, 0, 5), moves
    assert frames[0][:2] == (-30, 0) and frames[-1][:2] == (30, -10)
    assert frames[2][:2] == (0, -5)                # smooth ease: half way at the middle frame
    assert frames[1][0] - frames[0][0] < frames[2][0] - frames[1][0]   # starts slowly


def test_presets_resolve_to_concrete_motion(monkeypatch):
    import app.timelapse as T
    from app.servo import servo
    monkeypatch.setattr(servo, "limits", lambda: {"pan": 60, "tilt": (-40, 30)})
    monkeypatch.setattr(servo, "position", lambda: (10.0, -5.0))
    sweep = T.motion_for_preset("sweep")
    assert sweep["from"] == {"pan": -55, "tilt": -5.0} and sweep["to"] == {"pan": 55, "tilt": -5.0}
    rise = T.motion_for_preset("rise")
    assert rise["from"] == {"pan": 10.0, "tilt": -35} and rise["to"]["tilt"] == -5.0
    with pytest.raises(ValueError):
        T.motion_for_preset("glide", "a", "a")
