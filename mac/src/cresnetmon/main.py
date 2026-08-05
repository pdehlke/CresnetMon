"""Entry point for the CresnetMon macOS app.

Currently a stub: opens an empty window. UI, serial I/O, and protocol
parsing are wired in by later tasks (see ../STRATEGY.md).
"""

import tkinter as tk


def main() -> None:
    root = tk.Tk()
    root.title("Cresnet Monitor")
    root.geometry("640x400")
    root.mainloop()


if __name__ == "__main__":
    main()
