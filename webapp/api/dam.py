from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator
from urllib.parse import urljoin, urlsplit

import httpx


class DamApiError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 502):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class DamSettings:
    host: str = ""
    key: str = ""
    secret: str = ""
    tenant: str = ""
    catalog: str = ""

    @property
    def configured(self) -> bool:
        return all((self.host, self.key, self.secret, self.tenant, self.catalog))


class DamOpenApiClient:
    """Server-side adapter for the external DAM OpenAPI."""

    def __init__(self, settings: DamSettings, *, timeout: float = 60):
        self.settings = settings
        self.timeout = timeout
        self.base_url = settings.host.rstrip("/") + "/api/open/v1/"

    def _ensure_configured(self) -> None:
        if not self.settings.configured:
            raise DamApiError("DAM_NOT_CONFIGURED", "DAM 素材库尚未在服务端配置", 503)
        validate_dam_host(self.settings.host)

    async def request(
        self,
        method: str,
        path: str,
        *,
        context: bool = True,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        self._ensure_configured()
        headers = {
            "X-DAM-Open-Key": self.settings.key,
            "X-DAM-Open-Secret": self.settings.secret,
            "X-Request-Id": str(uuid.uuid4()),
            "Accept": "application/json",
        }
        if context:
            headers.update({
                "X-DAM-Open-Tenant": self.settings.tenant,
                "X-DAM-Open-Catalog": self.settings.catalog,
            })
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.request(
                    method,
                    urljoin(self.base_url, path.lstrip("/")),
                    headers=headers,
                    params=params,
                    json=body,
                )
            payload = response.json()
        except httpx.HTTPError as exc:
            raise DamApiError("DAM_NETWORK_ERROR", "无法连接 DAM 素材库") from exc
        except ValueError as exc:
            raise DamApiError("DAM_INVALID_RESPONSE", "DAM 返回了无法识别的响应") from exc
        if str(payload.get("code", "")) != "0":
            detail = payload.get("data") if isinstance(payload.get("data"), str) else ""
            message = str(payload.get("message") or detail or "DAM OpenAPI 调用失败")
            if detail and detail not in message:
                message += f"（{detail}）"
            status = response.status_code if response.status_code >= 400 else 502
            raise DamApiError(str(payload.get("code") or "DAM_ERROR"), message, status)
        return payload.get("data")

    async def bindings(self) -> list[dict[str, Any]]:
        data = await self.request("GET", "bindings", context=False)
        return data if isinstance(data, list) else []

    async def folders(self, parent_id: int | None = None) -> list[dict[str, Any]]:
        params = {"parentId": parent_id} if parent_id is not None else None
        data = await self.request("GET", "folders/children", params=params)
        return data if isinstance(data, list) else []

    async def assets(
        self,
        folder_id: int,
        *,
        page: int = 1,
        page_size: int = 40,
        keyword: str = "",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "context": {"type": "FOLDER", "folderId": folder_id, "includeSubfolders": False},
            "page": max(1, page),
            "pageSize": min(max(1, page_size), 100),
            "sort": {"field": "createTime", "order": "desc"},
        }
        if keyword.strip():
            body["keyword"] = keyword.strip()
        data = await self.request("POST", "assets/query", body=body)
        return data if isinstance(data, dict) else {"list": [], "total": 0}

    async def asset(self, asset_id: int) -> dict[str, Any]:
        data = await self.request("GET", f"assets/{asset_id}")
        if not isinstance(data, dict):
            raise DamApiError("DAM_INVALID_RESPONSE", "DAM 素材详情格式无效")
        return data


def validate_dam_host(raw_url: str) -> str:
    """Allow only the explicitly approved UAT gateway for DAM credentials."""
    parsed = urlsplit(raw_url)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower().rstrip(".") != "ross-api-uat.baozun.com"
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise DamApiError(
            "DAM_URL_INVALID",
            "测试期 DAM Host 仅允许 https://ross-api-uat.baozun.com",
            422,
        )
    return raw_url


def validate_download_url(raw_url: str, *, label: str) -> str:
    """Temporarily allow HTTP(S) DAM downloads without SSRF address filtering."""
    parsed = urlsplit(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise DamApiError("DAM_URL_INVALID", f"{label} 必须是有效的 HTTP(S) 地址", 422)
    return raw_url


async def stream_download(raw_url: str, *, max_bytes: int) -> tuple[httpx.Response, AsyncIterator[bytes]]:
    url = validate_download_url(raw_url, label="DAM 下载地址")
    client = httpx.AsyncClient(timeout=httpx.Timeout(60, read=None), follow_redirects=False)
    try:
        request = client.build_request("GET", url)
        response = await client.send(request, stream=True)
        response.raise_for_status()
        try:
            content_length = int(response.headers.get("content-length", "0") or 0)
        except ValueError as exc:
            raise DamApiError("DAM_DOWNLOAD_FAILED", "DAM 下载响应大小无效") from exc
        if content_length > max_bytes:
            raise DamApiError("DAM_ASSET_TOO_LARGE", "DAM 素材超过发布台允许的最大文件大小", 413)
    except DamApiError:
        await client.aclose()
        raise
    except httpx.HTTPError as exc:
        await client.aclose()
        raise DamApiError("DAM_DOWNLOAD_FAILED", "无法从 DAM 下载素材") from exc

    async def chunks() -> AsyncIterator[bytes]:
        received = 0
        try:
            async for chunk in response.aiter_bytes(1024 * 1024):
                received += len(chunk)
                if received > max_bytes:
                    raise DamApiError("DAM_ASSET_TOO_LARGE", "DAM 素材超过发布台允许的最大文件大小", 413)
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    return response, chunks()
