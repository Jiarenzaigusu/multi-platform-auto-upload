from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, build_opener


class AgentApiError(RuntimeError):
    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class AgentApiClient:
    """Authenticated standard-library client used by the desktop agent."""

    def __init__(self, server_url: str, agent_token: str = "") -> None:
        self.server_url = server_url.rstrip("/")
        self.agent_token = agent_token
        self.opener = build_opener()
        self.user: dict[str, Any] | None = None

    def _url(self, path: str) -> str:
        return f"{self.server_url}/{path.lstrip('/')}"

    @staticmethod
    def _error_message(raw: bytes, fallback: str) -> str:
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return fallback
        detail = body.get("detail") if isinstance(body, dict) else None
        return detail if isinstance(detail, str) else fallback

    def request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if authenticated:
            if not self.agent_token:
                raise AgentApiError("本地执行助手尚未配对", 401)
            headers["Authorization"] = f"Bearer {self.agent_token}"
        request = Request(self._url(path), data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=45) as response:
                raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
        except HTTPError as exc:
            raw = exc.read(200_000)
            raise AgentApiError(
                self._error_message(raw, f"请求失败（HTTP {exc.code}）"), exc.code
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise AgentApiError(f"无法连接发布服务：{exc}") from exc

    def connect(self, hello: dict[str, Any]) -> dict[str, Any]:
        return self.request_json("/api/agent/connect", method="POST", payload=hello)

    def pair(self, hello: dict[str, Any], pairing_code: str) -> dict[str, Any]:
        response = self.request_json(
            "/api/agent/pair",
            method="POST",
            payload={**hello, "pairing_code": pairing_code},
            authenticated=False,
        )
        token = response.get("agent_token")
        if not isinstance(token, str) or not token:
            raise AgentApiError("服务器没有返回设备令牌")
        self.agent_token = token
        self.user = response.get("user")
        return response

    def claim(
        self, agent_id: str, *, wait_seconds: float = 0
    ) -> dict[str, Any] | None:
        query = urlencode({"wait_seconds": max(0.0, min(30.0, wait_seconds))})
        response = self.request_json(
            f"/api/agent/claim?{query}",
            method="POST",
            payload={"agent_id": agent_id},
        )
        return response.get("job")

    def heartbeat(self, job_id: str, agent_id: str) -> dict[str, Any]:
        return self.request_json(
            f"/api/agent/jobs/{quote(job_id)}/heartbeat",
            method="POST",
            payload={"agent_id": agent_id},
        )

    def authorize_local_upload(
        self, ticket: str, origin: str, *, reserve: bool = True
    ) -> dict[str, Any]:
        return self.request_json(
            "/api/agent/local-upload/authorize",
            method="POST",
            payload={"ticket": ticket, "origin": origin, "reserve": reserve},
        )

    def complete_local_upload(
        self,
        ticket: str,
        origin: str,
        *,
        asset_id: str,
        sha256: str,
        size: int,
    ) -> dict[str, Any]:
        return self.request_json(
            "/api/agent/local-upload/complete",
            method="POST",
            payload={
                "ticket": ticket,
                "origin": origin,
                "reserve": False,
                "asset_id": asset_id,
                "sha256": sha256,
                "size": size,
            },
        )

    def latest_release(self) -> dict[str, Any]:
        return self.request_json("/api/agent/latest-release")

    def download_installer(
        self,
        destination: Path,
        *,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
        progress=None,
    ) -> None:
        """Stream the newest agent installer and verify size and SHA-256."""
        import hashlib

        if not self.agent_token:
            raise AgentApiError("本地执行助手尚未配对", 401)
        request = Request(
            self._url("/api/agent/download-installer"),
            headers={
                "Accept": "application/octet-stream",
                "Authorization": f"Bearer {self.agent_token}",
            },
        )
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = destination.with_name(destination.name + ".part")
        digest = hashlib.sha256()
        try:
            with self.opener.open(request, timeout=120) as response, temporary.open(
                "wb"
            ) as output:
                last_progress = time.monotonic()
                downloaded = 0
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
                    digest.update(chunk)
                    downloaded += len(chunk)
                    if expected_size is not None and downloaded > expected_size:
                        raise AgentApiError(
                            "更新安装包大小与服务器发布信息不一致",
                        )
                    if progress is not None and time.monotonic() - last_progress >= 10:
                        progress(downloaded)
                        last_progress = time.monotonic()
            if expected_size is not None and downloaded != expected_size:
                raise AgentApiError("更新安装包下载不完整")
            if expected_sha256 is not None and digest.hexdigest() != expected_sha256:
                raise AgentApiError("更新安装包校验失败，已取消安装")
            temporary.replace(destination)
            try:
                destination.chmod(0o600)
            except OSError:
                pass
        except HTTPError as exc:
            raw = exc.read(100_000)
            temporary.unlink(missing_ok=True)
            raise AgentApiError(
                self._error_message(raw, f"更新安装包下载失败（HTTP {exc.code}）"),
                exc.code,
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            temporary.unlink(missing_ok=True)
            raise AgentApiError(f"更新安装包下载失败：{exc}") from exc
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def revoke_device(self, agent_id: str) -> None:
        self.request_json(
            f"/api/agent/self/{quote(agent_id)}",
            method="DELETE",
        )

    def complete(
        self,
        job_id: str,
        *,
        agent_id: str,
        status: str,
        message: str,
        error: str = "",
        result: dict[str, Any] | None = None,
        logs: list[str] | None = None,
    ) -> dict[str, Any]:
        return self.request_json(
            f"/api/agent/jobs/{quote(job_id)}/complete",
            method="POST",
            payload={
                "agent_id": agent_id,
                "status": status,
                "message": message,
                "error": error[:4000],
                "result": result or {},
                "logs": (logs or [])[-500:],
            },
        )

    def download_video(
        self,
        job_id: str,
        agent_id: str,
        destination: Path,
        *,
        progress=None,
    ) -> None:
        self._download_file(
            f"/api/agent/jobs/{quote(job_id)}/video",
            agent_id,
            destination,
            resource_label="视频",
            progress=progress,
        )

    def download_cover_image(
        self,
        job_id: str,
        agent_id: str,
        destination: Path,
        *,
        progress=None,
    ) -> None:
        self._download_file(
            f"/api/agent/jobs/{quote(job_id)}/cover-image",
            agent_id,
            destination,
            resource_label="封面图片",
            progress=progress,
        )

    def download_article_image(
        self,
        job_id: str,
        agent_id: str,
        index: int,
        destination: Path,
        *,
        progress=None,
    ) -> None:
        self._download_file(
            f"/api/agent/jobs/{quote(job_id)}/article-images/{index}",
            agent_id,
            destination,
            resource_label="图文图片",
            progress=progress,
        )

    def _download_file(
        self,
        path: str,
        agent_id: str,
        destination: Path,
        *,
        resource_label: str,
        progress=None,
    ) -> None:
        query = urlencode({"agent_id": agent_id})
        path = f"{path}?{query}"
        if not self.agent_token:
            raise AgentApiError("本地执行助手尚未配对", 401)
        request = Request(
            self._url(path),
            headers={
                "Accept": "application/octet-stream",
                "Authorization": f"Bearer {self.agent_token}",
            },
        )
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with self.opener.open(request, timeout=120) as response, destination.open("wb") as output:
                last_progress = time.monotonic()
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
                    if progress is not None and time.monotonic() - last_progress >= 10:
                        progress()
                        last_progress = time.monotonic()
            try:
                destination.chmod(0o600)
            except OSError:
                pass
        except HTTPError as exc:
            raw = exc.read(100_000)
            destination.unlink(missing_ok=True)
            raise AgentApiError(
                self._error_message(
                    raw, f"{resource_label}下载失败（HTTP {exc.code}）"
                ),
                exc.code,
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            destination.unlink(missing_ok=True)
            raise AgentApiError(f"{resource_label}下载失败：{exc}") from exc
        except Exception:
            destination.unlink(missing_ok=True)
            raise
