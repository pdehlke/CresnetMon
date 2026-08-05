"""Persists window geometry and last-used port/device-id across launches.

Replaces FormSettings.cs's Windows Forms-specific persistence (including
its Win32 GetWindowPlacement P/Invoke, FormSettings.cs:29-54) with a JSON
file at ~/Library/Application Support/CresnetMon/config.json, matching
tkinter's geometry model instead of System.Drawing's.
"""

import contextlib
import json
import tkinter as tk
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

CONFIG_DIR = Path.home() / "Library" / "Application Support" / "CresnetMon"
CONFIG_PATH = CONFIG_DIR / "config.json"


@dataclass(slots=True)
class Settings:
    """Everything persisted between launches - mirrors
    Settings.Default.MainForm/DeviceId/ComPort (MainForm.cs:239-256) plus
    FormSettings' Location/Size/IsMaximized, flattened into one JSON shape.
    """

    x: int | None = None
    y: int | None = None
    width: int | None = None
    height: int | None = None
    maximized: bool = False
    device_id: str = ""
    com_port: str = ""


def load(path: Path = CONFIG_PATH) -> Settings:
    """Read saved settings. Defaults (no geometry, blank fields) if the
    file is missing, unreadable, or has unexpected content - mirrors
    FormSettings.RestoreForm's "nothing saved yet" fallback (skip restore)
    rather than crashing on a bad/missing settings file."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):  # fmt: skip
        return Settings()
    known = {f.name for f in fields(Settings)}
    return Settings(**{k: v for k, v in raw.items() if k in known})


def save(settings: Settings, path: Path = CONFIG_PATH) -> None:
    """Write settings to disk, creating the parent directory if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(settings), indent=2))


def apply_to_window(settings: Settings, root: tk.Tk) -> None:
    """Restore window geometry. Mirrors FormSettings.RestoreForm
    (FormSettings.cs:76-86): does nothing if no geometry was ever saved."""
    if settings.width is None or settings.height is None:
        return
    if settings.x is not None and settings.y is not None:
        root.geometry(f"{settings.width}x{settings.height}+{settings.x}+{settings.y}")
    else:
        root.geometry(f"{settings.width}x{settings.height}")
    if settings.maximized:
        # Not every platform/Tk build has a zoomed state.
        with contextlib.suppress(tk.TclError):
            root.state("zoomed")


def capture_from_window(settings: Settings, root: tk.Tk) -> Settings:
    """Read current geometry off the window into a copy of `settings`.

    Mirrors FormSettings.SaveForm (FormSettings.cs:61-74): while
    maximized/zoomed, only the flag is captured - winfo_* reports the
    maximized bounds, not restorable ones, matching the original's use of
    RestoreBounds instead of Bounds when maximized.
    """
    if root.state() == "zoomed":
        return replace(settings, maximized=True)
    return replace(
        settings,
        x=root.winfo_x(),
        y=root.winfo_y(),
        width=root.winfo_width(),
        height=root.winfo_height(),
        maximized=False,
    )
