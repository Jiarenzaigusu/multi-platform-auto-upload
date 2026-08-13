from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any

from local_agent.paths import secure_directory


@dataclass(frozen=True, slots=True)
class StoredConnection:
    server_url: str
    agent_token: str
    user: dict[str, Any]
    expires_at: str


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _windows_crypto_libraries():
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return crypt32, kernel32


def _dpapi_protect(value: bytes) -> bytes:
    if os.name != "nt":
        return b"plain:" + value
    buffer = ctypes.create_string_buffer(value)
    source = _DataBlob(
        len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    )
    output = _DataBlob()
    crypt32, kernel32 = _windows_crypto_libraries()
    if not crypt32.CryptProtectData(
        ctypes.byref(source),
        "MPAU Agent token",
        None,
        None,
        None,
        0x1,
        ctypes.byref(output),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return b"dpapi:" + ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p))


def _dpapi_unprotect(value: bytes) -> bytes:
    prefix, separator, protected = value.partition(b":")
    if not separator:
        raise ValueError("本地执行助手凭据格式无效")
    if prefix == b"plain" and os.name != "nt":
        return protected
    if prefix != b"dpapi" or os.name != "nt":
        raise ValueError("本地执行助手凭据不能由当前系统解密")
    buffer = ctypes.create_string_buffer(protected)
    source = _DataBlob(
        len(protected), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    )
    output = _DataBlob()
    crypt32, kernel32 = _windows_crypto_libraries()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0x1, ctypes.byref(output)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p))


class AgentConnectionStore:
    """Persist a paired agent token in the OS credential store when available."""

    def __init__(self, data_root: Path) -> None:
        self.data_root = secure_directory(data_root)
        self.path = self.data_root / "connection.json"

    @property
    def _macos_keychain_account(self) -> str:
        """Give every local-agent data root an isolated Keychain record."""
        import hashlib

        digest = hashlib.sha256(str(self.data_root).encode("utf-8")).hexdigest()
        return f"agent-{digest[:24]}"

    def _protect_token(self, token: str) -> bytes:
        if sys.platform != "darwin":
            return _dpapi_protect(token.encode("utf-8"))
        account = self._macos_keychain_account
        completed = subprocess.run(
            [
                "security",
                "add-generic-password",
                "-a",
                account,
                "-s",
                "MPAU Agent",
                "-w",
                token,
                "-U",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise ValueError("无法将本地执行助手凭据保存到 macOS 钥匙串")
        return f"keychain:{account}".encode("utf-8")

    def _unprotect_token(self, value: bytes) -> bytes:
        if not value.startswith(b"keychain:"):
            return _dpapi_unprotect(value)
        if sys.platform != "darwin":
            raise ValueError("本地执行助手凭据不能由当前系统解密")
        account = value.removeprefix(b"keychain:").decode("utf-8")
        completed = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-a",
                account,
                "-s",
                "MPAU Agent",
                "-w",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise ValueError("无法从 macOS 钥匙串读取本地执行助手凭据")
        return completed.stdout.rstrip("\n").encode("utf-8")

    def save(
        self,
        *,
        server_url: str,
        agent_token: str,
        user: dict[str, Any],
        expires_at: str,
    ) -> None:
        document = {
            "version": 1,
            "server_url": server_url.rstrip("/"),
            "protected_token": base64.b64encode(
                self._protect_token(agent_token)
            ).decode("ascii"),
            "user": {
                key: user.get(key, "")
                for key in ("id", "username", "display_name", "role")
            },
            "expires_at": expires_at,
        }
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".connection.", suffix=".tmp", dir=self.data_root
        )
        temporary = Path(temporary_name)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(document, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        finally:
            temporary.unlink(missing_ok=True)

    def load(self) -> StoredConnection | None:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
            protected = base64.b64decode(document["protected_token"], validate=True)
            token = self._unprotect_token(protected).decode("utf-8")
            server_url = str(document["server_url"]).strip().rstrip("/")
            user = document["user"]
            expires_at = str(document["expires_at"])
            if not server_url or not token or not isinstance(user, dict):
                raise ValueError("本地执行助手连接信息不完整")
            return StoredConnection(server_url, token, user, expires_at)
        except FileNotFoundError:
            return None
        except (KeyError, OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"无法读取本地执行助手连接信息：{exc}") from exc

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
        if sys.platform == "darwin":
            subprocess.run(
                [
                    "security",
                    "delete-generic-password",
                    "-a",
                    self._macos_keychain_account,
                    "-s",
                    "MPAU Agent",
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
