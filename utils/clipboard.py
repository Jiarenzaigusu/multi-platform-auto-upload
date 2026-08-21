from __future__ import annotations

import os
import subprocess
import sys


def write_clipboard(text: str) -> str:
    """Write text to the desktop clipboard and return the native paste shortcut."""
    if sys.platform == "darwin":
        command = ["pbcopy"]
        shortcut = "Meta+V"
    elif os.name == "nt":
        command = ["clip"]
        shortcut = "Control+V"
    else:
        command = ["xclip", "-selection", "clipboard"]
        shortcut = "Control+V"
    try:
        subprocess.run(
            command,
            input=text,
            text=True,
            check=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        raise RuntimeError("无法写入系统剪贴板，京东链接导入需要系统粘贴能力") from exc
    return shortcut
