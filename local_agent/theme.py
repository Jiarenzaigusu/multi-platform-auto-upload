"""Visual theme shared by the MPAU desktop helper dialogs.

Everything the Tkinter UI needs (palette, fonts, DPI setup and small
component builders) lives here so the dialogs in ``desktop.py`` stay
focused on behaviour.
"""

from __future__ import annotations

import ctypes
import sys

# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------
GREEN_900 = "#12362f"  # deepest green (hover on dark buttons)
GREEN_800 = "#1c4036"  # brand green (header band, primary text accents)
GREEN_700 = "#245246"
GREEN_600 = "#2f6b5b"  # input focus ring
GREEN_500 = "#3d8a75"

ORANGE_500 = "#e56d3e"  # brand accent (logo mark, primary buttons)
ORANGE_600 = "#c9522c"  # accent hover / pressed

CREAM = "#faf8f3"  # window background
CARD = "#ffffff"  # card surfaces
BORDER = "#e6e1d5"  # hairline card / input borders

TEXT_900 = "#22302c"  # primary text
TEXT_600 = "#5c6b66"  # secondary text
TEXT_400 = "#97a49f"  # muted text (footers)

STATUS_ONLINE = "#2e9e6b"  # green status dot
STATUS_OFFLINE = "#b54835"

AMBER_700 = "#a06a10"  # update-available text
AMBER_100 = "#fdf3dc"  # update-available wash
RED_600 = "#b54835"  # error text
RED_100 = "#fbeae6"

# ---------------------------------------------------------------------------
# Typography
# ---------------------------------------------------------------------------
FONT_FAMILY = "Microsoft YaHei UI"
MONO_FAMILY = "Consolas"


def font(size: int = 10, weight: str = "normal") -> tuple[str, int, str]:
    return (FONT_FAMILY, size, weight)


def mono_font(size: int = 10, weight: str = "normal") -> tuple[str, int, str]:
    return (MONO_FAMILY, size, weight)


# ---------------------------------------------------------------------------
# DPI awareness (Windows)
# ---------------------------------------------------------------------------
def enable_dpi_awareness() -> None:
    """Make Tk render crisply on high-DPI Windows displays.

    Safe to call on any platform / multiple times; failures are ignored so a
    stripped-down Windows VM can never fail to start the agent.
    """
    if sys.platform != "win32":
        return
    try:  # Per-Monitor v2 (Windows 10 1703+)
        ctypes.windll.user32.SetProcessDpiAwarenessContext(
            ctypes.c_void_p(-4)
        )
        return
    except Exception:
        pass
    try:  # System DPI aware (Windows 8.1+)
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        return
    except Exception:
        pass
    try:  # Legacy fallback
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def apply_tk_scaling(root) -> None:
    """Scale Tk point-based fonts with the real system DPI."""
    if sys.platform != "win32":
        return
    try:
        dpi = ctypes.windll.user32.GetDpiForSystem()
    except Exception:
        return
    if dpi and dpi != 96:
        try:
            root.tk.call("tk", "scaling", dpi / 72.0)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Window helpers
# ---------------------------------------------------------------------------
def center_window(root, width: int, height: int) -> None:
    root.update_idletasks()
    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    x = max(0, (screen_w - width) // 2)
    y = max(0, (screen_h - height) // 3)
    root.geometry(f"{width}x{height}+{x}+{y}")


# ---------------------------------------------------------------------------
# Component builders
# ---------------------------------------------------------------------------
def _hover(widget, normal: str, hovered: str) -> None:
    widget.bind("<Enter>", lambda _e: widget.configure(bg=hovered), add="+")
    widget.bind("<Leave>", lambda _e: widget.configure(bg=normal), add="+")


def _rounded_rect(canvas, x1, y1, x2, y2, radius, **kwargs):
    points = [
        x1 + radius, y1,
        x2 - radius, y1,
        x2, y1,
        x2, y1 + radius,
        x2, y2 - radius,
        x2, y2,
        x2 - radius, y2,
        x1 + radius, y2,
        x1, y2,
        x1, y2 - radius,
        x1, y1 + radius,
        x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


def logo_canvas(parent, size: int = 40, bg: str = GREEN_800):
    """Orange rounded-square logo mark with a white ``M``."""
    import tkinter as tk

    canvas = tk.Canvas(
        parent, width=size, height=size, bg=bg, highlightthickness=0
    )
    inset = max(2, size // 14)
    _rounded_rect(
        canvas,
        inset, inset, size - inset, size - inset,
        radius=size // 3,
        fill=ORANGE_500,
        outline="",
    )
    canvas.create_text(
        size // 2, size // 2,
        text="M",
        fill="white",
        font=(FONT_FAMILY, -int(size * 0.52), "bold"),
    )
    return canvas


def header_band(parent, title: str, subtitle: str = ""):
    """Brand header: green band with logo mark, title and orange accent."""
    import tkinter as tk

    band = tk.Frame(parent, bg=GREEN_800)
    inner = tk.Frame(band, bg=GREEN_800)
    inner.pack(fill="x", padx=28, pady=(20, 18))
    logo_canvas(inner, 40, GREEN_800).pack(side="left", padx=(0, 14))
    text_wrap = tk.Frame(inner, bg=GREEN_800)
    text_wrap.pack(side="left", fill="x", expand=True)
    tk.Label(
        text_wrap,
        text=title,
        font=font(15, "bold"),
        bg=GREEN_800,
        fg="white",
        anchor="w",
    ).pack(fill="x")
    if subtitle:
        tk.Label(
            text_wrap,
            text=subtitle,
            font=font(9),
            bg=GREEN_800,
            fg="#bcd2c9",
            anchor="w",
            justify="left",
        ).pack(fill="x", pady=(2, 0))
    tk.Frame(band, bg=ORANGE_500, height=3).pack(fill="x")
    return band


def card(parent, **pack_kwargs):
    """White surface with a hairline border."""
    import tkinter as tk

    frame = tk.Frame(
        parent,
        bg=CARD,
        highlightbackground=BORDER,
        highlightthickness=1,
    )
    if pack_kwargs:
        frame.pack(**pack_kwargs)
    return frame


def primary_button(parent, text: str, command, **pack_kwargs):
    import tkinter as tk

    button = tk.Button(
        parent,
        text=text,
        command=command,
        bg=ORANGE_500,
        fg="white",
        activebackground=ORANGE_600,
        activeforeground="white",
        relief="flat",
        bd=0,
        font=font(10, "bold"),
        cursor="hand2",
        padx=20,
        pady=6,
    )
    _hover(button, ORANGE_500, ORANGE_600)
    if pack_kwargs:
        button.pack(**pack_kwargs)
    return button


def secondary_button(parent, text: str, command, **pack_kwargs):
    import tkinter as tk

    button = tk.Button(
        parent,
        text=text,
        command=command,
        bg=CARD,
        fg=GREEN_800,
        activebackground=CREAM,
        activeforeground=GREEN_800,
        relief="flat",
        bd=0,
        font=font(10, "bold"),
        cursor="hand2",
        padx=18,
        pady=6,
        highlightbackground=BORDER,
        highlightthickness=1,
    )
    _hover(button, CARD, CREAM)
    if pack_kwargs:
        button.pack(**pack_kwargs)
    return button


def field_label(parent, text: str, **pack_kwargs):
    import tkinter as tk

    label = tk.Label(parent, text=text, bg=CARD, fg=TEXT_600, font=font(9))
    if pack_kwargs:
        label.pack(**pack_kwargs)
    return label


def styled_entry(parent, *, entry_font=None, show=None, **pack_kwargs):
    """Flat input with a hairline border that turns green on focus."""
    import tkinter as tk

    entry = tk.Entry(
        parent,
        font=entry_font or font(10),
        show=show,
        relief="flat",
        bd=0,
        bg=CARD,
        fg=TEXT_900,
        insertbackground=TEXT_900,
        highlightbackground=BORDER,
        highlightcolor=GREEN_600,
        highlightthickness=1,
    )
    entry.bind(
        "<FocusIn>", lambda _e: entry.configure(highlightbackground=GREEN_600)
    )
    entry.bind(
        "<FocusOut>", lambda _e: entry.configure(highlightbackground=BORDER)
    )
    if pack_kwargs:
        entry.pack(**pack_kwargs)
    return entry


def status_dot(parent, color: str = STATUS_ONLINE, size: int = 10):
    import tkinter as tk

    canvas = tk.Canvas(
        parent, width=size, height=size, bg=parent["bg"], highlightthickness=0
    )
    canvas.create_oval(
        1, 1, size - 1, size - 1, fill=color, outline=parent["bg"]
    )
    return canvas


def avatar_canvas(parent, name: str, size: int = 36, bg: str = CARD):
    """Round avatar with initials, used in the status window."""
    import tkinter as tk

    canvas = tk.Canvas(parent, width=size, height=size, bg=bg, highlightthickness=0)
    canvas.create_oval(0, 0, size, size, fill=GREEN_700, outline="")
    initials = "".join(part[0] for part in name.split()[:2]).upper() or "?"
    canvas.create_text(
        size // 2, size // 2,
        text=initials,
        fill="white",
        font=(FONT_FAMILY, -int(size * 0.4), "bold"),
    )
    return canvas


def update_banner(parent, textvariable, **pack_kwargs):
    """Amber 'update available' panel; empty text keeps it invisible."""
    import tkinter as tk

    panel = tk.Frame(parent, bg=AMBER_100)
    label = tk.Label(
        panel,
        textvariable=textvariable,
        bg=AMBER_100,
        fg=AMBER_700,
        font=font(9),
        wraplength=330,
        justify="left",
    )
    label.pack(anchor="w", padx=12, pady=8)
    if pack_kwargs:
        panel.pack(**pack_kwargs)
    return panel
