from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from local_agent.client import AgentApiClient, AgentApiError
from local_agent.paths import secure_directory


class LocalUploadServer:
    """Receive browser-selected files directly on the paired Windows PC."""

    def __init__(
        self,
        client: AgentApiClient,
        asset_root: Path,
        *,
        port: int = 48765,
    ) -> None:
        self.client = client
        self.asset_root = secure_directory(asset_root)
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._server is not None:
            return
        cutoff = time.time() - 2 * 24 * 60 * 60
        for path in self.asset_root.iterdir():
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "MPAU-Local-Upload/1"

            def log_message(self, _format: str, *_args) -> None:
                return

            def _origin(self) -> str:
                return (self.headers.get("Origin") or "").strip().rstrip("/")

            def _ticket(self) -> str:
                values = parse_qs(urlsplit(self.path).query).get("ticket") or []
                return values[0] if len(values) == 1 else ""

            def _valid_local_host(self) -> bool:
                host = (self.headers.get("Host") or "").split(":", 1)[0].lower()
                return host in {"127.0.0.1", "localhost"}

            @staticmethod
            def _valid_origin(origin: str) -> bool:
                """Only reflect origins that look like a real web page."""
                if not origin:
                    return False
                parsed = urlsplit(origin)
                return parsed.scheme in {"http", "https"} and bool(parsed.hostname)

            def _cors_headers(self, origin: str) -> None:
                # Every response, errors included, must carry CORS headers.
                # A preflight that answers with a non-2xx status *without* them
                # makes the browser raise a TypeError, which the web UI reports
                # as "助手未启动" even while this server is listening.
                if not self._valid_origin(origin):
                    return
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Private-Network", "true")
                self.send_header("Cache-Control", "no-store")

            def _json(self, status: int, body: dict) -> None:
                encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
                try:
                    # A browser that hung up mid-upload can leave the socket in a
                    # state where writing blocks instead of failing. Bound the
                    # write so one dead client cannot pin this worker forever.
                    self.connection.settimeout(5)
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(encoded)))
                    self._cors_headers(self._origin())
                    self.end_headers()
                    self.wfile.write(encoded)
                except (
                    BrokenPipeError,
                    ConnectionAbortedError,
                    ConnectionResetError,
                    TimeoutError,
                ):
                    # Nobody is left to read an answer; drop it quietly instead
                    # of letting the worker thread log a failure.
                    self.close_connection = True

            def do_OPTIONS(self) -> None:
                origin = self._origin()
                if (
                    urlsplit(self.path).path != "/v1/upload"
                    or not self._valid_local_host()
                    or not self._valid_origin(origin)
                    or not self._ticket()
                ):
                    self._json(403, {"detail": "本机上传预检失败"})
                    return
                # Preflight must never depend on the cloud: a transient server
                # error used to answer it with 5xx and no CORS headers, which
                # browsers report as an unreachable agent. The ticket is fully
                # validated by the POST that actually carries the file.
                self.send_response(204)
                self._cors_headers(origin)
                self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Max-Age", "300")
                self.end_headers()

            def do_POST(self) -> None:
                origin = self._origin()
                ticket = self._ticket()
                if (
                    urlsplit(self.path).path != "/v1/upload"
                    or not self._valid_local_host()
                    or not self._valid_origin(origin)
                    or not ticket
                ):
                    self._json(403, {"detail": "本机上传请求无效"})
                    return
                try:
                    authorization = owner.client.authorize_local_upload(
                        ticket, origin, reserve=True
                    )
                except AgentApiError as exc:
                    self._json(exc.status or 502, {"detail": str(exc)})
                    return

                expected_size = int(authorization["size"])
                try:
                    content_length = int(self.headers.get("Content-Length") or "-1")
                except ValueError:
                    content_length = -1
                if content_length != expected_size:
                    self._json(422, {"detail": "上传大小与票据不一致"})
                    return

                asset_id = str(authorization["asset_id"])
                suffix = Path(str(authorization["filename"])).suffix.lower()
                destination = owner.asset_root / f"{asset_id}{suffix}"
                temporary = owner.asset_root / f".{asset_id}.part"
                digest = hashlib.sha256()
                remaining = expected_size
                try:
                    with temporary.open("xb") as output:
                        while remaining:
                            chunk = self.rfile.read(min(1024 * 1024, remaining))
                            if not chunk:
                                raise OSError("浏览器在文件传输完成前断开")
                            output.write(chunk)
                            digest.update(chunk)
                            remaining -= len(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                    try:
                        temporary.chmod(0o600)
                    except OSError:
                        pass
                    temporary.replace(destination)
                    completed = owner.client.complete_local_upload(
                        ticket,
                        origin,
                        asset_id=asset_id,
                        sha256=digest.hexdigest(),
                        size=expected_size,
                    )
                except (AgentApiError, OSError) as exc:
                    temporary.unlink(missing_ok=True)
                    destination.unlink(missing_ok=True)
                    status = exc.status if isinstance(exc, AgentApiError) else 500
                    self._json(status or 502, {"detail": str(exc)})
                    return
                self._json(201, {"asset": completed})

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="mpau-local-upload",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
