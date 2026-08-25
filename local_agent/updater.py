"""Self-update support for the frozen Windows desktop agent.

The agent checks the control plane for a newer installer, downloads it with
SHA-256 verification, and then starts a detached PowerShell updater. The
external script waits for the old agent process to exit, runs the Inno Setup
installer silently into the existing directory, and relaunches the agent.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from local_agent.client import AgentApiClient, AgentApiError

UPDATE_DIRECTORY_NAME = "update"
UPDATE_FAILURE_MARKER = "update.failed.txt"
PROCESS_NAME = "MPAU-Agent.exe"
_VERSION_PATTERN = re.compile(r"^\d+(\.\d+)*$")
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
_DETACHED_FLAGS = (
    (
        getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | _CREATE_NO_WINDOW
    )
    if os.name == "nt"
    else 0
)


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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


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


def consume_update_failure(data_root: Path) -> str | None:
    """Return and remove the most recent failed-update marker, if any."""
    marker = data_root / UPDATE_DIRECTORY_NAME / UPDATE_FAILURE_MARKER
    try:
        message = marker.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    try:
        marker.unlink()
    except OSError:
        pass
    return message or None


def _update_script() -> str:
    """Return the external updater script used after the agent exits.

    The script intentionally runs under PowerShell rather than under
    ``MPAU-Agent.exe``. Windows keeps an executing EXE locked, so a helper
    launched from that EXE cannot reliably replace the installed program.
    """
    return r'''param(
    [Parameter(Mandatory = $true)][string]$InstallerPath,
    [Parameter(Mandatory = $true)][string]$InstallDir,
    [Parameter(Mandatory = $true)][string]$AgentExe,
    [Parameter(Mandatory = $true)][int]$ParentPid
)

$ErrorActionPreference = "Stop"
$LogPath = Join-Path $PSScriptRoot "update.log"
$FailurePath = Join-Path $PSScriptRoot "update.failed.txt"

function Write-UpdateLog([string]$Message) {
    Add-Content -LiteralPath $LogPath -Value ("{0:u} {1}" -f (Get-Date), $Message)
}

function Write-UpdateFailure([string]$Message) {
    try {
        Set-Content -LiteralPath $FailurePath -Value $Message -Encoding UTF8
    }
    catch {
        # Best-effort only; the log still records the failure details.
    }
}

try {
    Write-UpdateLog "Waiting for agent process $ParentPid to exit."
    $deadline = (Get-Date).AddMinutes(3)
    while ((Get-Date) -lt $deadline -and (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue)) {
        Start-Sleep -Seconds 1
    }
    if (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue) {
        throw "The previous agent process did not exit in time."
    }

    if (-not (Test-Path -LiteralPath $InstallerPath -PathType Leaf)) {
        throw "The downloaded installer is missing."
    }
    Write-UpdateLog "Installing update."
    $arguments = @(
        "/VERYSILENT"
        "/NORESTART"
        "/SUPPRESSMSGBOXES"
        "/SP-"
        ('/LOG="{0}"' -f (Join-Path $PSScriptRoot "installer.log"))
        ('/DIR="{0}"' -f $InstallDir)
    )
    $installer = Start-Process -FilePath $InstallerPath -ArgumentList $arguments -Wait -PassThru
    if ($installer.ExitCode -ne 0) {
        throw "Installer exited with code $($installer.ExitCode)."
    }
    if (-not (Test-Path -LiteralPath $AgentExe -PathType Leaf)) {
        throw "Updated agent executable was not found."
    }
    Write-UpdateLog "Starting updated agent."
    Start-Process -FilePath $AgentExe -ArgumentList "--background" -WorkingDirectory $InstallDir
    Write-UpdateLog "Update completed."
}
catch {
    $failure = "更新失败：$($_.Exception.Message)"
    Write-UpdateLog ("Update failed: " + $_.Exception.Message)
    Write-UpdateFailure $failure
    exit 1
}
'''


def _write_update_script(data_root: Path) -> Path:
    """Create the external updater script in the private agent data directory."""
    directory = data_root / UPDATE_DIRECTORY_NAME
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    script_path = directory / "apply-update.ps1"
    temporary_path = script_path.with_suffix(".tmp")
    temporary_path.write_text(_update_script(), encoding="utf-8", newline="\r\n")
    temporary_path.replace(script_path)
    try:
        script_path.chmod(0o600)
    except OSError:
        pass
    return script_path


def launch_update(data_root: Path, installer_path: Path) -> None:
    """Start an external updater, then let the installed agent exit.

    PowerShell waits for this process by PID before invoking Inno Setup. It is
    not loaded from the install directory and therefore does not lock the EXE
    being replaced.
    """
    if not getattr(sys, "frozen", False):
        raise RuntimeError("自动更新仅支持已安装的 Windows 助手")
    install_dir = Path(sys.executable).resolve().parent
    script_path = _write_update_script(data_root)
    subprocess.Popen(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
            "-InstallerPath",
            str(installer_path),
            "-InstallDir",
            str(install_dir),
            "-AgentExe",
            str(install_dir / PROCESS_NAME),
            "-ParentPid",
            str(os.getpid()),
        ],
        creationflags=_DETACHED_FLAGS,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(data_root),
    )
