from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class User:
    """Authenticated application user without password material."""

    id: str
    username: str
    display_name: str
    role: str
    status: str

    @property
    def can_operate(self) -> bool:
        return self.role in {"admin", "operator"} and self.status == "active"


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    """A validated user session resolved from its opaque cookie token."""

    user: User
    session_id: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class AgentDevice:
    """A paired desktop agent without its bearer-token material."""

    agent_id: str
    user_id: str
    device_name: str
    system: str
    version: str
    created_at: str
    last_seen_at: str | None
    expires_at: str
    revoked_at: str | None


@dataclass(frozen=True, slots=True)
class AuthenticatedAgent:
    """A validated agent bearer token bound to one application user/device."""

    user: User
    device: AgentDevice
