"""
Quest Controller Button Monitor

A standalone diagnostic tool: connects to your Quest over ADB and shows a
live log of every button press/release event from the headset and both
controllers, with the raw event code and a friendly name where known.

Use this to figure out exactly which code corresponds to a button you
want to use elsewhere (e.g. as a trigger for the Hz Switcher).

Requires:
  - Python 3.8+
  - tkinter (bundled with most Python installs)
  - adb (Android platform-tools) available on PATH, or adb.exe next to
    this script
"""

import tkinter as tk
from tkinter import ttk
import threading
import queue
import re
import subprocess
import time
import datetime

from hz_core import adb, adb_path, CREATION_FLAGS, detect_usb_device

PORT = "5555"

# Known Linux input-event-codes.h BTN_* codes relevant to VR controllers
# and the Quest's own buttons. Not exhaustive - Quest controllers route
# some buttons (notably face buttons A/B/X/Y on some firmware versions)
# through a path that may not appear here at all.
KNOWN_CODES = {
    "009f": "Oculus / Meta button (headset)",
    "0073": "Volume up",
    "0072": "Volume down",
    "0066": "Power button",
    "0130": "BTN_A / BTN_SOUTH (face button A/X)",
    "0131": "BTN_B / BTN_EAST (face button B/Y)",
    "0132": "BTN_C",
    "0133": "BTN_X / BTN_NORTH (face button A/X, alt mapping)",
    "0134": "BTN_Y / BTN_WEST (face button B/Y, alt mapping)",
    "0135": "BTN_Z",
    "0136": "BTN_TL (left trigger/bumper)",
    "0137": "BTN_TR (right trigger/bumper)",
    "0138": "BTN_TL2 (left grip)",
    "0139": "BTN_TR2 (right grip)",
    "013a": "BTN_SELECT",
    "013b": "BTN_START",
    "013c": "BTN_MODE",
    "013d": "BTN_THUMBL (left thumbstick click)",
    "013e": "BTN_THUMBR (right thumbstick click)",
    "0120": "BTN_JOYSTICK / BTN_TRIGGER",
    "0121": "BTN_THUMB",
    "0122": "BTN_THUMB2",
}


def find_any_quest_serial(timeout=2):
    """Return the first adb serial seen (USB or already-connected Wi-Fi)."""
    out = adb(["devices"], capture=True, timeout=timeout)
    if not out:
        return None
    for line in out.splitlines():
        if line.endswith("\tdevice"):
            return line.split("\t")[0]
    return None


class ButtonMonitorApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Quest Controller Button Monitor")
        self.geometry("620x520")
        self.minsize(560, 420)

        self._log_queue = queue.Queue()
        self._stop_flag = threading.Event()
        self._proc = None
        self._press_times = {}  # code -> press timestamp, for hold duration
        self._listener_thread = None
        self.serial = None

        self._build_ui()
        self._poll_log_queue()
        self._schedule_connection_check()

    # ---------------- UI ----------------

    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}

        status_frame = ttk.Frame(self)
        status_frame.pack(fill="x", **pad)

        self.status_dot = tk.Canvas(status_frame, width=14, height=14, highlightthickness=0)
        self.status_dot.pack(side="left", padx=(0, 8))
        self._draw_status(False)

        self.status_label = ttk.Label(status_frame, text="Not connected", font=("Segoe UI", 11, "bold"))
        self.status_label.pack(side="left")

        self.start_btn = ttk.Button(status_frame, text="Connect & Start Listening", command=self._on_start)
        self.start_btn.pack(side="right")
        self.stop_btn = ttk.Button(status_frame, text="Stop", command=self._on_stop, state="disabled")
        self.stop_btn.pack(side="right", padx=(0, 8))

        info_label = ttk.Label(
            self,
            text="Press buttons on the headset and both controllers one at a time. "
                 "Every press/release will show up below with its code and held duration.",
            wraplength=580, justify="left",
        )
        info_label.pack(fill="x", padx=10, pady=(0, 6))

        clear_btn = ttk.Button(self, text="Clear log", command=self._clear_log)
        clear_btn.pack(anchor="e", padx=10)

        adv_frame = ttk.Frame(self)
        adv_frame.pack(fill="x", padx=10, pady=(0, 4))

        self.list_devices_btn = ttk.Button(
            adv_frame, text="List input devices (getevent -p)",
            command=self._on_list_devices, state="disabled",
        )
        self.list_devices_btn.pack(side="left")

        self.raw_mode_var = tk.BooleanVar(value=False)
        raw_check = ttk.Checkbutton(
            adv_frame, text="Raw mode (show ALL events, not just key press/release)",
            variable=self.raw_mode_var,
        )
        raw_check.pack(side="left", padx=12)

        self.labeled_mode_var = tk.BooleanVar(value=False)
        labeled_check = ttk.Checkbutton(
            adv_frame, text="Use getevent -l (labeled, alt parser)",
            variable=self.labeled_mode_var,
        )
        labeled_check.pack(side="left")

        log_frame = ttk.LabelFrame(self, text="Live button events")
        log_frame.pack(fill="both", expand=True, padx=10, pady=8)

        self.log_text = tk.Text(log_frame, height=18, state="disabled", wrap="word", font=("Consolas", 10))
        self.log_text.pack(fill="both", expand=True, padx=8, pady=8)

        # color tags
        self.log_text.tag_config("press", foreground="#2ecc71")
        self.log_text.tag_config("release", foreground="#e74c3c")
        self.log_text.tag_config("info", foreground="#888888")

    def _draw_status(self, connected: bool):
        self.status_dot.delete("all")
        color = "#2ecc71" if connected else "#e74c3c"
        self.status_dot.create_oval(2, 2, 12, 12, fill=color, outline="")

    def _clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

    # ---------------- pre-start usb/connection detection ----------------

    def _schedule_connection_check(self):
        if self._listener_thread is None:  # only poll while not actively listening
            threading.Thread(target=self._check_connection_bg, daemon=True).start()
        self.after(2000, self._schedule_connection_check)

    def _check_connection_bg(self):
        serial = find_any_quest_serial(timeout=2)
        self._log_queue.put(("pre_status", serial))

    # ---------------- actions ----------------

    def _on_start(self):
        if self._listener_thread is not None:
            return
        self.start_btn.config(state="disabled")
        self._append_log("Looking for headset (USB or Wi-Fi)...", "info")
        threading.Thread(target=self._connect_and_listen, daemon=True).start()

    def _connect_and_listen(self):
        serial = find_any_quest_serial(timeout=5)
        if not serial:
            self._log_queue.put(("connect_failed", "No device found over USB or Wi-Fi."))
            return

        # If it's a raw USB serial, try to also bring up Wi-Fi so the cable
        # can be removed - but USB alone is enough to listen for buttons.
        self.serial = serial
        self._log_queue.put(("connected", serial))

        self._stop_flag.clear()
        self._listen_loop()

    def _on_list_devices(self):
        if not self.serial:
            return
        self.list_devices_btn.config(state="disabled")
        threading.Thread(target=self._run_list_devices, daemon=True).start()

    def _run_list_devices(self):
        out = adb(["shell", "getevent", "-pl"], self.serial, capture=True, timeout=5)
        self._log_queue.put(("devices", out or "(no output / command failed)"))
        self._log_queue.put(("_reenable_list_devices", None))

    def _on_stop(self):
        self._stop_flag.set()
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
        self._listener_thread = None
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self._draw_status(False)
        self.status_label.config(text="Not connected")
        self._append_log("Stopped listening.", "info")

    # ---------------- listening ----------------

    def _listen_loop(self):
        self._listener_thread = threading.current_thread()
        time_re = re.compile(r"\[\s*(\d+\.\d+)\]")
        code_re = re.compile(r"0001\s+([0-9a-fA-F]{4})\s+([0-9a-fA-F]{8})")
        # Labeled format example: [   123.456] /dev/input/event3: EV_KEY       BTN_B               DOWN
        labeled_re = re.compile(r"EV_KEY\s+(\S+)\s+(DOWN|UP|REPEAT)")

        while not self._stop_flag.is_set():
            getevent_flag = "-tl" if self.labeled_mode_var.get() else "-t"
            try:
                self._proc = subprocess.Popen(
                    [adb_path(), "-s", self.serial, "shell", "getevent", getevent_flag],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    creationflags=CREATION_FLAGS,
                )
            except Exception as e:
                self._log_queue.put(("connect_failed", f"Couldn't start getevent: {e}"))
                return

            for line in self._proc.stdout:
                if self._stop_flag.is_set():
                    break
                raw_line = line.strip()
                if not raw_line:
                    continue

                if self.raw_mode_var.get() or self.labeled_mode_var.get():
                    self._log_queue.put(("raw", raw_line))

                if self.labeled_mode_var.get():
                    lm = labeled_re.search(raw_line)
                    if lm:
                        name, state = lm.group(1), lm.group(2)
                        if state == "DOWN":
                            self._log_queue.put(("press", name))
                        elif state == "UP":
                            self._log_queue.put(("release", (name, None)))
                    continue

                tm = time_re.search(raw_line)
                cm = code_re.search(raw_line)
                if not tm or not cm:
                    continue
                now = float(tm.group(1))
                code = cm.group(1).lower()
                value = cm.group(2)

                if value == "00000001":
                    self._press_times[code] = now
                    self._log_queue.put(("press", code))
                elif value == "00000000":
                    press_t = self._press_times.pop(code, None)
                    held = (now - press_t) if press_t is not None else None
                    self._log_queue.put(("release", (code, held)))

            if not self._stop_flag.is_set():
                self._log_queue.put(("info", "Connection interrupted, retrying..."))
                time.sleep(1)

    # ---------------- logging ----------------

    def _append_log(self, text, tag=None):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_text.config(state="normal")
        self.log_text.insert("end", f"[{ts}] {text}\n", tag or "")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _poll_log_queue(self):
        try:
            while True:
                kind, payload = self._log_queue.get_nowait()
                if kind == "pre_status":
                    if self._listener_thread is None:
                        if payload:
                            self._draw_status(False)
                            self.status_label.config(text=f"Device detected ({payload}) — not listening yet")
                        else:
                            self._draw_status(False)
                            self.status_label.config(text="Not connected")
                elif kind == "connect_failed":
                    self._append_log(f"Connection failed: {payload}", "info")
                    self.start_btn.config(state="normal")
                elif kind == "connected":
                    self._draw_status(True)
                    self.status_label.config(text=f"Listening on {payload}")
                    self.stop_btn.config(state="normal")
                    self.list_devices_btn.config(state="normal")
                    self._append_log(f"Connected to {payload}. Listening for button events...", "info")
                elif kind == "raw":
                    self._append_log(payload, "info")
                elif kind == "devices":
                    self._append_log("--- Input devices (getevent -pl) ---", "info")
                    for ln in payload.splitlines():
                        self._append_log(ln, "info")
                    self._append_log("--- end of device list ---", "info")
                elif kind == "_reenable_list_devices":
                    if self._listener_thread is not None:
                        self.list_devices_btn.config(state="normal")
                elif kind == "press":
                    code = payload
                    name = KNOWN_CODES.get(code, "")
                    label = f"{code} ({name})" if name else code
                    self._append_log(f"PRESS    {label}", "press")
                elif kind == "release":
                    code, held = payload
                    name = KNOWN_CODES.get(code, "")
                    label = f"{code} ({name})" if name else code
                    held_str = f"{held:.2f}s" if held is not None else "?"
                    self._append_log(f"RELEASE  {label}  held={held_str}", "release")
                elif kind == "info":
                    self._append_log(payload, "info")
        except queue.Empty:
            pass
        self.after(100, self._poll_log_queue)

    def on_close(self):
        self._stop_flag.set()
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
        self.destroy()


def main():
    app = ButtonMonitorApp()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()


if __name__ == "__main__":
    main()
