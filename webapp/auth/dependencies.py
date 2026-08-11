from __future__ import annotations

from fastapi import HTTPException, Request

from webapp.auth.models import AuthenticatedSession, User


def require_session(request: Request) -> AuthenticatedSession:
    """Reject unauthenticated API calls before business services are resolved."""
    session = getattr(request.state, "auth_session", None)
    if session is None:
        raise HTTPException(status_code=401, detail="请先登录")
    return session


def require_user(request: Request) -> User:
    return require_session(request).user


def require_operator(request: Request) -> User:
    user = require_user(request)
    if not user.can_operate:
        raise HTTPException(status_code=403, detail="当前账号没有执行发布操作的权限")
    return user


def require_admin(request: Request) -> User:
    user = require_user(request)
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可以执行此操作")
    return user
