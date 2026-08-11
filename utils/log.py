from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def log_formatter(record: dict) -> str:
    colors = {
        "TRACE": "#cfe2f3",
        "INFO": "#9cbfdd",
        "DEBUG": "#8598ea",
        "WARNING": "#dcad5a",
        "SUCCESS": "#3dd08d",
        "ERROR": "#ae2c2c",
    }
    color = colors.get(record["level"].name, "#b3cfe7")
    return (
        f"<fg #70acde>{{time:YYYY-MM-DD HH:mm:ss}}</fg #70acde> | "
        f"<fg {color}>{{level}}</fg {color}>: "
        "<light-white>{message}</light-white>\n"
    )


logger.remove()


def _configure_process_sink() -> None:
    """Use a rotating file when a windowed executable has no stdio streams."""
    stream = sys.stdout
    if stream is not None and callable(getattr(stream, "write", None)):
        logger.add(stream, colorize=True, format=log_formatter)
        return

    configured_root = os.getenv("MPAU_AGENT_DATA_DIR", "").strip()
    if configured_root:
        data_root = Path(configured_root).expanduser()
    elif os.name == "nt" and (os.getenv("LOCALAPPDATA") or os.getenv("APPDATA")):
        data_root = Path(os.getenv("LOCALAPPDATA") or os.getenv("APPDATA") or "") / "MPAU-Agent"
    else:
        data_root = Path.home() / ".local" / "share" / "mpau-agent"
    log_directory = data_root / "logs"
    log_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    logger.add(
        log_directory / "agent.log",
        level="INFO",
        rotation="10 MB",
        retention="10 days",
        backtrace=True,
        diagnose=False,
        encoding="utf-8",
    )


_configure_process_sink()

# Uploaders bind only their business name. TaskManager contributes user_id and
# job_id through Loguru context so the same uploader code remains reusable.
tmall_logger = logger.bind(business_name="tmall")
jd_logger = logger.bind(business_name="jd")


@dataclass(slots=True)
class UserLogSinks:
    """Own per-user platform log sinks and release them on workspace shutdown."""

    sink_ids: list[int]

    def close(self) -> None:
        for sink_id in self.sink_ids:
            try:
                logger.remove(sink_id)
            except ValueError:
                pass
        self.sink_ids.clear()


def create_user_log_sinks(user_id: str, directory: Path) -> UserLogSinks:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    sink_ids: list[int] = []
    for platform in ("tmall", "jd"):
        path = directory / f"{platform}.log"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.close(descriptor)
        path.chmod(0o600)
        sink_ids.append(
            logger.add(
                path,
                level="INFO",
                rotation="10 MB",
                retention="10 days",
                backtrace=True,
                diagnose=False,
                opener=lambda target, flags: os.open(target, flags, 0o600),
                filter=lambda record, expected_platform=platform: (
                    record["extra"].get("user_id") == user_id
                    and record["extra"].get("business_name") == expected_platform
                ),
            )
        )
    return UserLogSinks(sink_ids)


def user_platform_logger(platform: str, user_id: str) -> Any:
    """Return a platform logger that retains user context outside a task."""
    if platform not in {"tmall", "jd"}:
        raise ValueError("平台不支持")
    return logger.bind(business_name=platform, user_id=user_id)
