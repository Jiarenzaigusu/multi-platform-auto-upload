from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import tempfile
from typing import Protocol

from webapp.llm_adapter.catalog import AdapterKind
from webapp.llm_adapter.errors import AdapterStorageError


@dataclass(frozen=True, slots=True)
class StoredAdapterCredential:
    api_key: str
    base_url: str | None = None


@dataclass(frozen=True, slots=True)
class StoredAdapterState:
    credentials: dict[AdapterKind, StoredAdapterCredential] = field(
        default_factory=dict
    )
    active_provider: AdapterKind | None = None


class AdapterCredentialStore(Protocol):
    def load(self) -> StoredAdapterState: ...

    def save(self, state: StoredAdapterState) -> None: ...


class FileAdapterCredentialStore:
    """Persist adapter secrets in a user-only local runtime file."""

    VERSION = 1

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> StoredAdapterState:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return StoredAdapterState()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdapterStorageError("无法读取本地 LLM API Key 配置") from exc

        try:
            if not isinstance(raw, dict) or raw.get("version") != self.VERSION:
                raise ValueError("unsupported credential file")
            raw_credentials = raw.get("credentials", {})
            if not isinstance(raw_credentials, dict):
                raise ValueError("invalid credentials")

            credentials: dict[AdapterKind, StoredAdapterCredential] = {}
            for raw_provider, value in raw_credentials.items():
                provider = AdapterKind(raw_provider)
                if not isinstance(value, dict):
                    raise ValueError("invalid credential")
                api_key = value.get("api_key")
                base_url = value.get("base_url")
                if not isinstance(api_key, str) or len(api_key) < 8:
                    raise ValueError("invalid api key")
                if base_url is not None and not isinstance(base_url, str):
                    raise ValueError("invalid base url")
                credentials[provider] = StoredAdapterCredential(api_key, base_url)

            raw_active = raw.get("active_provider")
            active_provider = AdapterKind(raw_active) if raw_active else None
            if active_provider not in credentials:
                active_provider = None
            return StoredAdapterState(credentials, active_provider)
        except (TypeError, ValueError) as exc:
            raise AdapterStorageError("本地 LLM API Key 配置格式无效") from exc

    def save(self, state: StoredAdapterState) -> None:
        document = {
            "version": self.VERSION,
            "active_provider": (
                state.active_provider.value if state.active_provider else None
            ),
            "credentials": {
                provider.value: {
                    "api_key": credential.api_key,
                    "base_url": credential.base_url,
                }
                for provider, credential in state.credentials.items()
            },
        }
        temporary_path: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.path.parent.chmod(0o700)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
            )
            temporary_path = Path(temporary_name)
            os.chmod(temporary_path, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(document, file, ensure_ascii=False, separators=(",", ":"))
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, self.path)
            self.path.chmod(0o600)
        except OSError as exc:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except OSError:
                    pass
            raise AdapterStorageError("无法保存本地 LLM API Key 配置") from exc


__all__ = [
    "AdapterCredentialStore",
    "FileAdapterCredentialStore",
    "StoredAdapterCredential",
    "StoredAdapterState",
]
