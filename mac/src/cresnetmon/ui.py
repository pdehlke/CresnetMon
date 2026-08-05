"""Tkinter/ttk UI shell for CresnetMon.

Pure UI construction and layout - no serial I/O or protocol parsing here
beyond enumerating ports for the dropdown. Start/Stop/Clear are pluggable
callbacks (constructor params, default no-ops) so this module has no
dependency on serial_io.SerialReader/protocol.CresnetProtocol; task 5 wires
real behavior in without touching layout code.

Mirrors the widget set in MainForm.Designer.cs: selComPort, txtDeviceId,
btnStart, btnClear, viewResults (ListView -> ttk.Treeview), statusText.
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
    ) -> None:
        self.root = root
        self._on_start_stop = on_start_stop or (lambda: None)
        self._on_clear = on_clear or (lambda: None)
        self._on_refresh_ports = on_refresh_ports or self._default_refresh_ports

        root.title("Cresnet Monitor")
        root.geometry("640x400")

        self.port_var = tk.StringVar()
        self.device_id_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Polling count: 0")

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
        btnStart_Click's UI updates (MainForm.cs:263-290)."""
        self.start_button.configure(text="Stop" if running else "Start")
        self.port_combo.configure(state=tk.DISABLED if running else "readonly")
        self.device_id_entry.configure(state=tk.DISABLED if running else tk.NORMAL)

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
