"""Native folder selection dialog for local UI (Tkinter)."""

from __future__ import annotations

from pathlib import Path


def pick_folder(initial_dir: str | None = None, title: str | None = None) -> str | None:
    """
    Open OS folder picker. Returns absolute path or None if cancelled.
    Blocks until the dialog closes — call from a worker thread.
    """
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        kwargs: dict = {"title": title or "Select folder"}
        if initial_dir:
            p = Path(initial_dir.strip())
            if p.is_dir():
                kwargs["initialdir"] = str(p.resolve())
        path = filedialog.askdirectory(**kwargs)
    finally:
        root.destroy()

    if not path:
        return None
    return str(Path(path).resolve())
