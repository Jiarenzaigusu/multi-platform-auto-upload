from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time

from webapp.auth.models import AgentDevice, AuthenticatedAgent, AuthenticatedSession, User
from webapp.auth.passwords import PasswordService
from webapp.auth.store import AuthStore


_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{3,64}$")
_PAIRING_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


class AuthenticationError(ValueError):
    """Raised when credentials or session security checks fail."""


class UserNotFoundError(AuthenticationError):
    """Raised when an administrator targets an unknown immutable user ID."""


class LoginThrottle:
    """Bound repeated login attempts without persisting raw credentials."""

    def __init__(self, *, attempts: int = 5, window_seconds: int = 300) -> None:
        self._attempts = attempts
        self._window_seconds = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _key(self, username: str, ip_address: str) -> str:
        return f"{username.casefold()}:{ip_address}"

    def check(self, username: str, ip_address: str) -> None:
        key = self._key(username, ip_address)
        now = time.monotonic()
        with self._lock:
            events = self._events[key]
            while events and events[0] <= now - self._window_seconds:
                events.popleft()
            if len(events) >= self._attempts:
                raise AuthenticationError("登录失败次数过多，请稍后再试")

    def failure(self, username: str, ip_address: str) -> None:
        with self._lock:
            self._events[self._key(username, ip_address)].append(time.monotonic())

    def success(self, username: str, ip_address: str) -> None:
        with self._lock:
            self._events.pop(self._key(username, ip_address), None)


class AuthService:
    """Application service for user provisioning and opaque sessions."""

    def __init__(
        self,
        store: AuthStore,
        *,
        session_seconds: int = 12 * 60 * 60,
        agent_token_seconds: int = 180 * 24 * 60 * 60,
        passwords: PasswordService | None = None,
    ) -> None:
        self.store = store
        self.session_seconds = max(15 * 60, session_seconds)
        self.agent_token_seconds = max(24 * 60 * 60, agent_token_seconds)
        self.passwords = passwords or PasswordService()
        self.throttle = LoginThrottle()

    @staticmethod
    def _hash_token(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_username(username: str) -> str:
        value = username.strip()
        if not _USERNAME_PATTERN.fullmatch(value):
            raise AuthenticationError("用户名须为 3-64 位字母、数字、点、下划线或连字符")
        return value

    @staticmethod
    def _validate_password(password: str) -> None:
        if len(password) < 10 or len(password) > 256:
            raise AuthenticationError("密码长度必须为 10-256 个字符")
        if password.isspace():
            raise AuthenticationError("密码不能仅包含空白字符")

    def setup_required(self) -> bool:
        """Return whether the one-time local administrator setup is available."""
        return self.store.user_count() == 0

    @staticmethod
    def _normalize_display_name(display_name: str, fallback: str) -> str:
        name = display_name.strip() or fallback
        if len(name) > 80:
            raise AuthenticationError("显示名称不能超过 80 个字符")
        return name

    def bootstrap_admin(
        self,
        *,
        username: str,
        display_name: str,
        password: str,
    ) -> User:
        if not self.setup_required():
            raise AuthenticationError("系统已经完成初始化")
        normalized = self._normalize_username(username)
        self._validate_password(password)
        name = self._normalize_display_name(display_name, normalized)
        try:
            user = self.store.create_initial_user(
                username=normalized,
                display_name=name,
                password_hash=self.passwords.hash(password),
            )
        except ValueError as exc:
            raise AuthenticationError("系统已经完成初始化") from exc
        self.store.record_audit(user_id=user.id, action="bootstrap_admin")
        return user

    def register_operator(
        self,
        *,
        username: str,
        display_name: str,
        password: str,
        ip_address: str,
    ) -> User:
        """Self-register an active operator after the administrator initializes the app."""
        normalized = self._normalize_username(username)
        self._validate_password(password)
        try:
            user = self.store.register_operator(
                username=normalized,
                display_name=self._normalize_display_name(display_name, normalized),
                password_hash=self.passwords.hash(password),
            )
        except sqlite3.IntegrityError as exc:
            raise AuthenticationError("用户名已经存在") from exc
        except ValueError as exc:
            raise AuthenticationError(str(exc)) from exc
        self.store.record_audit(
            user_id=user.id,
            action="self_register",
            resource_type="user",
            resource_id=user.id,
            ip_address=ip_address,
            detail="role=operator",
        )
        return user

    def list_users(self) -> list[User]:
        """List provisioned users for the administration console."""
        return self.store.list_users()

    def create_user(
        self,
        *,
        actor_user_id: str,
        username: str,
        display_name: str,
        password: str,
        role: str,
        ip_address: str,
    ) -> User:
        """Provision a user and record who performed the action."""
        normalized = self._normalize_username(username)
        self._validate_password(password)
        if role not in {"admin", "operator"}:
            raise AuthenticationError("用户角色无效")
        try:
            user = self.store.create_user(
                username=normalized,
                display_name=self._normalize_display_name(display_name, normalized),
                password_hash=self.passwords.hash(password),
                role=role,
            )
        except sqlite3.IntegrityError as exc:
            raise AuthenticationError("用户名已经存在") from exc
        self.store.record_audit(
            user_id=actor_user_id,
            action="create_user",
            resource_type="user",
            resource_id=user.id,
            ip_address=ip_address,
            detail=f"role={role}",
        )
        return user

    def update_user(
        self,
        *,
        actor_user_id: str,
        user_id: str,
        display_name: str | None,
        role: str | None,
        status: str | None,
        ip_address: str,
    ) -> User:
        """Change profile or access state without changing the workspace ID."""
        if display_name is None and role is None and status is None:
            raise AuthenticationError("至少需要修改一个用户字段")
        if display_name is not None and not display_name.strip():
            raise AuthenticationError("显示名称不能为空")
        if display_name is not None and len(display_name.strip()) > 80:
            raise AuthenticationError("显示名称不能超过 80 个字符")
        if role is not None and role not in {"admin", "operator"}:
            raise AuthenticationError("用户角色无效")
        if status is not None and status not in {"active", "disabled"}:
            raise AuthenticationError("用户状态无效")
        normalized_name = display_name.strip() if display_name is not None else None
        try:
            user = self.store.update_user(
                user_id,
                display_name=normalized_name,
                role=role,
                status=status,
            )
        except KeyError as exc:
            raise UserNotFoundError("用户不存在") from exc
        except ValueError as exc:
            raise AuthenticationError(str(exc)) from exc
        self.store.record_audit(
            user_id=actor_user_id,
            action="update_user",
            resource_type="user",
            resource_id=user.id,
            ip_address=ip_address,
            detail=f"role={role or '-'},status={status or '-'}",
        )
        return user

    def reset_password(
        self,
        *,
        actor_user_id: str,
        user_id: str,
        password: str,
        ip_address: str,
    ) -> User:
        """Set a replacement password and revoke all sessions for that user."""
        self._validate_password(password)
        try:
            user = self.store.set_password_and_revoke_sessions(
                user_id, self.passwords.hash(password)
            )
        except KeyError as exc:
            raise UserNotFoundError("用户不存在") from exc
        self.store.record_audit(
            user_id=actor_user_id,
            action="reset_password",
            resource_type="user",
            resource_id=user.id,
            ip_address=ip_address,
        )
        return user

    def revoke_user_sessions(
        self, *, actor_user_id: str, user_id: str, ip_address: str
    ) -> User:
        """Force a user to sign in again on every browser."""
        try:
            user = self.store.revoke_user_sessions(user_id)
        except KeyError as exc:
            raise UserNotFoundError("用户不存在") from exc
        self.store.record_audit(
            user_id=actor_user_id,
            action="revoke_user_sessions",
            resource_type="user",
            resource_id=user.id,
            ip_address=ip_address,
        )
        return user

    @staticmethod
    def _normalize_pairing_code(code: str) -> str:
        return "".join(character for character in code.upper() if character.isalnum())

    def issue_agent_pairing_code(
        self, *, user_id: str, ip_address: str
    ) -> tuple[str, str]:
        """Create a high-entropy, single-use code valid for five minutes."""
        raw = "".join(secrets.choice(_PAIRING_ALPHABET) for _ in range(10))
        display_code = f"{raw[:5]}-{raw[5:]}"
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        try:
            pairing_id = self.store.create_agent_pairing_code(
                user_id=user_id,
                code_hash=self._hash_token(raw),
                expires_at=expires_at.isoformat(),
            )
        except KeyError as exc:
            raise AuthenticationError("当前账号不可用于设备配对") from exc
        self.store.record_audit(
            user_id=user_id,
            action="create_agent_pairing_code",
            resource_type="agent_pairing",
            resource_id=pairing_id,
            ip_address=ip_address,
        )
        return display_code, expires_at.isoformat()

    def pair_agent(
        self,
        *,
        pairing_code: str,
        agent_id: str,
        device_name: str,
        system: str,
        version: str,
        ip_address: str,
    ) -> tuple[AuthenticatedAgent, str]:
        """Exchange one pairing code for a long-lived, revocable device token."""
        normalized_code = self._normalize_pairing_code(pairing_code)
        if len(normalized_code) != 10:
            raise AuthenticationError("配对码无效或已经过期")
        token = secrets.token_urlsafe(48)
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=self.agent_token_seconds
        )
        paired = self.store.consume_agent_pairing_code(
            code_hash=self._hash_token(normalized_code),
            agent_id=agent_id,
            token_hash=self._hash_token(token),
            device_name=device_name,
            system=system,
            version=version,
            expires_at=expires_at.isoformat(),
        )
        if paired is None:
            raise AuthenticationError("配对码无效或已经过期")
        self.store.record_audit(
            user_id=paired.user.id,
            action="pair_agent_device",
            resource_type="agent_device",
            resource_id=agent_id,
            ip_address=ip_address,
            detail=f"device={device_name[:120]}",
        )
        return paired, token

    def resolve_agent(self, token: str | None) -> AuthenticatedAgent | None:
        if not token:
            return None
        return self.store.resolve_agent_token(
            self._hash_token(token), datetime.now(timezone.utc).isoformat()
        )

    def list_agent_devices(self, user_id: str) -> list[AgentDevice]:
        return self.store.list_agent_devices(user_id)

    def revoke_agent_device(
        self, *, user_id: str, agent_id: str, ip_address: str
    ) -> None:
        if not self.store.revoke_agent_device(user_id, agent_id):
            raise AuthenticationError("本地执行助手不存在或已经解除配对")
        self.store.record_audit(
            user_id=user_id,
            action="revoke_agent_device",
            resource_type="agent_device",
            resource_id=agent_id,
            ip_address=ip_address,
        )

    def authenticate(
        self,
        *,
        username: str,
        password: str,
        ip_address: str,
        user_agent: str,
    ) -> tuple[AuthenticatedSession, str, str]:
        """Verify credentials and issue opaque session plus CSRF tokens."""
        normalized = self._normalize_username(username)
        self.throttle.check(normalized, ip_address)
        credentials = self.store.credentials_for_username(normalized)
        if credentials is None:
            self.throttle.failure(normalized, ip_address)
            raise AuthenticationError("用户名或密码错误")
        user, password_hash = credentials
        if user.status != "active" or not self.passwords.verify(password_hash, password):
            self.throttle.failure(normalized, ip_address)
            raise AuthenticationError("用户名或密码错误")
        if self.passwords.needs_rehash(password_hash):
            self.store.update_password_hash(user.id, self.passwords.hash(password))

        now = datetime.now(timezone.utc)
        self.store.prune_sessions(now.isoformat())
        token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        expires_at = now + timedelta(seconds=self.session_seconds)
        session_id = self.store.create_session(
            user_id=user.id,
            token_hash=self._hash_token(token),
            csrf_hash=self._hash_token(csrf_token),
            expires_at=expires_at.isoformat(),
            ip_address=ip_address,
            user_agent=user_agent[:500],
        )
        self.store.mark_login(user.id)
        self.store.record_audit(
            user_id=user.id,
            action="login",
            resource_type="session",
            resource_id=session_id,
            ip_address=ip_address,
        )
        self.throttle.success(normalized, ip_address)
        return (
            AuthenticatedSession(user, session_id, expires_at.isoformat()),
            token,
            csrf_token,
        )

    def resolve(self, token: str | None) -> AuthenticatedSession | None:
        """Resolve a browser Cookie token without persisting or logging it."""
        if not token:
            return None
        return self.store.resolve_session(
            self._hash_token(token), datetime.now(timezone.utc).isoformat()
        )

    def verify_csrf(self, session_id: str, csrf_token: str | None) -> bool:
        """Compare the request CSRF token to the server-side session hash."""
        if not csrf_token:
            return False
        expected = self.store.csrf_hash_for_session(session_id)
        return bool(expected and hmac.compare_digest(expected, self._hash_token(csrf_token)))

    def logout(self, session: AuthenticatedSession, *, ip_address: str) -> None:
        """Revoke the current login while leaving background work untouched."""
        self.store.revoke_session(session.session_id)
        self.store.record_audit(
            user_id=session.user.id,
            action="logout",
            resource_type="session",
            resource_id=session.session_id,
            ip_address=ip_address,
        )
