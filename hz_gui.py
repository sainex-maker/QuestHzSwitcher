"""
SaineX - GUI

A small cross-platform desktop app (Windows / macOS / Linux) for toggling
your Quest's rendering swap interval (effective Hz) on the fly, either via
a button in this window or by holding the Oculus/Meta button on the headset.

Requires:
  - Python 3.8+
  - tkinter (bundled with most Python installs)
  - adb (Android platform-tools) available on PATH
"""

import tkinter as tk
from tkinter import ttk
import queue
import datetime
import threading

from hz_core import HzSwitcher, detect_usb_device

# Friendly name -> raw getevent code. Only buttons confirmed reachable
# via getevent on Quest 2 are listed (A/B/X/Y are not visible this way).
TRIGGER_BUTTONS = {
    "Meta button": "009f",
    "Right Trigger": "0137",
    "Right Grip": "0139",
    "Left Trigger": "0136",
    "Left Grip": "0138",
    "Power Button": "0074",
    "Volume Up": "0073",
    "Volume Down": "0072",
}


class HzSwitcherApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SaineX")
        self.geometry("640x620")
        self.minsize(560, 520)

        self._log_queue = queue.Queue()
        self.switcher = None
        self._connected = False
        self._conn_type = None
        self._build_ui()
        self._poll_log_queue()
        self._schedule_pre_start_usb_poll()

    # ---------------- UI ----------------

    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}

        # --- status row ---
        status_frame = ttk.Frame(self)
        status_frame.pack(fill="x", **pad)

        self.status_dot = tk.Canvas(status_frame, width=14, height=14, highlightthickness=0)
        self.status_dot.pack(side="left", padx=(0, 8))
        self._draw_status(False)

        self.status_label = ttk.Label(status_frame, text="Disconnected", font=("Segoe UI", 11, "bold"))
        self.status_label.pack(side="left")

        self.start_btn = ttk.Button(status_frame, text="Start / Connect", command=self._on_start)
        self.start_btn.pack(side="right")
        self.stop_btn = ttk.Button(status_frame, text="Stop", command=self._on_stop, state="disabled")
        self.stop_btn.pack(side="right", padx=(0, 8))

        # --- target swap interval config ---
        config_frame = ttk.LabelFrame(self, text="Target swap interval")
        config_frame.pack(fill="x", **pad)

        row1 = ttk.Frame(config_frame)
        row1.pack(fill="x", padx=8, pady=8)

        ttk.Label(row1, text="Swap interval:").pack(side="left")
        self.swap_var = tk.IntVar(value=2)
        swap_combo = ttk.Combobox(
            row1, textvariable=self.swap_var,
            values=list(range(2, 11)), state="readonly", width=6,
        )
        swap_combo.pack(side="left", padx=8)
        swap_combo.bind("<<ComboboxSelected>>", self._on_swap_change)
        self._on_swap_change()

        # --- direct refresh rate ---
        refresh_frame = ttk.LabelFrame(self, text="Display refresh rate (Hz)")
        refresh_frame.pack(fill="x", **pad)

        refresh_row = ttk.Frame(refresh_frame)
        refresh_row.pack(fill="x", padx=8, pady=8)

        ttk.Label(refresh_row, text="Set base Hz directly:").pack(side="left")

        self.refresh_buttons = {}
        for hz in (60, 72, 90, 120):
            b = ttk.Button(
                refresh_row, text=f"{hz} Hz", width=7,
                command=lambda h=hz: self._on_set_refresh_rate(h),
                state="disabled",
            )
            b.pack(side="left", padx=4)
            self.refresh_buttons[hz] = b

        ttk.Label(
            refresh_frame,
            text="Note: not every headset/app supports every rate (e.g. 120 Hz needs app support).",
            font=("Segoe UI", 8), foreground="#777",
        ).pack(anchor="w", padx=8, pady=(0, 8))

        # --- trigger button config ---
        trigger_frame = ttk.LabelFrame(self, text="Trigger button (hold to toggle Hz)")
        trigger_frame.pack(fill="x", **pad)

        trig_row = ttk.Frame(trigger_frame)
        trig_row.pack(fill="x", padx=8, pady=8)

        ttk.Label(trig_row, text="Button:").pack(side="left")
        self.trigger_name_var = tk.StringVar(value="Meta button")
        trigger_combo = ttk.Combobox(
            trig_row, textvariable=self.trigger_name_var,
            values=list(TRIGGER_BUTTONS.keys()), state="readonly", width=20,
        )
        trigger_combo.pack(side="left", padx=8)
        trigger_combo.bind("<<ComboboxSelected>>", self._on_trigger_name_change)

        self.detect_status_label = ttk.Label(trigger_frame, text="")
        self.detect_status_label.pack(anchor="w", padx=8, pady=(0, 4))

        hold_row = ttk.Frame(trigger_frame)
        hold_row.pack(fill="x", padx=8, pady=(0, 8))

        ttk.Label(hold_row, text="Hold duration (s):").pack(side="left")
        ttk.Label(hold_row, text="min").pack(side="left", padx=(8, 2))
        self.min_hold_var = tk.DoubleVar(value=HzSwitcher.DEFAULT_MIN_HOLD)
        min_hold_spin = ttk.Spinbox(
            hold_row, from_=0.05, to=5.0, increment=0.05, textvariable=self.min_hold_var,
            width=5, command=self._on_hold_change,
        )
        min_hold_spin.pack(side="left")
        min_hold_spin.bind("<Return>", lambda e: self._on_hold_change())
        min_hold_spin.bind("<FocusOut>", lambda e: self._on_hold_change())

        ttk.Label(hold_row, text="max").pack(side="left", padx=(8, 2))
        self.max_hold_var = tk.DoubleVar(value=HzSwitcher.DEFAULT_MAX_HOLD)
        max_hold_spin = ttk.Spinbox(
            hold_row, from_=0.1, to=10.0, increment=0.05, textvariable=self.max_hold_var,
            width=5, command=self._on_hold_change,
        )
        max_hold_spin.pack(side="left")
        max_hold_spin.bind("<Return>", lambda e: self._on_hold_change())
        max_hold_spin.bind("<FocusOut>", lambda e: self._on_hold_change())

        ttk.Button(hold_row, text="Reset to default", command=self._on_hold_reset).pack(side="left", padx=12)

        # --- manual controls ---
        manual_frame = ttk.LabelFrame(self, text="Manual control")
        manual_frame.pack(fill="x", **pad)

        btn_row = ttk.Frame(manual_frame)
        btn_row.pack(fill="x", padx=8, pady=8)

        self.toggle_btn = ttk.Button(btn_row, text="Toggle Hz", command=self._on_toggle, state="disabled")
        self.toggle_btn.pack(side="left")

        self.normal_btn = ttk.Button(btn_row, text="Force Normal", command=lambda: self._on_force(1), state="disabled")
        self.normal_btn.pack(side="left", padx=8)

        self.target_btn = ttk.Button(btn_row, text="Force Target", command=self._on_force_target, state="disabled")
        self.target_btn.pack(side="left")

        self.current_state_label = ttk.Label(manual_frame, text="Current swap interval: -")
        self.current_state_label.pack(anchor="w", padx=8, pady=(0, 8))

        # --- log ---
        log_frame = ttk.LabelFrame(self, text="Log")
        log_frame.pack(fill="both", expand=True, **pad)

        log_inner = ttk.Frame(log_frame)
        log_inner.pack(fill="both", expand=True, padx=8, pady=8)

        log_scroll = ttk.Scrollbar(log_inner, orient="vertical")
        log_scroll.pack(side="right", fill="y")

        self.log_text = tk.Text(
            log_inner, height=16, state="disabled", wrap="word",
            font=("Consolas", 10), yscrollcommand=log_scroll.set,
        )
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.config(command=self.log_text.yview)

    def _draw_status(self, connected: bool):
        self.status_dot.delete("all")
        color = "#2ecc71" if connected else "#e74c3c"
        self.status_dot.create_oval(2, 2, 12, 12, fill=color, outline="")

    def _update_status_label(self):
        if not self._connected:
            self.status_label.config(text="Reconnecting..." if self.switcher else "Disconnected")
            return
        if self._conn_type == "usb":
            self.status_label.config(text="Connected (USB)")
        elif self._conn_type == "wifi":
            self.status_label.config(text="Connected (Wi-Fi)")
        else:
            self.status_label.config(text="Connected")

    def _schedule_pre_start_usb_poll(self):
        """Before the user presses Start, periodically check for a raw USB
        adb device and show 'USB detected' right away instead of just
        'Disconnected'. Stops once a real HzSwitcher session is running."""
        if self.switcher is None:
            threading.Thread(target=self._pre_start_usb_check, daemon=True).start()
        self.after(2000, self._schedule_pre_start_usb_poll)

    def _pre_start_usb_check(self):
        serial = detect_usb_device(timeout=2)
        self._log_queue.put(("pre_start_usb", serial))

    # ---------------- preset handling ----------------

    def _on_swap_change(self, event=None):
        self._apply_target(self.swap_var.get())

    def _on_trigger_name_change(self, event=None):
        name = self.trigger_name_var.get()
        code = TRIGGER_BUTTONS.get(name, "009f")
        if self.switcher:
            self.switcher.set_trigger_code(code, display_name=name)
        else:
            self._queue_log(f"Trigger button set to {name} (will apply once started).")
        self.detect_status_label.config(text=f"Trigger set to: {name}")

    def _on_hold_change(self):
        try:
            min_h = float(self.min_hold_var.get())
            max_h = float(self.max_hold_var.get())
        except (tk.TclError, ValueError):
            return
        if min_h <= 0 or max_h <= min_h:
            self._queue_log("Invalid hold duration: min must be > 0 and less than max.")
            return
        if self.switcher:
            self.switcher.set_hold_range(min_h, max_h)
        else:
            self._queue_log(f"Hold duration set to {min_h:.2f}s - {max_h:.2f}s (will apply once started).")

    def _on_hold_reset(self):
        self.min_hold_var.set(HzSwitcher.DEFAULT_MIN_HOLD)
        self.max_hold_var.set(HzSwitcher.DEFAULT_MAX_HOLD)
        if self.switcher:
            self.switcher.reset_hold_range()
        else:
            self._queue_log(
                f"Hold duration reset to default "
                f"({HzSwitcher.DEFAULT_MIN_HOLD:.2f}s - {HzSwitcher.DEFAULT_MAX_HOLD:.2f}s)."
            )

    def _apply_target(self, value: int):
        if self.switcher:
            self.switcher.set_target(value)
        else:
            self._queue_log(f"Target swap interval set to {value} (will apply once connected).")

    def _current_target(self) -> int:
        return self.swap_var.get()

    # ---------------- actions ----------------

    def _on_start(self):
        if self.switcher:
            return
        self.switcher = HzSwitcher(
            target_swap=self._current_target(),
            on_log=self._queue_log,
            on_status=self._on_status_change,
            on_conn_type=self._on_conn_type_change,
            on_swap_change=self._on_swap_change_cb,
            trigger_code=TRIGGER_BUTTONS.get(self.trigger_name_var.get(), "009f"),
            min_hold=self.min_hold_var.get(),
            max_hold=self.max_hold_var.get(),
        )
        self.switcher.start()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.toggle_btn.config(state="normal")
        self.normal_btn.config(state="normal")
        self.target_btn.config(state="normal")
        for b in self.refresh_buttons.values():
            b.config(state="normal")

    def _on_stop(self):
        if self.switcher:
            self.switcher.stop()
            self.switcher = None
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.toggle_btn.config(state="disabled")
        self.normal_btn.config(state="disabled")
        self.target_btn.config(state="disabled")
        for b in self.refresh_buttons.values():
            b.config(state="disabled")
        self._connected = False
        self._conn_type = None
        self._draw_status(False)
        self._update_status_label()

    def _on_toggle(self):
        if self.switcher:
            self.switcher.toggle_hz()

    def _on_force(self, value):
        if self.switcher:
            self.switcher.force_state(value)

    def _on_force_target(self):
        if self.switcher:
            self.switcher.force_state(self._current_target())

    def _on_set_refresh_rate(self, hz: int):
        if self.switcher:
            self.switcher.set_refresh_rate(hz)

    def _refresh_state_label(self):
        if self.switcher:
            self.current_state_label.config(
                text=f"Current swap interval: {self.switcher.swap_state}"
            )

    def _on_status_change(self, connected: bool):
        # called from a background thread - just queue a UI update
        self._log_queue.put(("status", connected))

    def _on_conn_type_change(self, conn_type):
        # called from a background thread - just queue a UI update
        self._log_queue.put(("conn_type", conn_type))

    def _on_swap_change_cb(self, swap_state):
        # called from a background thread (e.g. controller button trigger) - queue a UI update
        self._log_queue.put(("swap_state", swap_state))

    # ---------------- logging ----------------

    def _queue_log(self, msg: str):
        self._log_queue.put(("log", msg))

    def _poll_log_queue(self):
        try:
            while True:
                kind, payload = self._log_queue.get_nowait()
                if kind == "log":
                    ts = datetime.datetime.now().strftime("%H:%M:%S")
                    self.log_text.config(state="normal")
                    self.log_text.insert("end", f"[{ts}] {payload}\n")
                    self.log_text.see("end")
                    self.log_text.config(state="disabled")
                elif kind == "status":
                    self._connected = payload
                    self._draw_status(payload)
                    self._update_status_label()
                    self._refresh_state_label()
                elif kind == "conn_type":
                    self._conn_type = payload
                    self._update_status_label()
                elif kind == "swap_state":
                    self.current_state_label.config(text=f"Current swap interval: {payload}")
                elif kind == "pre_start_usb":
                    if self.switcher is None:  # ignore stale results after Start was pressed
                        if payload:
                            self._draw_status(False)
                            self.status_label.config(text="USB detected (not started)")
                        else:
                            self._draw_status(False)
                            self.status_label.config(text="Disconnected")
        except queue.Empty:
            pass
        self.after(150, self._poll_log_queue)

    def on_close(self):
        if self.switcher:
            self.switcher.stop()
        self.destroy()


def main():
    app = HzSwitcherApp()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()


if __name__ == "__main__":
    main()
