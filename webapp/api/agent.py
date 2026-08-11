from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from webapp.auth import AuthService
from webapp.auth.models import AuthenticatedAgent
from webapp.auth.service import AuthenticationError
from webapp.workspaces.service import UserWorkspace

_AGENT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class AgentHello(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=32, max_length=32)
    device_name: str = Field(min_length=1, max_length=120)
    system: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=40)


class AgentIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=32, max_length=32)


class AgentPairRequest(AgentHello):
    pairing_code: str = Field(min_length=10, max_length=20)


class AgentCompletion(AgentIdentity):
    status: Literal["succeeded", "failed", "cancelled", "uncertain"]
    message: str = Field(default="", max_length=1000)
    error: str = Field(default="", max_length=4000)
    result: dict[str, Any] = Field(default_factory=dict)
    logs: list[str] = Field(default_factory=list, max_length=500)


def _validate_agent_id(agent_id: str) -> str:
    if not _AGENT_ID_PATTERN.fullmatch(agent_id):
        raise HTTPException(status_code=422, detail="本地代理 ID 格式无效")
    return agent_id


def _agent_job_response(job: dict[str, Any]) -> dict[str, Any]:
    payload = dict(job.get("payload") or {})
    payload.pop("video_path", None)
    response = {
        key: value
        for key, value in job.items()
        if key not in {"result", "error", "payload"}
    }
    response["payload"] = payload
    if job["kind"] == "publish":
        response["video_download_url"] = f"/api/agent/jobs/{job['id']}/video"
    return response


def create_agent_router(
    workspace_resolver: Callable[[Request], UserWorkspace],
    workspace_for_user: Callable[[str], UserWorkspace],
    auth_service: AuthService,
    installer_path: Path | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/agent", tags=["local-agent"])

    def request_ip(request: Request) -> str:
        return request.client.host if request.client else ""

    def authenticated_agent(request: Request) -> AuthenticatedAgent:
        agent = getattr(request.state, "agent_device", None)
        if agent is None:
            raise HTTPException(
                status_code=401,
                detail="本地执行助手尚未配对或设备授权已经失效",
            )
        return agent

    def bound_agent_id(request: Request, supplied: str) -> str:
        selected = _validate_agent_id(supplied)
        authenticated = authenticated_agent(request)
        if selected != authenticated.device.agent_id:
            raise HTTPException(status_code=403, detail="设备令牌与本地代理 ID 不匹配")
        return selected

    def remote_manager(request: Request):
        authenticated = authenticated_agent(request)
        manager = workspace_for_user(authenticated.user.id).task_manager
        if not getattr(manager, "remote_execution", False):
            raise HTTPException(status_code=409, detail="服务没有启用本地代理执行模式")
        return manager

    @router.post("/pairing-code")
    def create_pairing_code(request: Request) -> dict[str, Any]:
        workspace = workspace_resolver(request)
        try:
            code, expires_at = auth_service.issue_agent_pairing_code(
                user_id=workspace.user_id,
                ip_address=request_ip(request),
            )
        except AuthenticationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"pairing_code": code, "expires_at": expires_at}

    @router.post("/pair")
    def pair(payload: AgentPairRequest, request: Request) -> dict[str, Any]:
        agent_id = _validate_agent_id(payload.agent_id)
        try:
            paired, token = auth_service.pair_agent(
                pairing_code=payload.pairing_code,
                agent_id=agent_id,
                device_name=payload.device_name,
                system=payload.system,
                version=payload.version,
                ip_address=request_ip(request),
            )
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        workspace = workspace_for_user(paired.user.id)
        manager = workspace.task_manager
        if getattr(manager, "remote_execution", False):
            manager.disconnect_all_agents()
        return {
            "agent_token": token,
            "agent_id": paired.device.agent_id,
            "expires_at": paired.device.expires_at,
            "user": {
                "id": paired.user.id,
                "username": paired.user.username,
                "display_name": paired.user.display_name,
                "role": paired.user.role,
            },
        }

    @router.get("/devices")
    def devices(request: Request) -> dict[str, Any]:
        workspace = workspace_resolver(request)
        return {
            "devices": [
                asdict(device)
                for device in auth_service.list_agent_devices(workspace.user_id)
            ]
        }

    @router.delete("/devices/{agent_id}")
    def revoke_device(agent_id: str, request: Request) -> dict[str, Any]:
        selected = _validate_agent_id(agent_id)
        workspace = workspace_resolver(request)
        try:
            auth_service.revoke_agent_device(
                user_id=workspace.user_id,
                agent_id=selected,
                ip_address=request_ip(request),
            )
        except AuthenticationError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if getattr(workspace.task_manager, "remote_execution", False):
            workspace.task_manager.disconnect_agent(selected)
        return {"revoked_agent_id": selected}

    @router.delete("/self/{agent_id}")
    def revoke_self(agent_id: str, request: Request) -> dict[str, Any]:
        authenticated = authenticated_agent(request)
        selected = bound_agent_id(request, agent_id)
        workspace = workspace_for_user(authenticated.user.id)
        try:
            auth_service.revoke_agent_device(
                user_id=authenticated.user.id,
                agent_id=selected,
                ip_address=request_ip(request),
            )
        except AuthenticationError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if getattr(workspace.task_manager, "remote_execution", False):
            workspace.task_manager.disconnect_agent(selected)
        return {"revoked_agent_id": selected}

    @router.get("/status")
    def status(request: Request) -> dict[str, Any]:
        workspace = workspace_resolver(request)
        manager = workspace.task_manager
        if not getattr(manager, "remote_execution", False):
            return {
                "execution_mode": "server",
                "online": False,
                "agents": [],
                "lease_seconds": 0,
            }
        return manager.agent_status() | {
            "installer": {
                "available": bool(installer_path and installer_path.is_file()),
                "download_url": "/downloads/MPAU-Agent-Setup.exe",
            }
        }

    @router.post("/connect")
    def connect(payload: AgentHello, request: Request) -> dict[str, Any]:
        manager = remote_manager(request)
        authenticated = authenticated_agent(request)
        agent_id = bound_agent_id(request, payload.agent_id)
        try:
            agent = manager.connect_agent(
                agent_id=agent_id,
                device_name=payload.device_name,
                system=payload.system,
                version=payload.version,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "agent": agent,
            "lease_seconds": manager.lease_seconds,
            "poll_seconds": 2,
            "user": {
                "id": authenticated.user.id,
                "username": authenticated.user.username,
                "display_name": authenticated.user.display_name,
                "role": authenticated.user.role,
            },
        }

    @router.post("/claim")
    def claim(payload: AgentIdentity, request: Request) -> dict[str, Any]:
        manager = remote_manager(request)
        agent_id = bound_agent_id(request, payload.agent_id)
        try:
            job = manager.claim_next_job(agent_id)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "job": _agent_job_response(job) if job else None,
            "lease_seconds": manager.lease_seconds,
        }

    @router.post("/jobs/{job_id}/heartbeat")
    def heartbeat(
        job_id: str, payload: AgentIdentity, request: Request
    ) -> dict[str, Any]:
        manager = remote_manager(request)
        agent_id = bound_agent_id(request, payload.agent_id)
        try:
            return manager.heartbeat(job_id, agent_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/jobs/{job_id}/video")
    def download_video(
        job_id: str,
        agent_id: str,
        request: Request,
    ) -> FileResponse:
        authenticated = authenticated_agent(request)
        workspace = workspace_for_user(authenticated.user.id)
        manager = remote_manager(request)
        selected_agent = bound_agent_id(request, agent_id)
        try:
            job = manager.get_claimed_job(job_id, selected_agent)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if job["kind"] != "publish":
            raise HTTPException(status_code=409, detail="该任务没有视频文件")
        path = Path(job["payload"]["video_path"]).resolve()
        allowed_roots = (workspace.paths.uploads.resolve(), workspace.paths.media.resolve())
        if not any(path.is_relative_to(root) for root in allowed_roots) or not path.is_file():
            raise HTTPException(status_code=404, detail="任务视频不存在")
        filename = Path(job["payload"].get("original_filename") or path.name).name
        return FileResponse(path, media_type="application/octet-stream", filename=filename)

    @router.post("/jobs/{job_id}/complete")
    def complete(
        job_id: str, payload: AgentCompletion, request: Request
    ) -> dict[str, Any]:
        manager = remote_manager(request)
        agent_id = bound_agent_id(request, payload.agent_id)
        try:
            job = manager.complete_agent_job(
                job_id,
                agent_id,
                status=payload.status,
                message=payload.message,
                error=payload.error,
                result=payload.result,
                logs=payload.logs,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"job": _agent_job_response(job)}

    return router
