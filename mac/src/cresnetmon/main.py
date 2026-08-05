"""Entry point for the CresnetMon macOS app."""

import tkinter as tk

from cresnetmon.app import CresnetMonApp


def main() -> None:
    root = tk.Tk()
    CresnetMonApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
