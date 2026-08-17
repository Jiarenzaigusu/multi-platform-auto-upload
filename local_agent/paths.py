from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import uuid

from webapp.workspaces.paths import UserDataPaths


def default_data_root() -> Path:
    configured = os.getenv("MPAU_AGENT_DATA_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    base = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA")
    if base:
        return (Path(base) / "MPAU-Agent").resolve()
    return (Path.home() / ".mpau-agent").resolve()


def secure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path.resolve()


def load_or_create_agent_id(root: Path) -> str:
    secure_directory(root)
    path = root / "device.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        agent_id = document.get("agent_id")
        if isinstance(agent_id, str) and len(agent_id) == 32:
            int(agent_id, 16)
            return agent_id
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        pass

    agent_id = uuid.uuid4().hex
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".device.", suffix=".tmp", dir=root
    )
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump({"agent_id": agent_id}, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        temporary.unlink(missing_ok=True)
    return agent_id


def user_paths(root: Path, user_id: str) -> UserDataPaths:
    return UserDataPaths.create(secure_directory(root / "users"), user_id)
