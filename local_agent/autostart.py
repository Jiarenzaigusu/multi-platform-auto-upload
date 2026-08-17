from __future__ import annotations

import os
from pathlib import Path
import sys


_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "MPAU Agent"


def autostart_arguments() -> list[str]:
    executable = Path(sys.executable).resolve()
    if getattr(sys, "frozen", False):
        return [str(executable), "--background"]
    return [str(executable), "-m", "local_agent.desktop", "--background"]


def autostart_command() -> str:
    return " ".join(f'"{argument}"' for argument in autostart_arguments())


def set_windows_autostart(enabled: bool) -> bool:
    """Register the interactive helper for the current Windows user only."""
    if os.name != "nt":
        return False
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(
                key, _VALUE_NAME, 0, winreg.REG_SZ, autostart_command()
            )
        else:
            try:
                winreg.DeleteValue(key, _VALUE_NAME)
            except FileNotFoundError:
                pass
    return True


def set_autostart(enabled: bool) -> bool:
    """Enable or disable autostart for the current Windows user."""
    return set_windows_autostart(enabled)


def open_url(url: str) -> None:
    if os.name != "nt":
        raise RuntimeError("MPAU 本地执行助手仅支持 Windows")
    os.startfile(url)  # type: ignore[attr-defined]
