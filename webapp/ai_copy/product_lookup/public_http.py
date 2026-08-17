"""webapp.ai_copy.product_lookup.public_http 模块：公开网页 HTTP 客户端基础设施。

提供：
- create_trusted_ssl_context(): 基于 certifi CA 包创建 SSL 上下文
- validate_public_product_url(): SSRF 防护（DNS 解析校验，禁止内网/保留地址）
- PinnedHTTPConnection/PinnedHTTPSConnection: DNS pinning（连接到指定 IP 避免 TOCTOU）
- PublicPageHttpClient: 核心客户端，支持多 IP 尝试 + 重定向跟踪（最多 5 次）

每次重定向都重新校验 URL 公开性，避免重定向到内网地址。
"""
from __future__ import annotations

from dataclasses import dataclass
import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urljoin, urlsplit, urlunsplit

import certifi

from webapp.ai_copy.errors import ProductLookupError


DEFAULT_PAGE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 Chrome/136 Safari/537.36"
    ),
}


def create_trusted_ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=certifi.where())


def validate_public_product_url(url: str) -> list[str]:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ProductLookupError("商品链接必须是公开的 HTTP(S) 地址")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, port)}
    except (socket.gaierror, ValueError) as exc:
        raise ProductLookupError("无法解析商品链接的域名") from exc
    if not addresses:
        raise ProductLookupError("商品链接没有可用的网络地址")
    for address in addresses:
        try:
            if not ipaddress.ip_address(address).is_global:
                raise ProductLookupError("商品链接不能指向本机、内网或保留地址")
        except ValueError as exc:
            raise ProductLookupError("商品链接解析到了无效网络地址") from exc
    return sorted(addresses)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, pinned_ip: str, timeout: float):
        self._pinned_ip = pinned_ip
        super().__init__(host, port=port, timeout=timeout)

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout, self.source_address
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        host: str,
        port: int,
        pinned_ip: str,
        timeout: float,
        ssl_context: ssl.SSLContext,
    ) -> None:
        self._pinned_ip = pinned_ip
        super().__init__(host, port=port, timeout=timeout, context=ssl_context)

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout, self.source_address
        )
        self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)


@dataclass(frozen=True, slots=True)
class FetchedPage:
    content: bytes
    content_type: str
    charset: str
    final_url: str


class PublicPageHttpClient:
    """Fetches public pages with DNS pinning and a trusted CA bundle."""

    def __init__(
        self,
        *,
        timeout_seconds: float,
        max_bytes: int,
        ssl_context: ssl.SSLContext | None = None,
        max_redirects: int = 5,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._max_bytes = max_bytes
        self._ssl_context = ssl_context or create_trusted_ssl_context()
        self._max_redirects = max_redirects

    def get(
        self, product_url: str, *, headers: dict[str, str] | None = None
    ) -> FetchedPage:
        current_url = product_url
        request_headers = {**DEFAULT_PAGE_HEADERS, **(headers or {})}

        for redirect_count in range(self._max_redirects + 1):
            parsed = urlsplit(current_url)
            addresses = validate_public_product_url(current_url)
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            last_error: Exception | None = None
            redirect_url: str | None = None

            for address in addresses:
                if parsed.scheme == "https":
                    connection = _PinnedHTTPSConnection(
                        parsed.hostname or "",
                        port,
                        address,
                        self._timeout_seconds,
                        self._ssl_context,
                    )
                else:
                    connection = _PinnedHTTPConnection(
                        parsed.hostname or "", port, address, self._timeout_seconds
                    )
                try:
                    connection.request("GET", target, headers=request_headers)
                    response = connection.getresponse()
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        response.read(1024)
                        if not location:
                            raise ProductLookupError(
                                "商品页面返回了没有目标地址的重定向"
                            )
                        if redirect_count >= self._max_redirects:
                            raise ProductLookupError("商品页面重定向次数过多")
                        redirect_url = urljoin(current_url, location)
                        break
                    if response.status >= 400:
                        raise ProductLookupError(
                            f"商品页面返回 HTTP {response.status}"
                        )
                    content_type = response.headers.get_content_type()
                    charset = response.headers.get_content_charset() or "utf-8"
                    raw = response.read(self._max_bytes + 1)
                    if len(raw) > self._max_bytes:
                        raise ProductLookupError("商品页面超过读取大小限制")
                    return FetchedPage(raw, content_type, charset, current_url)
                except ProductLookupError:
                    raise
                except (
                    OSError,
                    TimeoutError,
                    ssl.SSLError,
                    http.client.HTTPException,
                ) as exc:
                    last_error = exc
                finally:
                    connection.close()

            if redirect_url is not None:
                current_url = redirect_url
                continue
            raise ProductLookupError(f"无法读取商品页面：{last_error}") from last_error

        raise ProductLookupError("商品页面重定向次数过多")
