"""Backward-compat shim — v2 split camera.py into manager.py + backends/.

Consumers still `from .camera import camera, cam_ctrl, cam_ctrl_lock`; those
names now live in manager.py and are re-exported here so app.py, timelapse.py,
and stats.py need no import changes.
"""
from .manager import camera, cam_ctrl, cam_ctrl_lock

__all__ = ["camera", "cam_ctrl", "cam_ctrl_lock"]
