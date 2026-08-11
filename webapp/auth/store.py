from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Iterator

from webapp.auth.models import AgentDevice, AuthenticatedAgent, AuthenticatedSession, User


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AuthStore:
    """SQLite repository for users, sessions, and security audit events."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        self._write_lock = threading.RLock()
        self._initialize()

    def _secure_database_files(self) -> None:
        """Restrict the database and transient WAL files to the service account."""
        for candidate in (
            self.path,
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
        ):
            try:
                candidate.chmod(0o600)
            except FileNotFoundError:
                continue

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            yield connection
        finally:
            connection.close()
            self._secure_database_files()

    @staticmethod
    def _create_users_table(
        connection: sqlite3.Connection,
        table_name: str,
        *,
        if_not_exists: bool = False,
    ) -> None:
        """Create the strict two-role user table used by fresh and migrated stores."""
        if table_name not in {"users", "users_without_viewer"}:
            raise ValueError("用户表名称无效")
        existence_clause = "IF NOT EXISTS " if if_not_exists else ""
        connection.execute(
            f"""
            CREATE TABLE {existence_clause}{table_name} (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('admin', 'operator')),
                status TEXT NOT NULL CHECK (status IN ('active', 'disabled', 'locked')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_login_at TEXT
            )
            """
        )

    def _remove_viewer_role(self, connection: sqlite3.Connection) -> None:
        """Disable former viewers and rebuild the table without the removed role."""
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'users'"
        ).fetchone()
        if row is None or "'viewer'" not in str(row["sql"]):
            return

        now = _utc_now()
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE sessions
                SET revoked_at = ?
                WHERE revoked_at IS NULL
                  AND user_id IN (SELECT id FROM users WHERE role = 'viewer')
                """,
                (now,),
            )
            connection.execute(
                """
                INSERT INTO audit_logs (
                    user_id, action, resource_type, resource_id, ip_address,
                    detail, created_at
                )
                SELECT
                    id, 'remove_viewer_role', 'user', id, '',
                    'role=operator,status=disabled', ?
                FROM users
                WHERE role = 'viewer'
                """,
                (now,),
            )
            connection.execute("DROP TABLE IF EXISTS users_without_viewer")
            self._create_users_table(connection, "users_without_viewer")
            connection.execute(
                """
                INSERT INTO users_without_viewer (
                    id, username, display_name, password_hash, role, status,
                    created_at, updated_at, last_login_at
                )
                SELECT
                    id,
                    username,
                    display_name,
                    password_hash,
                    CASE WHEN role = 'viewer' THEN 'operator' ELSE role END,
                    CASE WHEN role = 'viewer' THEN 'disabled' ELSE status END,
                    created_at,
                    CASE WHEN role = 'viewer' THEN ? ELSE updated_at END,
                    last_login_at
                FROM users
                """,
                (now,),
            )
            connection.execute("DROP TABLE users")
            connection.execute("ALTER TABLE users_without_viewer RENAME TO users")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RuntimeError("移除只读角色后数据库外键校验失败")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

    def _initialize(self) -> None:
        with self._write_lock, self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            self._create_users_table(connection, "users", if_not_exists=True)
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    token_hash TEXT NOT NULL UNIQUE,
                    csrf_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    ip_address TEXT NOT NULL,
                    user_agent TEXT NOT NULL,
                    revoked_at TEXT
                );

                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                    action TEXT NOT NULL,
                    resource_type TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    ip_address TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_sessions_token_hash
                    ON sessions(token_hash);
                CREATE INDEX IF NOT EXISTS idx_sessions_user_id
                    ON sessions(user_id);
                CREATE INDEX IF NOT EXISTS idx_audit_user_created
                    ON audit_logs(user_id, created_at);
                """
            )
            connection.commit()
            self._remove_viewer_role(connection)
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_pairing_codes (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    code_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_at TEXT
                );

                CREATE TABLE IF NOT EXISTS agent_devices (
                    agent_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    token_hash TEXT NOT NULL UNIQUE,
                    device_name TEXT NOT NULL,
                    system TEXT NOT NULL,
                    version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_agent_pairing_user
                    ON agent_pairing_codes(user_id);
                CREATE INDEX IF NOT EXISTS idx_agent_devices_user
                    ON agent_devices(user_id);
                CREATE INDEX IF NOT EXISTS idx_agent_devices_token
                    ON agent_devices(token_hash);
                """
            )
            connection.commit()
        self._secure_database_files()

    @staticmethod
    def _user(row: sqlite3.Row) -> User:
        return User(
            id=row["id"],
            username=row["username"],
            display_name=row["display_name"],
            role=row["role"],
            status=row["status"],
        )

    @staticmethod
    def _agent_device(row: sqlite3.Row) -> AgentDevice:
        return AgentDevice(
            agent_id=row["agent_id"],
            user_id=row["user_id"],
            device_name=row["device_name"],
            system=row["system"],
            version=row["version"],
            created_at=row["created_at"],
            last_seen_at=row["last_seen_at"],
            expires_at=row["expires_at"],
            revoked_at=row["revoked_at"],
        )

    def user_count(self) -> int:
        """Return the number of provisioned application users."""
        with self._connection() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM users").fetchone()
        return int(row["count"] if row else 0)

    def create_user(
        self,
        *,
        username: str,
        display_name: str,
        password_hash: str,
        role: str,
    ) -> User:
        """Insert a user with an immutable random workspace identifier."""
        user_id = uuid.uuid4().hex
        now = _utc_now()
        with self._write_lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO users (
                    id, username, display_name, password_hash, role, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (user_id, username, display_name, password_hash, role, now, now),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        assert row is not None
        return self._user(row)

    def register_operator(
        self,
        *,
        username: str,
        display_name: str,
        password_hash: str,
    ) -> User:
        """Create an operator only after an active administrator exists."""
        user_id = uuid.uuid4().hex
        now = _utc_now()
        with self._write_lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            admin = connection.execute(
                "SELECT 1 FROM users WHERE role = 'admin' AND status = 'active' LIMIT 1"
            ).fetchone()
            if admin is None:
                connection.rollback()
                raise ValueError("请先在服务器本机创建初始管理员")
            try:
                connection.execute(
                    """
                    INSERT INTO users (
                        id, username, display_name, password_hash, role, status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'operator', 'active', ?, ?)
                    """,
                    (user_id, username, display_name, password_hash, now, now),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            created = connection.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        assert created is not None
        return self._user(created)

    def create_initial_user(
        self,
        *,
        username: str,
        display_name: str,
        password_hash: str,
    ) -> User:
        """Atomically create the first administrator and close bootstrap races."""
        user_id = uuid.uuid4().hex
        now = _utc_now()
        with self._write_lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT COUNT(*) AS count FROM users").fetchone()
            if row and int(row["count"]) != 0:
                raise ValueError("系统已经完成初始化")
            connection.execute(
                """
                INSERT INTO users (
                    id, username, display_name, password_hash, role, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'admin', 'active', ?, ?)
                """,
                (user_id, username, display_name, password_hash, now, now),
            )
            connection.commit()
            created = connection.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        assert created is not None
        return self._user(created)

    def list_users(self) -> list[User]:
        """Return all users without password or session material."""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM users ORDER BY username COLLATE NOCASE"
            ).fetchall()
        return [self._user(row) for row in rows]

    def get_user(self, user_id: str) -> User | None:
        """Look up a user by immutable identifier."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return self._user(row) if row else None

    def update_user(
        self,
        user_id: str,
        *,
        display_name: str | None,
        role: str | None,
        status: str | None,
    ) -> User:
        """Update one user while preserving at least one active administrator."""
        with self._write_lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if current is None:
                raise KeyError(user_id)

            next_role = role if role is not None else str(current["role"])
            next_status = status if status is not None else str(current["status"])
            removes_active_admin = (
                current["role"] == "admin"
                and current["status"] == "active"
                and (next_role != "admin" or next_status != "active")
            )
            if removes_active_admin:
                row = connection.execute(
                    "SELECT COUNT(*) AS count FROM users "
                    "WHERE role = 'admin' AND status = 'active'"
                ).fetchone()
                if row and int(row["count"]) <= 1:
                    raise ValueError("必须保留至少一个启用状态的管理员")

            connection.execute(
                """
                UPDATE users
                SET display_name = ?, role = ?, status = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    display_name if display_name is not None else current["display_name"],
                    next_role,
                    next_status,
                    _utc_now(),
                    user_id,
                ),
            )
            if next_status != "active":
                connection.execute(
                    "UPDATE sessions SET revoked_at = ? "
                    "WHERE user_id = ? AND revoked_at IS NULL",
                    (_utc_now(), user_id),
                )
                connection.execute(
                    "UPDATE agent_devices SET revoked_at = ? "
                    "WHERE user_id = ? AND revoked_at IS NULL",
                    (_utc_now(), user_id),
                )
            connection.commit()
            updated = connection.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        assert updated is not None
        return self._user(updated)

    def set_password_and_revoke_sessions(
        self, user_id: str, password_hash: str
    ) -> User:
        """Replace a password and invalidate every existing login atomically."""
        now = _utc_now()
        with self._write_lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if current is None:
                raise KeyError(user_id)
            connection.execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
                (password_hash, now, user_id),
            )
            connection.execute(
                "UPDATE sessions SET revoked_at = ? "
                "WHERE user_id = ? AND revoked_at IS NULL",
                (now, user_id),
            )
            connection.execute(
                "UPDATE agent_devices SET revoked_at = ? "
                "WHERE user_id = ? AND revoked_at IS NULL",
                (now, user_id),
            )
            connection.commit()
        return self._user(current)

    def revoke_user_sessions(self, user_id: str) -> User:
        """Invalidate every session belonging to a known user."""
        now = _utc_now()
        with self._write_lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if current is None:
                raise KeyError(user_id)
            connection.execute(
                "UPDATE sessions SET revoked_at = ? "
                "WHERE user_id = ? AND revoked_at IS NULL",
                (now, user_id),
            )
            connection.execute(
                "UPDATE agent_devices SET revoked_at = ? "
                "WHERE user_id = ? AND revoked_at IS NULL",
                (now, user_id),
            )
            connection.commit()
        return self._user(current)

    def create_agent_pairing_code(
        self, *, user_id: str, code_hash: str, expires_at: str
    ) -> str:
        """Replace unused codes for a user and persist only the new code hash."""
        pairing_id = uuid.uuid4().hex
        now = _utc_now()
        with self._write_lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM users WHERE id = ? AND status = 'active'", (user_id,)
            ).fetchone() is None:
                connection.rollback()
                raise KeyError(user_id)
            connection.execute(
                "DELETE FROM agent_pairing_codes WHERE user_id = ? OR expires_at <= ?",
                (user_id, now),
            )
            connection.execute(
                """
                INSERT INTO agent_pairing_codes (
                    id, user_id, code_hash, created_at, expires_at, used_at
                ) VALUES (?, ?, ?, ?, ?, NULL)
                """,
                (pairing_id, user_id, code_hash, now, expires_at),
            )
            connection.commit()
        return pairing_id

    def consume_agent_pairing_code(
        self,
        *,
        code_hash: str,
        agent_id: str,
        token_hash: str,
        device_name: str,
        system: str,
        version: str,
        expires_at: str,
    ) -> AuthenticatedAgent | None:
        """Atomically consume a code and replace the user's paired device."""
        now = _utc_now()
        with self._write_lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT p.id AS pairing_id, u.*
                FROM agent_pairing_codes p
                JOIN users u ON u.id = p.user_id
                WHERE p.code_hash = ?
                  AND p.used_at IS NULL
                  AND p.expires_at > ?
                  AND u.status = 'active'
                """,
                (code_hash, now),
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            user = self._user(row)
            updated = connection.execute(
                "UPDATE agent_pairing_codes SET used_at = ? "
                "WHERE id = ? AND used_at IS NULL",
                (now, row["pairing_id"]),
            )
            if updated.rowcount != 1:
                connection.rollback()
                return None
            connection.execute(
                "UPDATE agent_devices SET revoked_at = ? "
                "WHERE user_id = ? AND revoked_at IS NULL",
                (now, user.id),
            )
            connection.execute("DELETE FROM agent_devices WHERE agent_id = ?", (agent_id,))
            connection.execute(
                """
                INSERT INTO agent_devices (
                    agent_id, user_id, token_hash, device_name, system, version,
                    created_at, last_seen_at, expires_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    agent_id,
                    user.id,
                    token_hash,
                    device_name,
                    system,
                    version,
                    now,
                    now,
                    expires_at,
                ),
            )
            device_row = connection.execute(
                "SELECT * FROM agent_devices WHERE agent_id = ?", (agent_id,)
            ).fetchone()
            connection.commit()
        assert device_row is not None
        return AuthenticatedAgent(user=user, device=self._agent_device(device_row))

    def resolve_agent_token(self, token_hash: str, now: str) -> AuthenticatedAgent | None:
        """Resolve an active device token and update its last-seen timestamp."""
        with self._write_lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT
                    d.agent_id, d.user_id, d.device_name, d.system, d.version,
                    d.created_at, d.last_seen_at, d.expires_at, d.revoked_at,
                    u.id AS id, u.username, u.display_name, u.role, u.status
                FROM agent_devices d
                JOIN users u ON u.id = d.user_id
                WHERE d.token_hash = ?
                  AND d.revoked_at IS NULL
                  AND d.expires_at > ?
                  AND u.status = 'active'
                """,
                (token_hash, now),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE agent_devices SET last_seen_at = ? WHERE agent_id = ?",
                (now, row["agent_id"]),
            )
            connection.commit()
        return AuthenticatedAgent(user=self._user(row), device=self._agent_device(row))

    def list_agent_devices(self, user_id: str) -> list[AgentDevice]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_devices WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
        return [self._agent_device(row) for row in rows]

    def revoke_agent_device(self, user_id: str, agent_id: str) -> bool:
        with self._write_lock, self._connection() as connection:
            updated = connection.execute(
                "UPDATE agent_devices SET revoked_at = ? "
                "WHERE user_id = ? AND agent_id = ? AND revoked_at IS NULL",
                (_utc_now(), user_id, agent_id),
            )
            connection.commit()
        return updated.rowcount == 1

    def credentials_for_username(self, username: str) -> tuple[User, str] | None:
        """Return login verification material without exposing it to routers."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username = ? COLLATE NOCASE",
                (username,),
            ).fetchone()
        if row is None:
            return None
        return self._user(row), str(row["password_hash"])

    def update_password_hash(self, user_id: str, password_hash: str) -> None:
        """Persist an opportunistic Argon2 policy rehash after successful login."""
        with self._write_lock, self._connection() as connection:
            connection.execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
                (password_hash, _utc_now(), user_id),
            )
            connection.commit()

    def mark_login(self, user_id: str) -> None:
        """Record the latest successful login time for administration audits."""
        now = _utc_now()
        with self._write_lock, self._connection() as connection:
            connection.execute(
                "UPDATE users SET last_login_at = ?, updated_at = ? WHERE id = ?",
                (now, now, user_id),
            )
            connection.commit()

    def create_session(
        self,
        *,
        user_id: str,
        token_hash: str,
        csrf_hash: str,
        expires_at: str,
        ip_address: str,
        user_agent: str,
    ) -> str:
        """Persist only hashes of the opaque session and CSRF tokens."""
        session_id = uuid.uuid4().hex
        with self._write_lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    id, user_id, token_hash, csrf_hash, created_at, expires_at,
                    ip_address, user_agent, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    session_id,
                    user_id,
                    token_hash,
                    csrf_hash,
                    _utc_now(),
                    expires_at,
                    ip_address,
                    user_agent,
                ),
            )
            connection.commit()
        return session_id

    def resolve_session(self, token_hash: str, now: str) -> AuthenticatedSession | None:
        """Resolve an active session and its latest user role/status."""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT
                    s.id AS session_id,
                    s.expires_at,
                    u.*
                FROM sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = ?
                  AND s.revoked_at IS NULL
                  AND s.expires_at > ?
                  AND u.status = 'active'
                """,
                (token_hash, now),
            ).fetchone()
        if row is None:
            return None
        return AuthenticatedSession(
            user=self._user(row),
            session_id=row["session_id"],
            expires_at=row["expires_at"],
        )

    def csrf_hash_for_session(self, session_id: str) -> str | None:
        """Read the expected double-submit token hash for an active session."""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT csrf_hash FROM sessions
                WHERE id = ? AND revoked_at IS NULL AND expires_at > ?
                """,
                (session_id, _utc_now()),
            ).fetchone()
        return str(row["csrf_hash"]) if row else None

    def revoke_session(self, session_id: str) -> None:
        """Invalidate one server-side session without touching running jobs."""
        with self._write_lock, self._connection() as connection:
            connection.execute(
                "UPDATE sessions SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (_utc_now(), session_id),
            )
            connection.commit()

    def prune_sessions(self, now: str) -> None:
        """Delete expired or long-revoked sessions to bound database growth."""
        with self._write_lock, self._connection() as connection:
            connection.execute(
                "DELETE FROM sessions WHERE expires_at <= ? OR revoked_at IS NOT NULL",
                (now,),
            )
            connection.commit()

    def record_audit(
        self,
        *,
        user_id: str | None,
        action: str,
        resource_type: str = "",
        resource_id: str = "",
        ip_address: str = "",
        detail: str = "",
    ) -> None:
        """Append a security-relevant action without recording secrets."""
        with self._write_lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO audit_logs (
                    user_id, action, resource_type, resource_id, ip_address,
                    detail, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    action,
                    resource_type,
                    resource_id,
                    ip_address,
                    detail,
                    _utc_now(),
                ),
            )
            connection.commit()
