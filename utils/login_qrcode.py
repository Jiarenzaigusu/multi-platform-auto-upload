from __future__ import annotations

import base64
import re
from pathlib import Path

from utils.config import BASE_DIR


def build_login_qrcode_path(account_file: str | Path, suffix: str = "login_qrcode") -> Path:
    account_path = Path(account_file)
    directory = BASE_DIR / "runtime" / "qrcodes"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{account_path.stem}_{suffix}.png"


def save_data_url_image(data_url: str, target_path: Path) -> Path:
    header, _, payload = data_url.partition(",")
    if not header.startswith("data:image/") or not payload:
        raise ValueError("二维码数据格式无效")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(base64.b64decode(payload))
    return target_path


def decode_qrcode_from_path(path: Path) -> str:
    """Best-effort QR decode without introducing a heavy dependency."""
    del path
    return ""


def print_terminal_qrcode(content: str, path: Path, app_name: str) -> None:
    del content, path, app_name


def remove_qrcode_file(path: Path | None) -> bool:
    if not path:
        return False
    try:
        Path(path).unlink()
        return True
    except FileNotFoundError:
        return False

