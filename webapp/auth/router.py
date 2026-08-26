from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, HTTPException, Request, Response

from webapp.auth.contracts import (
    AdminUserResponse,
    AuthStatusResponse,
    BootstrapAdminRequest,
    CreateUserRequest,
    LoginRequest,
    RegisterUserRequest,
    ResetPasswordRequest,
    UpdateUserRequest,
    UserResponse,
)
from webapp.auth.dependencies import require_admin, require_session
from webapp.auth.models import AuthenticatedSession, User
from webapp.auth.service import AuthenticationError, AuthService, UserNotFoundError


_SESSION_COOKIE = "mpau_session_v2"
_CSRF_COOKIE = "mpau_csrf_v2"


def _user_response(user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
    )


def _admin_user_response(user: User) -> AdminUserResponse:
    """Map a domain user to the administration response contract."""
    return AdminUserResponse(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        status=user.status,
    )


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _set_session_cookies(
    response: Response,
    *,
    session_token: str,
    csrf_token: str,
    max_age: int,
    secure: bool,
) -> None:
    response.set_cookie(
        _SESSION_COOKIE,
        session_token,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        _CSRF_COOKIE,
        csrf_token,
        max_age=max_age,
        httponly=False,
        secure=secure,
        samesite="lax",
        path="/",
    )


def create_auth_router(
    service: AuthService,
    *,
    allow_remote_bootstrap: bool = False,
    delete_user_data: Callable[[str], None] | None = None,
) -> APIRouter:
    """Expose authentication without coupling it to publishing workspaces."""
    router = APIRouter(tags=["auth"])

    @router.get("/api/auth/status", response_model=AuthStatusResponse)
    def status(request: Request) -> AuthStatusResponse:
        session = getattr(request.state, "auth_session", None)
        return AuthStatusResponse(
            setup_required=service.setup_required(),
            authenticated=session is not None,
            user=_user_response(session.user) if session else None,
        )

    @router.post("/api/auth/bootstrap", response_model=UserResponse, status_code=201)
    def bootstrap(
        payload: BootstrapAdminRequest,
        request: Request,
        response: Response,
    ) -> UserResponse:
        if not allow_remote_bootstrap and (
            not request.client
            or request.client.host not in {"127.0.0.1", "::1", "testclient"}
        ):
            raise HTTPException(status_code=403, detail="初始管理员只能在服务器本机创建")
        try:
            user = service.bootstrap_admin(
                username=payload.username,
                display_name=payload.display_name,
                password=payload.password,
            )
            session, token, csrf_token = service.authenticate(
                username=payload.username,
                password=payload.password,
                ip_address=_client_ip(request),
                user_agent=request.headers.get("user-agent", ""),
            )
        except AuthenticationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        _set_session_cookies(
            response,
            session_token=token,
            csrf_token=csrf_token,
            max_age=service.session_seconds,
            secure=request.url.scheme == "https",
        )
        return _user_response(session.user if session else user)

    @router.post("/api/auth/login", response_model=UserResponse)
    def login(payload: LoginRequest, request: Request, response: Response) -> UserResponse:
        try:
            session, token, csrf_token = service.authenticate(
                username=payload.username,
                password=payload.password,
                ip_address=_client_ip(request),
                user_agent=request.headers.get("user-agent", ""),
            )
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        _set_session_cookies(
            response,
            session_token=token,
            csrf_token=csrf_token,
            max_age=service.session_seconds,
            secure=request.url.scheme == "https",
        )
        return _user_response(session.user)

    @router.post("/api/auth/register", response_model=UserResponse, status_code=201)
    def register(
        payload: RegisterUserRequest, request: Request, response: Response
    ) -> UserResponse:
        try:
            user = service.register_operator(
                username=payload.username,
                display_name=payload.display_name,
                password=payload.password,
                ip_address=_client_ip(request),
            )
            session, token, csrf_token = service.authenticate(
                username=payload.username,
                password=payload.password,
                ip_address=_client_ip(request),
                user_agent=request.headers.get("user-agent", ""),
            )
        except AuthenticationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        _set_session_cookies(
            response,
            session_token=token,
            csrf_token=csrf_token,
            max_age=service.session_seconds,
            secure=request.url.scheme == "https",
        )
        return _user_response(session.user if session else user)

    @router.get("/api/auth/me", response_model=UserResponse)
    def me(request: Request) -> UserResponse:
        return _user_response(require_session(request).user)

    @router.post("/api/auth/logout", status_code=204)
    def logout(request: Request, response: Response) -> Response:
        session: AuthenticatedSession = require_session(request)
        service.logout(session, ip_address=_client_ip(request))
        response.delete_cookie(_SESSION_COOKIE, path="/")
        response.delete_cookie(_CSRF_COOKIE, path="/")
        response.status_code = 204
        return response

    @router.get("/api/admin/users", response_model=list[AdminUserResponse])
    def list_users(request: Request) -> list[AdminUserResponse]:
        require_admin(request)
        return [_admin_user_response(user) for user in service.list_users()]

    @router.post(
        "/api/admin/users", response_model=AdminUserResponse, status_code=201
    )
    def create_user(
        payload: CreateUserRequest, request: Request
    ) -> AdminUserResponse:
        actor = require_admin(request)
        try:
            user = service.create_user(
                actor_user_id=actor.id,
                username=payload.username,
                display_name=payload.display_name,
                password=payload.password,
                role=payload.role,
                ip_address=_client_ip(request),
            )
        except AuthenticationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _admin_user_response(user)

    @router.patch("/api/admin/users/{user_id}", response_model=AdminUserResponse)
    def update_user(
        user_id: str, payload: UpdateUserRequest, request: Request
    ) -> AdminUserResponse:
        actor = require_admin(request)
        try:
            user = service.update_user(
                actor_user_id=actor.id,
                user_id=user_id,
                display_name=payload.display_name,
                role=payload.role,
                status=payload.status,
                ip_address=_client_ip(request),
            )
        except UserNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except AuthenticationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _admin_user_response(user)

    @router.post(
        "/api/admin/users/{user_id}/reset-password",
        response_model=AdminUserResponse,
    )
    def reset_password(
        user_id: str, payload: ResetPasswordRequest, request: Request
    ) -> AdminUserResponse:
        actor = require_admin(request)
        try:
            user = service.reset_password(
                actor_user_id=actor.id,
                user_id=user_id,
                password=payload.password,
                ip_address=_client_ip(request),
            )
        except UserNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except AuthenticationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _admin_user_response(user)

    @router.post(
        "/api/admin/users/{user_id}/revoke-sessions",
        response_model=AdminUserResponse,
    )
    def revoke_sessions(user_id: str, request: Request) -> AdminUserResponse:
        actor = require_admin(request)
        try:
            user = service.revoke_user_sessions(
                actor_user_id=actor.id,
                user_id=user_id,
                ip_address=_client_ip(request),
            )
        except UserNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _admin_user_response(user)

    @router.delete("/api/admin/users/{user_id}", status_code=204)
    def delete_user(user_id: str, request: Request, response: Response) -> Response:
        actor = require_admin(request)
        try:
            service.validate_user_deletion(actor_user_id=actor.id, user_id=user_id)
            if delete_user_data is not None:
                delete_user_data(user_id)
            service.delete_user(
                actor_user_id=actor.id,
                user_id=user_id,
                ip_address=_client_ip(request),
            )
        except UserNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except AuthenticationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"删除用户工作区数据失败：{exc}") from exc
        response.status_code = 204
        return response

    return router
