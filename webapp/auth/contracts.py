from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class BootstrapAdminRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    display_name: str = Field(default="", max_length=80)
    password: str = Field(min_length=10, max_length=256)


class RegisterUserRequest(BaseModel):
    """Public self-registration payload; the server always assigns operator."""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=64)
    display_name: str = Field(default="", max_length=80)
    password: str = Field(min_length=10, max_length=256)


class UserResponse(BaseModel):
    id: str
    username: str
    display_name: str
    role: str


class AdminUserResponse(UserResponse):
    """User details that are only exposed to an administrator."""

    status: str


class CreateUserRequest(BaseModel):
    """Payload used by an administrator to provision a company user."""

    username: str = Field(min_length=3, max_length=64)
    display_name: str = Field(default="", max_length=80)
    password: str = Field(min_length=10, max_length=256)
    role: Literal["admin", "operator"] = "operator"


class UpdateUserRequest(BaseModel):
    """Mutable user attributes; usernames and immutable IDs never change."""

    display_name: str | None = Field(default=None, max_length=80)
    role: Literal["admin", "operator"] | None = None
    status: Literal["active", "disabled"] | None = None


class ResetPasswordRequest(BaseModel):
    """Administrator password-reset payload."""

    password: str = Field(min_length=10, max_length=256)


class AuthStatusResponse(BaseModel):
    setup_required: bool
    authenticated: bool
    user: UserResponse | None = None
