"""Shared V4L2 helpers — USB device presence + reset.

Extracted from the previous monolithic camera.py so both v4l2_passthrough and
v4l2_opencv paths can share the reset logic without duplicating sysfs walks.
All `open()` calls use `with` blocks to close file descriptors deterministically —
the previous code leaked one FD per read in the sysfs loop, which mattered
after a burst of USB resets on the C930e.
"""
import fcntl
import logging
import os
import time


USBDEVFS_RESET = 0x5514


def _read_sysfs(path):
    """Read a sysfs attribute or return empty string on OSError.

    Uses `with` so the FD is closed even when the attribute is unreadable
    (some transient sysfs entries return -ENOENT on read).
    """
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def find_usb_device(usb_id):
    """Return (busnum, devnum, sysfs_path) for the first matching USB device.

    `usb_id` is "vvvv:pppp" (vendor:product). Returns None if not present.
    """
    vendor, product = usb_id.split(":")
    try:
        entries = os.scandir("/sys/bus/usb/devices")
    except OSError:
        return None

    with entries as it:
        for entry in it:
            if _read_sysfs(os.path.join(entry.path, "idVendor")) != vendor:
                continue
            if _read_sysfs(os.path.join(entry.path, "idProduct")) != product:
                continue
            busnum = _read_sysfs(os.path.join(entry.path, "busnum"))
            devnum = _read_sysfs(os.path.join(entry.path, "devnum"))
            if not (busnum and devnum):
                continue
            try:
                return int(busnum), int(devnum), entry.path
            except ValueError:
                continue
    return None


def usb_reset(usb_id, settle_seconds=3):
    """Send USBDEVFS_RESET to the device — recover a hung C930e without replug.

    Requires plugdev/root write access to /dev/bus/usb (udev rule
    99-c930e-power.rules on pi4/pi5). Sleeps briefly for re-enumeration.
    """
    dev = find_usb_device(usb_id)
    if dev is None:
        logging.warning("usb_reset: %s not found in sysfs", usb_id)
        return False
    busnum, devnum, _ = dev
    dev_path = f"/dev/bus/usb/{busnum:03d}/{devnum:03d}"
    try:
        fd = os.open(dev_path, os.O_WRONLY)
        try:
            fcntl.ioctl(fd, USBDEVFS_RESET, 0)
            logging.info("usb_reset: reset sent to %s", dev_path)
        finally:
            os.close(fd)
        time.sleep(settle_seconds)
        return True
    except OSError as e:
        logging.warning("usb_reset: %s failed: %s", dev_path, e)
        return False
