from __future__ import annotations

from dataclasses import dataclass
import re
from pathlib import Path


_USER_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def _secure_directory(path: Path) -> Path:
    """Create a private directory and return its resolved path."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path.resolve()


@dataclass(frozen=True, slots=True)
class UserDataPaths:
    """All mutable files owned by one authenticated application user."""

    root: Path
    runtime: Path
    cookies: Path
    uploads: Path
    media: Path
    job_logs: Path
    platform_logs: Path
    screenshots: Path
    secrets: Path

    @classmethod
    def create(cls, users_root: Path, user_id: str) -> "UserDataPaths":
        if not _USER_ID_PATTERN.fullmatch(user_id):
            raise ValueError("用户 ID 格式无效")
        root = _secure_directory(users_root / user_id)
        paths = cls(
            root=root,
            runtime=_secure_directory(root / "runtime"),
            cookies=_secure_directory(root / "cookies"),
            uploads=_secure_directory(root / "uploads"),
            media=_secure_directory(root / "media"),
            job_logs=_secure_directory(root / "job-logs"),
            platform_logs=_secure_directory(root / "platform-logs"),
            screenshots=_secure_directory(root / "screenshots"),
            secrets=_secure_directory(root / "secrets"),
        )
        _secure_directory(paths.cookies / "tmall")
        _secure_directory(paths.cookies / "jd")
        _secure_directory(paths.screenshots / "tmall")
        _secure_directory(paths.screenshots / "jd")
        return paths

    def cookie_file(self, platform: str, account: str) -> Path:
        """Return one platform account state file inside this user workspace."""
        if platform not in {"tmall", "jd"}:
            raise ValueError("平台不支持")
        return self.cookies / platform / f"{account}.json"


@dataclass(frozen=True, slots=True)
class AppDataPaths:
    """System-level and user-level persistent storage roots."""

    root: Path
    system: Path
    users: Path
    auth_database: Path

    @classmethod
    def create(cls, root: Path) -> "AppDataPaths":
        resolved_root = _secure_directory(root.expanduser())
        system = _secure_directory(resolved_root / "system")
        users = _secure_directory(resolved_root / "users")
        return cls(
            root=resolved_root,
            system=system,
            users=users,
            auth_database=system / "auth.db",
        )

    def for_user(self, user_id: str) -> UserDataPaths:
        """Create or reopen the private storage tree for one immutable user ID."""
        return UserDataPaths.create(self.users, user_id)
