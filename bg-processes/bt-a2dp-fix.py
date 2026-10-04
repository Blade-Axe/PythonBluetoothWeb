#!/usr/bin/env python3
"""Reconnect a Bluetooth audio device from the Pi when the audio stream is at risk.

Two triggers. Both work for any device. No address is stored in this file.
1. EARLY: BlueZ logs a transport path with no "sepN". This means the remote device
   started the connection. Some devices (many Android phones) get no audio then.
   The fix: block the device, unblock it, then connect from the Pi.
   This runs once for each connection (COOLDOWN).
2. FAILURE: WirePlumber logs "Acquire ... NotAuthorized". The audio stream failed.
   The same fix runs again. At most MAX_TRIES times in WINDOW seconds.
A device that had a failure is also stored in STATE_FILE. This is the "learned" list.
"""
import json
import os
import re
import subprocess
import time

BOUNCE_ALL = True  # True: EARLY trigger for every device that starts the connection.
                   # False: EARLY trigger only for devices on the learned list.
MAX_TRIES = 3      # FAILURE reconnects for one device in one window
WINDOW = 120       # seconds before the try counter resets
COOLDOWN = 90      # seconds after a reconnect with no EARLY trigger
STATE_FILE = os.environ.get("BT_FIX_STATE", "/var/lib/bt-a2dp-fix/known.json")

MAC = r"([0-9A-F]{2}(?:_[0-9A-F]{2}){5})"
FAILED = re.compile(
    r"spa\.bluez5: Acquire /org/bluez/hci\d+/dev_" + MAC + r"/\S+ "
    r"returned error: org\.bluez\.Error\.NotAuthorized"
)
READY = re.compile(r"/org/bluez/hci\d+/dev_" + MAC + r"(/sep\d+)?/fd\d+: fd\(\d+\) ready")
STAMP = re.compile(r"^(\d+\.\d+)\s")

known = set()      # devices that had a stream failure
last_connect = {}  # mac -> time when this script sent "connect"
last_bounce = {}   # mac -> time when this script started a reconnect
tries = {}         # mac -> (count, first_time)


def log(text):
    print(text, flush=True)


def load():
    try:
        with open(STATE_FILE) as f:
            known.update(json.load(f))
    except (OSError, ValueError):
        pass


def learn(mac):
    if mac in known:
        return
    known.add(mac)
    log(f"{mac}: stream failure stored in the learned list")
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(sorted(known), f)
    except OSError as e:
        log(f"cannot write {STATE_FILE}: {e}")


def seed():
    """Read old failures of this boot, so the learned list is full at start."""
    try:
        out = subprocess.run(
            ["journalctl", "-b", "--no-pager", "-o", "short-unix",
             "--grep", "Acquire .* returned error: org.bluez.Error.NotAuthorized"],
            capture_output=True, text=True, timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return
    for line in out.splitlines():
        m = FAILED.search(line)
        if m:
            learn(m.group(1).replace("_", ":"))


def bt(*args):
    subprocess.run(["bluetoothctl", *args], capture_output=True, timeout=30)


def reconnect(mac, why):
    log(f"{mac}: {why}, reconnect from the Pi")
    last_bounce[mac] = time.time()
    try:
        bt("block", mac)  # also disconnects, so the device cannot reconnect first
        time.sleep(1)
    finally:
        bt("unblock", mac)  # always unblock, even after an error
    time.sleep(1)
    last_connect[mac] = time.time()
    bt("connect", mac)


def early(mac, stamp):
    if stamp < last_connect.get(mac, 0):
        return  # old line from before the last reconnect
    if time.time() - last_bounce.get(mac, 0) < COOLDOWN:
        return
    reconnect(mac, "the device started the connection")


def failed(mac, stamp):
    if stamp < last_connect.get(mac, 0):
        return  # old line from before the last reconnect
    now = time.time()
    count, first = tries.get(mac, (0, now))
    if now - first > WINDOW:
        count, first = 0, now
    if count >= MAX_TRIES:
        log(f"{mac}: {MAX_TRIES} reconnects failed, wait {WINDOW} s")
        return
    tries[mac] = (count + 1, first)
    reconnect(mac, "the audio stream failed")


def handle(line):
    s = STAMP.match(line)
    stamp = float(s.group(1)) if s else time.time()
    m = FAILED.search(line)
    if m:
        mac = m.group(1).replace("_", ":")
        learn(mac)
        failed(mac, stamp)
        return
    m = READY.search(line)
    if m and m.group(2) is None:  # no "sepN": the remote device started the connection
        mac = m.group(1).replace("_", ":")
        if BOUNCE_ALL or mac in known:
            early(mac, stamp)


def main():
    load()
    seed()
    log(f"learned list: {sorted(known) or 'empty'}, BOUNCE_ALL={BOUNCE_ALL}")
    proc = subprocess.Popen(
        ["journalctl", "-f", "-n", "0", "-o", "short-unix"],
        stdout=subprocess.PIPE,
        text=True,
    )
    for line in proc.stdout:
        handle(line)


if __name__ == "__main__":
    main()