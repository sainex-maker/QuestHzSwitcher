"""
Core logic for the SaineX.

Connects to a Meta Quest headset over ADB (USB first, then Wi-Fi),
and lets you toggle the rendering swap interval (i.e. effective Hz)
either manually or automatically by holding the Oculus/Meta button
for 0.25-1.5 seconds.

This module has no GUI code in it - it's meant to be driven by a
frontend (see hz_gui.py) but can also be used headlessly.
"""

import threading
import time
import re
import subprocess
import shutil
import platform
import sys
import os

PORT = "5555"

# On Windows we don't want a console flashing up for every adb call.
# On macOS/Linux there is no such flag, so we use 0 (no-op).
if platform.system() == "Windows":
    CREATION_FLAGS = subprocess.CREATE_NO_WINDOW
else:
    CREATION_FLAGS = 0

ADB_EXE_NAME = "adb.exe" if platform.system() == "Windows" else "adb"


def _bundle_dir() -> str:
    """Directory to look for a bundled adb in.
    - If frozen by PyInstaller (--onefile), sys._MEIPASS is the temp extraction dir.
    - Otherwise, it's the folder this script/exe lives in."""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def adb_path() -> str:
    """Return the adb executable to use, preferring a bundled copy next to
    this script/exe, falling back to PATH. Raises a clear error if missing."""
    bundled = os.path.join(_bundle_dir(), ADB_EXE_NAME)
    if os.path.isfile(bundled):
        return bundled

    exe = shutil.which("adb")
    if not exe:
        raise FileNotFoundError(
            "adb not found on PATH. Install Android platform-tools and "
            "make sure the folder containing adb is in your PATH."
        )
    return exe


def adb(cmd, serial=None, capture=False, timeout=None):
    """Run an adb command. Returns stdout (decoded) if capture=True, else None.
    Returns None on any failure (timeout, non-zero exit, adb missing, etc.)."""
    try:
        base = [adb_path()]
    except FileNotFoundError:
        return None

    if serial:
        base += ["-s", serial]
    base += cmd

    try:
        if capture:
            return subprocess.check_output(
                base,
                stderr=subprocess.DEVNULL,
                creationflags=CREATION_FLAGS,
                timeout=timeout,
            ).decode(errors="ignore")
        else:
            subprocess.run(
                base,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATION_FLAGS,
                timeout=timeout,
            )
            return None
    except Exception:
        return None


def detect_usb_device(timeout=2):
    """Lightweight, one-off check for any device connected via raw USB serial
    (i.e. not already an ip:port adb connection). Returns the serial string
    if found, otherwise None. Safe to call before any HzSwitcher exists -
    used by the GUI to show 'USB detected' immediately on launch, before
    the user presses Start."""
    out = adb(["devices"], capture=True, timeout=timeout)
    if not out:
        return None
    for line in out.splitlines():
        if line.endswith("\tdevice"):
            serial = line.split("\t")[0]
            if ":" not in serial:
                return serial
    return None


def detect_button_codes(serial, duration=6.0, on_event=None):
    """Listen to `getevent -t` for `duration` seconds and collect every
    distinct (press/release) event code seen, e.g. '0001 0090 00000001'.
    Returns a list of unique 4-char hex codes (like '009f') seen with a
    press (00000001) transition, in the order first seen.

    on_event, if given, is called with each raw matching line as it arrives -
    useful for live feedback while the user is pressing a button.

    This is a blocking call - run it in a background thread from a GUI."""
    if not serial:
        return []

    seen_codes = []
    seen_set = set()
    code_re = re.compile(r"0001\s+([0-9a-fA-F]{4})\s+00000001")

    try:
        proc = subprocess.Popen(
            [adb_path(), "-s", serial, "shell", "getevent", "-t"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=CREATION_FLAGS,
        )
    except Exception:
        return []

    end_time = time.time() + duration
    try:
        while time.time() < end_time:
            line = proc.stdout.readline()
            if not line:
                time.sleep(0.05)
                continue
            line = line.strip()
            m = code_re.search(line)
            if m:
                code = m.group(1).lower()
                if code not in seen_set:
                    seen_set.add(code)
                    seen_codes.append(code)
                if on_event:
                    on_event(line)
    finally:
        try:
            proc.terminate()
        except Exception:
            pass

    return seen_codes


class HzSwitcher:
    """
    Encapsulates connection state + hz-toggling for a single headset session.

    Usage:
        switcher = HzSwitcher(target_swap=2, on_log=print)
        switcher.start()           # spins up background threads
        switcher.toggle_hz()       # manually flip between normal and target
        switcher.set_target(3)     # change target swap interval live
        switcher.stop()            # stop everything
    """

    DEFAULT_MIN_HOLD = 0.25
    DEFAULT_MAX_HOLD = 1.5

    def __init__(self, target_swap: int = 2, on_log=None, on_status=None, on_conn_type=None,
                 trigger_code="009f", min_hold=None, max_hold=None, on_swap_change=None):
        self.target_swap = target_swap
        self.on_log = on_log or (lambda msg: None)
        self.on_status = on_status or (lambda connected: None)
        self.on_conn_type = on_conn_type or (lambda conn_type: None)
        self.on_swap_change = on_swap_change or (lambda swap_state: None)
        self.trigger_code = trigger_code  # hex event code string, e.g. "009f" for Oculus button
        self.min_hold = min_hold if min_hold is not None else self.DEFAULT_MIN_HOLD
        self.max_hold = max_hold if max_hold is not None else self.DEFAULT_MAX_HOLD

        self.serial = None          # raw adb serial (usb id or ip:port)
        self.ip = None
        self.ready = False
        self.connection_type = None  # "usb" | "wifi" | None
        self.swap_state = 1         # 1 = normal, target_swap = toggled
        self._stop_flag = threading.Event()
        self._getevent_proc = None
        self._threads = []

    # ---------- logging helpers ----------

    def _log(self, msg: str):
        self.on_log(msg)

    def _set_ready(self, value: bool):
        if value != self.ready:
            self.ready = value
            self.on_status(value)

    def _set_conn_type(self, conn_type):
        if conn_type != self.connection_type:
            self.connection_type = conn_type
            self.on_conn_type(conn_type)

    def _usb_cable_present(self) -> bool:
        """Check `adb devices` for any entry that isn't an ip:port (i.e. a real USB serial)."""
        out = adb(["devices"], capture=True, timeout=2)
        if not out:
            return False
        for line in out.splitlines():
            if line.endswith("\tdevice"):
                dev_serial = line.split("\t")[0]
                if ":" not in dev_serial:
                    return True
        return False

    # ---------- connection ----------

    def _find_quest(self):
        self._log("Waiting for Quest (check USB cable / Wi-Fi)...")
        while not self._stop_flag.is_set():
            out = adb(["devices"], capture=True)
            if out:
                for line in out.splitlines():
                    if line.endswith("\tdevice"):
                        return line.split("\t")[0]
            time.sleep(1)
        return None

    def _get_ip(self, serial):
        for _ in range(10):
            if self._stop_flag.is_set():
                return None
            out = adb(["shell", "ip", "-o", "-4", "addr", "show", "wlan0"], serial, capture=True)
            if out:
                m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", out)
                if m:
                    return m.group(1)
            time.sleep(1)
        return None

    def _connect_loop(self):
        """Keeps trying to get a working Wi-Fi adb connection to the headset."""
        while not self._stop_flag.is_set():
            serial = self._find_quest()
            if serial is None:
                return  # stopping

            if ":" not in serial:
                self._set_conn_type("usb")
                self._log(f"Found device over USB ({serial}). Switching to TCP/IP mode...")
                adb(["tcpip", PORT], serial)
                time.sleep(2)

            ip = self._get_ip(serial)
            if not ip:
                self._log("Couldn't get an IP for the headset, retrying...")
                time.sleep(2)
                continue

            self.ip = ip
            self.serial = f"{ip}:{PORT}"
            adb(["connect", self.serial])
            time.sleep(2)

            test = adb(["shell", "echo", "ok"], self.serial, capture=True, timeout=2)
            if test:
                if self._usb_cable_present():
                    self._set_conn_type("usb")
                    self._log(f"Connected to {self.serial} (USB cable still plugged in).")
                else:
                    self._set_conn_type("wifi")
                    self._log(f"Connected to {self.serial} over Wi-Fi. You can unplug the USB cable now.")
                self._set_ready(True)
                return
            self._set_ready(False)
            time.sleep(2)

    def _connection_monitor(self):
        while not self._stop_flag.is_set():
            if not self.ready:
                self._connect_loop()
            else:
                ok = adb(["shell", "echo", "ping"], self.serial, capture=True, timeout=2)
                if not ok:
                    self._log("Lost connection to headset, reconnecting...")
                    self._set_ready(False)
                    self._set_conn_type(None)
                else:
                    new_type = "usb" if self._usb_cable_present() else "wifi"
                    if new_type != self.connection_type:
                        if new_type == "wifi":
                            self._log("USB cable unplugged — now running over Wi-Fi.")
                        else:
                            self._log("USB cable plugged back in.")
                        self._set_conn_type(new_type)
            time.sleep(3)

    # ---------- hz toggling ----------

    def set_target(self, target_swap: int):
        self.target_swap = target_swap
        self._log(f"Target swap interval set to {target_swap}.")

    def toggle_hz(self):
        """Flip between normal (1) and the configured target swap interval."""
        if not self.ready or not self.serial:
            self._log("Can't toggle Hz: not connected yet.")
            return
        self.swap_state = self.target_swap if self.swap_state == 1 else 1
        adb(["shell", "setprop", "debug.oculus.swapInterval", str(self.swap_state)], self.serial)
        self._log(f"Swap interval set to {self.swap_state}.")
        self.on_swap_change(self.swap_state)

    def force_state(self, swap_value: int):
        """Set a specific swap interval directly (used by GUI buttons)."""
        if not self.ready or not self.serial:
            self._log("Can't set Hz: not connected yet.")
            return
        self.swap_state = swap_value
        adb(["shell", "setprop", "debug.oculus.swapInterval", str(self.swap_state)], self.serial)
        self._log(f"Swap interval set to {self.swap_state}.")
        self.on_swap_change(self.swap_state)

    def set_refresh_rate(self, hz: int):
        """Directly set the headset's base display refresh rate (e.g. 60/72/90/120).
        This is independent of swap interval - it changes the actual panel Hz."""
        if not self.ready or not self.serial:
            self._log("Can't set refresh rate: not connected yet.")
            return
        adb(["shell", "setprop", "debug.oculus.refreshRate", str(hz)], self.serial)
        self._log(f"Refresh rate set to {hz} Hz. "
                   f"(Tap the headset's power button off/on if it doesn't apply immediately.)")

    def set_trigger_code(self, code: str, display_name: str = None):
        """Change which button event code triggers the Hz toggle (hex string, e.g. '009f').
        Pass display_name for a human-readable log line instead of the raw code."""
        self.trigger_code = code
        self._log(f"Trigger button set to {display_name or code}.")

    def set_hold_range(self, min_hold: float, max_hold: float):
        """Change the press-and-hold duration window (in seconds) that counts as a trigger."""
        self.min_hold = min_hold
        self.max_hold = max_hold
        self._log(f"Hold duration set to {min_hold:.2f}s - {max_hold:.2f}s.")

    def reset_hold_range(self):
        """Reset hold duration window back to the default (0.25s - 1.5s)."""
        self.min_hold = self.DEFAULT_MIN_HOLD
        self.max_hold = self.DEFAULT_MAX_HOLD
        self._log(f"Hold duration reset to default ({self.DEFAULT_MIN_HOLD:.2f}s - {self.DEFAULT_MAX_HOLD:.2f}s).")

    # ---------- button-hold detection ----------

    def _button_listener(self):
        """Watches getevent for a hold (0.25-1.5s) of self.trigger_code and toggles Hz."""
        press_time = None
        time_re = re.compile(r"\[\s*(\d+\.\d+)\]")

        while not self._stop_flag.is_set():
            if not self.ready:
                time.sleep(1)
                continue
            try:
                self._getevent_proc = subprocess.Popen(
                    [adb_path(), "-s", self.serial, "shell", "getevent", "-t"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    creationflags=CREATION_FLAGS,
                )
            except Exception as e:
                self._log(f"Couldn't start button listener: {e}")
                time.sleep(2)
                continue

            for line in self._getevent_proc.stdout:
                if self._stop_flag.is_set():
                    break
                line = line.strip()
                m = time_re.search(line)
                if not m:
                    continue
                now = float(m.group(1))
                press_code = f"0001 {self.trigger_code} 00000001"
                release_code = f"0001 {self.trigger_code} 00000000"
                if press_code in line:
                    press_time = now
                elif release_code in line:
                    if press_time is not None:
                        held = now - press_time
                        press_time = None
                        if self.min_hold <= held <= self.max_hold:
                            self.toggle_hz()

            if not self._stop_flag.is_set():
                time.sleep(1)  # getevent died (likely disconnect); retry once ready again

    # ---------- lifecycle ----------

    def start(self):
        self._stop_flag.clear()
        t1 = threading.Thread(target=self._connection_monitor, daemon=True)
        t2 = threading.Thread(target=self._button_listener, daemon=True)
        self._threads = [t1, t2]
        t1.start()
        t2.start()
        self._log("Hold the Oculus/Meta button for ~0.5-1s to toggle Hz "
                   "(let go right as the recenter circle appears).")

    def stop(self):
        self._stop_flag.set()
        if self._getevent_proc:
            try:
                self._getevent_proc.terminate()
            except Exception:
                pass
        self._set_ready(False)
        self._set_conn_type(None)
        self._log("Stopped.")
