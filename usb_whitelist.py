"""
usb_whitelist.py - Device-identity (allow-list) layer for Anti-BadUSB.

Demonstrates the Access-Control principle: a keyboard-class HID device is
trusted only if its identity (VID, PID and, when available, serial number)
appears in a whitelist file. Any NEW keyboard that is not on the list
triggers a callback (the detector blocks input until it is unplugged).

Windows 10/11 only (uses PowerShell's Get-PnpDevice, no extra pip packages).

Usage:
    python usb_whitelist.py --enroll     # trust every keyboard plugged in now
    python usb_whitelist.py --list       # show keyboards and their status
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, asdict

DEFAULT_WHITELIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'whitelist.json')

# HID\VID_046D&PID_C31C&MI_00\7&2A1B3C4D&0&0000  or  USB\VID_046D&PID_C31C\ABC123
_ID_RE = re.compile(r'VID_([0-9A-F]{4})&PID_([0-9A-F]{4})', re.IGNORECASE)


@dataclass(frozen=True)
class HIDDevice:
    instance_id: str
    name: str
    vid: str
    pid: str
    serial: str = ''          # '' when the device exposes no iSerialNumber

    def key(self) -> str:
        return f'{self.vid}:{self.pid}' + (f':{self.serial}' if self.serial else '')


def parse_instance_id(instance_id: str, parent_id: str = ''):
    """Return (vid, pid, serial) from a PnP instance ID. Serial comes from the
    parent USB node; Windows invents IDs containing '&' when there is no real
    serial, so those are treated as 'no serial'."""
    m = _ID_RE.search(instance_id) or _ID_RE.search(parent_id or '')
    if not m:
        return None
    vid, pid = m.group(1).upper(), m.group(2).upper()
    serial = ''
    if parent_id and parent_id.upper().startswith('USB\\'):
        tail = parent_id.split('\\')[-1]
        if '&' not in tail:
            serial = tail.upper()
    return vid, pid, serial


def enumerate_keyboards():
    """List present keyboard-class devices that have a USB VID/PID."""
    ps = (
        "Get-PnpDevice -Class Keyboard -PresentOnly | ForEach-Object { "
        "$p = (Get-PnpDeviceProperty -InstanceId $_.InstanceId "
        "-KeyName 'DEVPKEY_Device_Parent' -ErrorAction SilentlyContinue).Data; "
        "[pscustomobject]@{Id=$_.InstanceId; Name=$_.FriendlyName; Parent=$p} "
        "} | ConvertTo-Json -Compress"
    )
    out = subprocess.run(
        ['powershell', '-NoProfile', '-Command', ps],
        capture_output=True, text=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    ).stdout.strip()
    if not out:
        return []
    rows = json.loads(out)
    if isinstance(rows, dict):
        rows = [rows]
    devices = []
    for r in rows:
        parsed = parse_instance_id(r.get('Id') or '', r.get('Parent') or '')
        if parsed:                              # skip PS/2, virtual, Bluetooth-only
            vid, pid, serial = parsed
            devices.append(HIDDevice(r['Id'], r.get('Name') or '', vid, pid, serial))
    return devices


class Whitelist:
    def __init__(self, path: str = DEFAULT_WHITELIST):
        self.path = path
        self.entries = {}                       # key -> description
        if os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                self.entries = {e['key']: e.get('name', '') for e in json.load(f)}

    def is_allowed(self, dev: HIDDevice) -> bool:
        # A serial-specific entry pins one physical unit; a VID:PID entry
        # trusts the whole model (weaker, but many keyboards lack a serial).
        return dev.key() in self.entries or f'{dev.vid}:{dev.pid}' in self.entries

    def add(self, dev: HIDDevice):
        self.entries[dev.key()] = dev.name

    def save(self):
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump([{'key': k, 'name': n} for k, n in self.entries.items()], f, indent=2)


class DeviceWatcher(threading.Thread):
    """Polls keyboard-class devices; reports arrivals not in the whitelist
    and removals of devices it previously reported."""

    def __init__(self, whitelist: Whitelist, on_unknown, on_removed=None,
                 interval: float = 1.0, enumerate_fn=enumerate_keyboards):
        super().__init__(daemon=True)
        self.whitelist = whitelist
        self.on_unknown = on_unknown
        self.on_removed = on_removed
        self.interval = interval
        self.enumerate_fn = enumerate_fn
        self.known = {d.instance_id: d for d in enumerate_fn()}   # baseline at start
        self.flagged = {}
        for d in self.known.values():                              # already plugged in
            if not whitelist.is_allowed(d):
                self.flagged[d.instance_id] = d
                on_unknown(d)

    def poll_once(self):
        current = {d.instance_id: d for d in self.enumerate_fn()}
        for iid, dev in current.items():
            if iid not in self.known and not self.whitelist.is_allowed(dev):
                self.flagged[iid] = dev
                self.on_unknown(dev)
        for iid in list(self.flagged):
            if iid not in current:
                dev = self.flagged.pop(iid)
                if self.on_removed:
                    self.on_removed(dev, still_flagged=bool(self.flagged))
        self.known = current

    def run(self):
        while True:
            try:
                self.poll_once()
            except Exception as err:
                print(f'[whitelist] poll error: {err}')
            time.sleep(self.interval)


if __name__ == '__main__':
    wl = Whitelist()
    devs = enumerate_keyboards()
    if '--enroll' in sys.argv:
        for d in devs:
            wl.add(d)
        wl.save()
        print(f'Enrolled {len(devs)} keyboard(s) into {wl.path}')
    for d in devs:
        status = 'ALLOWED' if wl.is_allowed(d) else 'UNKNOWN'
        print(f'[{status}] {d.key():<28} {d.name}  ({d.instance_id})')
