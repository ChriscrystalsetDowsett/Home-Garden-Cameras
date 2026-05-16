#!/usr/bin/env python3
"""Home Garden Cameras — entry point.

Run directly:  python3 run.py
Systemd:       ExecStart=/usr/bin/python3 /home/<user>/home-garden-cameras/run.py
"""
import os
os.nice(10)   # yield CPU to SSH/network when under load;
              # must be before imports so all spawned camera threads inherit it

from app.config import SERVER_HOST, SERVER_PORT
from app.app import app
from app.camera import camera

if __name__ == "__main__":
    try:
        app.run(host=SERVER_HOST, port=SERVER_PORT, threaded=True)
    finally:
        camera.stop()
