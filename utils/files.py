from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from webapp.api.models import SUPPORTED_VIDEO_EXTENSIONS

_WINDOWS_RESERVED_NAMES = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
_WINDOWS_INVALID_CHARACTERS = set('<>:"/\\|?*')


def validate_media_filename(filename: str) -> str:
    """Validate one portable root-level video name for cloud-to-Windows use."""
    value = filename.strip()
    if (
        not value
        or value in {".", ".."}
        or len(value) > 200
        or value.endswith((".", " "))
    ):
        raise ValueError("视频文件名为空或超过 200 个字符")
    if Path(value).name != value or any(
        character in _WINDOWS_INVALID_CHARACTERS or ord(character) < 32
        for character in value
    ):
        raise ValueError(f"视频文件名包含不允许的字符：{value}")
    if value.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_NAMES:
        raise ValueError(f"视频文件名是 Windows 保留名称：{value}")
    if Path(value).suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS:
        raise ValueError(f"不支持的视频格式：{value}")
    return value


def cleanup_old_files(
    directory: Path, *, older_than_days: int, suffixes: set[str]
) -> list[Path]:
    """Remove old regular artifacts without following links outside the workspace."""
    cutoff = datetime.now(timezone.utc).timestamp() - max(1, older_than_days) * 86400
    removed: list[Path] = []
    if not directory.exists():
        return removed
    for path in directory.rglob("*"):
        if path.is_symlink() or not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed.append(path)
        except FileNotFoundError:
            continue
    return removed


def cleanup_old_directories(directory: Path, *, older_than_days: int) -> list[Path]:
    """Best-effort cleanup for abandoned per-job download directories."""
    cutoff = datetime.now(timezone.utc).timestamp() - max(1, older_than_days) * 86400
    removed: list[Path] = []
    if not directory.exists():
        return removed
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_dir():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                shutil.rmtree(path)
                removed.append(path)
        except (FileNotFoundError, OSError):
            continue
    return removed
