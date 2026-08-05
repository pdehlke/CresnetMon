"""Entry point for the CresnetMon macOS app.

Builds the UI shell (cresnetmon.ui.CresnetMonWindow) with default no-op
callbacks. Wiring to serial_io/protocol happens in task 5; see STRATEGY.md.
"""

import tkinter as tk

from cresnetmon.ui import CresnetMonWindow


def main() -> None:
    root = tk.Tk()
    CresnetMonWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()
