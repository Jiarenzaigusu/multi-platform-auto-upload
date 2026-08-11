from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import UploadFile

from utils.files import validate_media_filename


class UploadTooLargeError(ValueError):
    """Raised after an upload crosses its configured byte limit."""


class MediaQuotaExceededError(ValueError):
    """Raised before retained user media would exceed its configured quota."""


def stage_upload(upload: UploadFile, destination: Path, max_bytes: int) -> None:
    """Write an upload once with private permissions and a strict size bound."""
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    written = 0
    try:
        with os.fdopen(descriptor, "wb") as output:
            upload.file.seek(0)
            while chunk := upload.file.read(1024 * 1024):
                written += len(chunk)
                if written > max_bytes:
                    raise UploadTooLargeError
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def list_media_files(directory: Path, *, limit: int = 1000) -> list[dict]:
    """List user-owned root media files without following symbolic links."""
    files: list[dict] = []
    for path in directory.iterdir():
        if path.suffix == ".upload" or path.is_symlink() or not path.is_file():
            continue
        stat = path.stat()
        files.append(
            {
                "name": path.name,
                "size": stat.st_size,
                "modified_at": datetime.fromtimestamp(
                    stat.st_mtime, timezone.utc
                ).isoformat(),
            }
        )
    return sorted(
        files, key=lambda item: item["modified_at"], reverse=True
    )[:limit]


def directory_usage(directory: Path, *, recursive: bool = False) -> tuple[int, int]:
    """Return regular-file count and bytes without following symbolic links."""
    count = 0
    total = 0
    paths = directory.rglob("*") if recursive else directory.iterdir()
    for path in paths:
        if path.suffix == ".upload" or path.is_symlink() or not path.is_file():
            continue
        try:
            total += path.stat().st_size
            count += 1
        except FileNotFoundError:
            continue
    return count, total


def enforce_media_quota(
    directory: Path,
    *,
    incoming_files: int,
    incoming_bytes: int,
    max_files: int,
    max_bytes: int,
) -> None:
    existing_files, existing_bytes = directory_usage(directory)
    if existing_files + incoming_files > max_files:
        raise MediaQuotaExceededError(f"素材库最多保留 {max_files} 个视频")
    if existing_bytes + incoming_bytes > max_bytes:
        gib = max_bytes / (1024**3)
        raise MediaQuotaExceededError(f"素材库总容量不能超过 {gib:g} GiB")


def resolve_user_media_path(directory: Path, filename: str) -> Path:
    """Resolve a validated filename and prove it remains inside one user directory."""
    safe_name = validate_media_filename(filename)
    root = directory.resolve()
    path = (root / safe_name).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("素材文件路径超出当前用户目录") from exc
    return path
