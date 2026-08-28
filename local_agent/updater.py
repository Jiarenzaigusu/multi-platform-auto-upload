"""Self-update support for the frozen Windows desktop agent.

The agent checks the control plane for a newer installer, downloads it with
SHA-256 verification, and opens the normal Inno Setup wizard.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from local_agent.client import AgentApiClient, AgentApiError

UPDATE_DIRECTORY_NAME = "update"
INSTALLER_LOG_NAME = "installer.log"
_VERSION_PATTERN = re.compile(r"^\d+(\.\d+)*$")
INSTALLER_STARTUP_CHECK_SECONDS = 0.8


def parse_version(value: str) -> tuple[int, ...] | None:
    """Parse a dotted numeric version string such as "0.3.1"."""
    text = str(value or "").strip().lstrip("vV")
    if not _VERSION_PATTERN.fullmatch(text):
        return None
    return tuple(int(part) for part in text.split("."))


def is_newer(latest: str, current: str) -> bool:
    """Return True when ``latest`` is strictly newer than ``current``."""
    latest_parts = parse_version(latest)
    current_parts = parse_version(current)
    if latest_parts is None or current_parts is None:
        return str(latest).strip() != str(current).strip() and bool(latest)
    # Pad with zeros so 0.3 > 0.2.1 and 0.3 == 0.3.0 compare cleanly.
    width = max(len(latest_parts), len(current_parts))
    latest_padded = latest_parts + (0,) * (width - len(latest_parts))
    current_padded = current_parts + (0,) * (width - len(current_parts))
    return latest_padded > current_padded


def normalize_release(raw: Any) -> dict[str, Any] | None:
    """Validate the server release manifest and return it, or None."""
    if not isinstance(raw, dict):
        return None
    version = raw.get("version")
    if not isinstance(version, str) or parse_version(version) is None:
        return None
    sha256 = raw.get("sha256")
    if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
        return None
    return {
        "version": version,
        "sha256": sha256.lower(),
        "size": raw.get("size") if isinstance(raw.get("size"), int) else 0,
        "notes": raw.get("notes") if isinstance(raw.get("notes"), str) else "",
        "released_at": (
            raw.get("released_at") if isinstance(raw.get("released_at"), str) else ""
        ),
    }


def fetch_latest_release(
    client: AgentApiClient, current_version: str
) -> dict[str, Any] | None:
    """Ask the server for the latest installer; return it when it is newer."""
    try:
        response = client.latest_release()
    except (AgentApiError, OSError):
        return None
    release = normalize_release(response.get("release"))
    if release is None:
        return None
    if not is_newer(release["version"], current_version):
        return None
    return release


def download_release(
    client: AgentApiClient,
    release: dict[str, Any],
    data_root: Path,
    *,
    progress: Callable[[int, int | None], None] | None = None,
) -> Path:
    """Download the installer for ``release`` and verify its integrity."""
    directory = data_root / UPDATE_DIRECTORY_NAME
    destination = directory / f"MPAU-Agent-Setup-{release['version']}.exe"
    client.download_installer(
        destination,
        expected_size=release.get("size") or None,
        expected_sha256=release["sha256"],
        progress=progress,
    )
    if not destination.is_file() or destination.stat().st_size == 0:
        raise RuntimeError("更新安装包下载为空")
    return destination


def cleanup_stale_installers(data_root: Path, keep: Path | None = None) -> None:
    """Remove leftover update installers except the one being kept."""
    directory = data_root / UPDATE_DIRECTORY_NAME
    if not directory.is_dir():
        return
    for path in directory.glob("MPAU-Agent-Setup*.exe"):
        if keep is not None and path == keep:
            continue
        try:
            path.unlink()
        except OSError:
            pass


def launch_update(data_root: Path, installer_path: Path) -> subprocess.Popen:
    """Open the visible installer and verify that its process stays running."""
    if not getattr(sys, "frozen", False):
        raise RuntimeError("自动更新仅支持已安装的 Windows 助手")
    installer = installer_path.resolve()
    update_directory = (data_root / UPDATE_DIRECTORY_NAME).resolve()
    if installer.parent != update_directory or not installer.is_file():
        raise RuntimeError("更新安装包不存在或位置无效，请重新下载")

    try:
        process = subprocess.Popen(
            [
                str(installer),
                "/NORESTART",
                "/CLOSEAPPLICATIONS",
                f"/LOG={update_directory / INSTALLER_LOG_NAME}",
            ],
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(update_directory),
        )
    except OSError as exc:
        raise RuntimeError(f"无法打开更新安装程序：{exc}") from exc

    # Do not terminate the helper until Windows has accepted and kept the
    # visible installer process alive. Immediate startup failures stay visible
    # in the helper window instead of leaving the user with no application.
    time.sleep(INSTALLER_STARTUP_CHECK_SECONDS)
    exit_code = process.poll()
    if exit_code is not None:
        raise RuntimeError(f"更新安装程序启动后立即退出（错误代码 {exit_code}）")
    return process
