"""Tkinter/ttk UI shell for CresnetMon.

Pure UI construction and layout - no serial I/O or protocol parsing here
beyond enumerating ports for the dropdown. Start/Stop/Clear/Arm are
pluggable callbacks (constructor params, default no-ops) so this module
has no dependency on serial_io.SerialReader/protocol.CresnetProtocol/
burst.BurstGrouper; app.py wires real behavior in without touching layout
code.

Mirrors the widget set in MainForm.Designer.cs: selComPort, txtDeviceId,
btnStart, btnClear, viewResults (ListView -> ttk.Treeview), statusText. The
Arm button, Raw log checkbox, and LabelDialog have no original-app
equivalent - new for the labeling/capture mode (STRATEGY.md).
"""

import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

from cresnetmon.serial_io import list_ports

COLUMNS = ("cycle", "time", "dev", "sent", "received")
COLUMN_HEADINGS = {
    "cycle": "ID",
    "time": "Time",
    "dev": "Dev",
    "sent": "Sent",
    "received": "Received",
}


class CresnetMonWindow:
    """Owns the main window and its widgets.

    Callers (task 5) read/write widget state (`port_var`, `device_id_var`,
    `start_button`, `results`, ...) directly rather than through a bespoke
    API - the app is small enough that a thin wrapper would just be
    indirection.
    """

    def __init__(
        self,
        root: tk.Tk,
        *,
        on_start_stop: Callable[[], None] | None = None,
        on_clear: Callable[[], None] | None = None,
        on_refresh_ports: Callable[[], None] | None = None,
        on_arm_disarm: Callable[[], None] | None = None,
        initial_port: str = "",
        initial_device_id: str = "",
    ) -> None:
        self.root = root
        self._on_start_stop = on_start_stop or (lambda: None)
        self._on_clear = on_clear or (lambda: None)
        self._on_refresh_ports = on_refresh_ports or self._default_refresh_ports
        self._on_arm_disarm = on_arm_disarm or (lambda: None)

        root.title("Cresnet Monitor")
        root.geometry("640x400")

        self.port_var = tk.StringVar(value=initial_port)
        self.device_id_var = tk.StringVar(value=initial_device_id)
        self.status_var = tk.StringVar(value="Polling count: 0")
        # Off by default (STRATEGY.md task 14) - not persisted like
        # port/device-id, since it's a deliberate one-session choice, not
        # a standing preference.
        self.raw_log_var = tk.BooleanVar(value=False)

        self._build_widgets()
        self._on_refresh_ports()

    def _build_widgets(self) -> None:
        top = ttk.Frame(self.root, padding=8)
        top.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(top, text="Port:").pack(side=tk.LEFT)
        self.port_combo = ttk.Combobox(top, textvariable=self.port_var, state="readonly", width=28)
        self.port_combo.pack(side=tk.LEFT, padx=(4, 8))

        ttk.Button(top, text="Refresh", command=self._on_refresh_ports).pack(side=tk.LEFT)

        ttk.Label(top, text="Device ID:").pack(side=tk.LEFT, padx=(12, 0))
        self.device_id_entry = ttk.Entry(top, textvariable=self.device_id_var, width=6)
        self.device_id_entry.pack(side=tk.LEFT, padx=(4, 8))

        self.start_button = ttk.Button(top, text="Start", command=self._on_start_stop)
        self.start_button.pack(side=tk.LEFT, padx=(12, 4))

        self.clear_button = ttk.Button(top, text="Clear", command=self._on_clear)
        self.clear_button.pack(side=tk.LEFT)

        self.arm_button = ttk.Button(top, text="Arm", command=self._on_arm_disarm)
        self.arm_button.configure(state=tk.DISABLED)
        self.arm_button.pack(side=tk.LEFT, padx=(12, 0))

        # Set before Start, like the port/device-id fields above - not a
        # live toggle. app.py reads it once, in _start(). Raw logging is
        # meant to start and stop with monitoring, not with Arm/Disarm
        # (STRATEGY.md task 14), so it lives here rather than next to Arm.
        self.raw_log_check = ttk.Checkbutton(top, text="Raw log", variable=self.raw_log_var)
        self.raw_log_check.pack(side=tk.LEFT, padx=(12, 0))

        self.results = ttk.Treeview(self.root, columns=COLUMNS, show="headings")
        for col in COLUMNS:
            self.results.heading(col, text=COLUMN_HEADINGS[col])
        self.results.column("cycle", width=60, anchor=tk.E)
        self.results.column("time", width=80, anchor=tk.CENTER)
        self.results.column("dev", width=50, anchor=tk.CENTER)
        self.results.column("sent", width=200, anchor=tk.W)
        self.results.column("received", width=200, anchor=tk.W)
        self.results.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=(0, 4))

        ttk.Label(self.root, textvariable=self.status_var, anchor=tk.W).pack(
            side=tk.BOTTOM, fill=tk.X, padx=8, pady=(0, 6)
        )

    def _default_refresh_ports(self) -> None:
        """Populate the port dropdown from the live port list (mirrors
        RefreshPorts, MainForm.cs:73-84). Pure enumeration, opens nothing."""
        ports = [p.device for p in list_ports()]
        current = self.port_var.get()
        self.port_combo["values"] = ports
        if current in ports:
            self.port_var.set(current)
        elif ports:
            self.port_var.set(ports[0])

    def set_running(self, *, running: bool) -> None:
        """Toggle widget state/labels for start/stop, mirroring
        btnStart_Click's UI updates (MainForm.cs:263-290). Arm is only
        ever clickable while running - labeling needs a live bus. Raw log
        is disabled while running for the opposite reason: it's read once
        at Start, like port/device-id, so changing it mid-run wouldn't do
        anything."""
        self.start_button.configure(text="Stop" if running else "Start")
        self.port_combo.configure(state=tk.DISABLED if running else "readonly")
        self.device_id_entry.configure(state=tk.DISABLED if running else tk.NORMAL)
        self.arm_button.configure(state=tk.NORMAL if running else tk.DISABLED)
        self.raw_log_check.configure(state=tk.DISABLED if running else tk.NORMAL)

    def set_armed(self, *, armed: bool) -> None:
        """Toggle the Arm/Disarm label. Separate from set_running() since
        the caller (app.py) tracks armed state independently of run state -
        e.g. it resets this to False on Stop without necessarily going
        through a user click on this button."""
        self.arm_button.configure(text="Disarm" if armed else "Arm")

    def add_row(self, cycle: int, time_str: str, dev_id_str: str, sent: str, received: str) -> None:
        """Append one row to the results view (mirrors DisplayMessage,
        MainForm.cs:118-138)."""
        self.results.insert("", tk.END, values=(cycle, time_str, dev_id_str, sent, received))

    def clear_rows(self) -> None:
        """Mirrors btnClear_Click's viewResults.Items.Clear() (MainForm.cs:294)."""
        self.results.delete(*self.results.get_children())

    def set_status(self, msg_count: int) -> None:
        """Mirrors ShowStatus (MainForm.cs:113-116)."""
        self.status_var.set(f"Polling count: {msg_count}")


class LabelDialog:
    """Modal dialog prompting for one burst's label: device, button/action
    text, and an optional note - per STRATEGY.md's "Label schema".

    Pure UI: the caller supplies device choices as opaque (value,
    display-text) pairs and gets the submitted values back through
    `on_submit`; this class has no idea what a Burst or a Cresnet device
    id is. Closing the window (the OS close button) counts as Cancel.
    """

    def __init__(
        self,
        parent: tk.Misc,
        *,
        device_options: list[tuple[str, str]],
        default_label: str,
        on_submit: Callable[[str, str, str], None],
        on_cancel: Callable[[], None],
    ) -> None:
        self._value_by_label = {label: value for value, label in device_options}
        self._on_submit = on_submit
        self._on_cancel = on_cancel

        self.top = tk.Toplevel(parent)
        self.top.title("Label this event")
        self.top.transient(parent)
        self.top.grab_set()
        self.top.protocol("WM_DELETE_WINDOW", self._cancel)

        frame = ttk.Frame(self.top, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="Device:").grid(row=0, column=0, sticky=tk.W, pady=4)
        self.device_var = tk.StringVar(value=default_label)
        self.device_combo = ttk.Combobox(
            frame,
            textvariable=self.device_var,
            values=[label for _value, label in device_options],
            state="readonly",
            width=32,
        )
        self.device_combo.grid(row=0, column=1, sticky=tk.EW, pady=4)

        ttk.Label(frame, text="Button/action:").grid(row=1, column=0, sticky=tk.W, pady=4)
        self.button_var = tk.StringVar()
        self.button_entry = ttk.Entry(frame, textvariable=self.button_var, width=32)
        self.button_entry.grid(row=1, column=1, sticky=tk.EW, pady=4)

        ttk.Label(frame, text="Note (optional):").grid(row=2, column=0, sticky=tk.W, pady=4)
        self.note_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.note_var, width=32).grid(
            row=2, column=1, sticky=tk.EW, pady=4
        )

        buttons = ttk.Frame(frame)
        buttons.grid(row=3, column=0, columnspan=2, sticky=tk.E, pady=(10, 0))
        ttk.Button(buttons, text="Cancel", command=self._cancel).pack(side=tk.LEFT, padx=(0, 6))
        self.submit_button = ttk.Button(buttons, text="Submit", command=self._submit)
        self.submit_button.pack(side=tk.LEFT)

        self.button_entry.focus_set()

    def _submit(self) -> None:
        label = self.device_var.get()
        device_value = self._value_by_label.get(label, label)
        button_text = self.button_var.get()
        note = self.note_var.get()
        self.top.destroy()
        self._on_submit(device_value, button_text, note)

    def _cancel(self) -> None:
        self.top.destroy()
        self._on_cancel()
