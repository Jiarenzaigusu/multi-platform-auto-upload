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


async def dispatch_paste(frame, text: str) -> None:
    """Dispatch a page-realm paste event without activating a desktop clipboard tool."""
    await frame.evaluate(
        """(value) => {
            const candidates = [...document.querySelectorAll('.paste-search-input-content')];
            const target = candidates.find((element) => {
                const style = window.getComputedStyle(element);
                return style.display !== 'none' && style.visibility !== 'hidden';
            });
            if (!target) throw new Error('未找到可粘贴的商品链接输入区');
            const transfer = new DataTransfer();
            transfer.setData('text/plain', value);
            target.dispatchEvent(new ClipboardEvent('paste', {
                bubbles: true,
                cancelable: true,
                clipboardData: transfer,
            }));
        }""",
        text,
    )
