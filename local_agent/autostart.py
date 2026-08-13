from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from xml.sax.saxutils import escape


_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "MPAU Agent"
_MACOS_LABEL = "com.mpau.agent"


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


def _macos_launch_agent_path() -> Path:
    return Path.home() / "Library/LaunchAgents" / f"{_MACOS_LABEL}.plist"


def set_macos_autostart(enabled: bool) -> bool:
    """Register the packaged helper for the current macOS login session."""
    if sys.platform != "darwin":
        return False

    plist_path = _macos_launch_agent_path()
    uid = os.getuid()
    domain_target = f"gui/{uid}/{_MACOS_LABEL}"
    if not enabled:
        subprocess.run(
            ["launchctl", "bootout", domain_target],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        plist_path.unlink(missing_ok=True)
        return True

    plist_path.parent.mkdir(parents=True, exist_ok=True)
    arguments = "".join(
        f"<string>{escape(argument)}</string>" for argument in autostart_arguments()
    )
    plist_path.write_text(
        f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{_MACOS_LABEL}</string>
  <key>ProgramArguments</key><array>{arguments}</array>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Interactive</string>
</dict></plist>
''',
        encoding="utf-8",
    )
    subprocess.run(
        ["launchctl", "bootout", domain_target],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # The foreground app is already connected after a successful pairing.
    # Loading the LaunchAgent here would create a duplicate helper; RunAtLoad
    # starts it during the next macOS login instead.
    return True


def set_autostart(enabled: bool) -> bool:
    """Enable or disable autostart using the current platform's user mechanism."""
    if os.name == "nt":
        return set_windows_autostart(enabled)
    if sys.platform == "darwin":
        return set_macos_autostart(enabled)
    return False


def open_url(url: str) -> None:
    if os.name == "nt":
        os.startfile(url)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", url])
    else:
        subprocess.Popen(["xdg-open", url])
