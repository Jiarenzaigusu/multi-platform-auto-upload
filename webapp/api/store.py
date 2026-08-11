from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)


TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "uncertain"}
JOB_STATUSES = TERMINAL_STATUSES | {"queued", "running", "cancelling"}
JOB_KINDS = {"login", "check", "publish", "delete_account", "inspect_product"}
PLATFORMS = {"tmall", "jd"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


_DEFAULT_STATE: dict[str, Any] = {"accounts": [], "jobs": {}}


class JobStore:
    """Small JSON-backed store for a single-machine publishing console."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.path = data_dir / "state.json"
        self._lock = threading.RLock()
        self.media_lock = threading.RLock()
        self._state: dict[str, Any] = deepcopy(_DEFAULT_STATE)
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.data_dir.chmod(0o700)
        if not self.path.exists():
            self._write(_DEFAULT_STATE)
        else:
            self.path.chmod(0o600)
            self._recover_if_corrupt()
        self._state = self._read_disk()

    @staticmethod
    def _validate_state(state: object) -> dict[str, Any]:
        if not isinstance(state, dict):
            raise ValueError("根节点不是对象")
        accounts = state.get("accounts", [])
        jobs = state.get("jobs", {})
        if not isinstance(accounts, list) or not isinstance(jobs, dict):
            raise ValueError("accounts 或 jobs 结构无效")

        for index, account in enumerate(accounts):
            if not isinstance(account, dict):
                raise ValueError(f"accounts[{index}] 不是对象")
            if account.get("platform") not in PLATFORMS:
                raise ValueError(f"accounts[{index}].platform 无效")
            if not isinstance(account.get("account"), str) or not account["account"]:
                raise ValueError(f"accounts[{index}].account 无效")
            if not isinstance(account.get("updated_at"), str):
                raise ValueError(f"accounts[{index}].updated_at 无效")

        for job_id, job in jobs.items():
            if not isinstance(job_id, str) or not isinstance(job, dict):
                raise ValueError("jobs 中的任务键和值必须是对象")
            if job.get("id") != job_id:
                raise ValueError(f"任务 {job_id} 的 id 不匹配")
            if job.get("kind") not in JOB_KINDS:
                raise ValueError(f"任务 {job_id} 的 kind 无效")
            if job.get("platform") not in PLATFORMS:
                raise ValueError(f"任务 {job_id} 的 platform 无效")
            if not isinstance(job.get("account"), str) or not job["account"]:
                raise ValueError(f"任务 {job_id} 的 account 无效")
            if not isinstance(job.get("payload"), dict):
                raise ValueError(f"任务 {job_id} 的 payload 无效")
            if job.get("status") not in JOB_STATUSES:
                raise ValueError(f"任务 {job_id} 的 status 无效")
            if not isinstance(job.get("message"), str) or not isinstance(job.get("error"), str):
                raise ValueError(f"任务 {job_id} 的消息字段无效")
            if not isinstance(job.get("result"), dict):
                raise ValueError(f"任务 {job_id} 的 result 无效")
            if not isinstance(job.get("created_at"), str):
                raise ValueError(f"任务 {job_id} 的 created_at 无效")
            for field in ("started_at", "finished_at"):
                if job.get(field) is not None and not isinstance(job[field], str):
                    raise ValueError(f"任务 {job_id} 的 {field} 无效")
        normalized = dict(state)
        normalized["accounts"] = accounts
        normalized["jobs"] = jobs
        return normalized

    def _recover_if_corrupt(self) -> None:
        """Back up an empty or unreadable state file and recreate defaults.

        A user editing state.json in a text editor may save an empty or partial
        file; we do not want a single accidental edit to keep the console from
        starting. The original bytes are preserved next to state.json so the
        operator can still inspect what was lost.
        """
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            _LOGGER.warning("无法读取 %s，将以默认状态启动：%s", self.path, exc)
            self._write(_DEFAULT_STATE)
            return
        stripped = raw.strip()
        if stripped:
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError as exc:
                self._quarantine_corrupt_state(
                    "state.json 不是合法 JSON，已自动备份并使用默认状态重置",
                    exc,
                )
                return
            try:
                self._validate_state(parsed)
            except ValueError as exc:
                self._quarantine_corrupt_state(
                    "state.json 数据结构无效，已自动备份并使用默认状态重置",
                    exc,
                )
                return
            return
        # Empty file: treat as missing but keep a copy for forensics.
        self._quarantine_corrupt_state(
            "state.json 是空文件，已自动备份并使用默认状态重置",
            None,
        )

    def _quarantine_corrupt_state(
        self, message: str, decode_error: Exception | None
    ) -> None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_name = f"state.json.corrupt-{timestamp}"
        backup_path = self.data_dir / backup_name
        suffix = 1
        while backup_path.exists():
            backup_path = self.data_dir / f"{backup_name}-{suffix}"
            suffix += 1
        try:
            os.replace(self.path, backup_path)
            backup_path.chmod(0o600)
        except OSError as exc:
            _LOGGER.warning(
                "%s。备份失败：%s，将直接覆盖原文件", message, exc
            )
            try:
                self.path.unlink()
            except OSError:
                pass
        else:
            detail = f"：{decode_error}" if decode_error is not None else ""
            _LOGGER.warning("%s。原文件已备份到 %s%s", message, backup_path.name, detail)
        self._write(_DEFAULT_STATE)

    def _read_disk(self) -> dict[str, Any]:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"无法读取 Web 任务状态：{exc}") from exc
        try:
            return self._validate_state(state)
        except ValueError as exc:
            raise RuntimeError(f"无法读取 Web 任务状态：{exc}") from exc

    def _read(self) -> dict[str, Any]:
        return deepcopy(self._state)

    def _write(self, state: dict[str, Any]) -> None:
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.data_dir, delete=False
            ) as file_obj:
                temp_path = Path(file_obj.name)
                json.dump(state, file_obj, ensure_ascii=False, indent=2, sort_keys=True)
                file_obj.flush()
                os.fsync(file_obj.fileno())
            temp_path.chmod(0o600)
            temp_path.replace(self.path)
            self._state = deepcopy(state)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def list_accounts(self) -> list[dict[str, Any]]:
        with self._lock:
            accounts = self._read()["accounts"]
            return sorted(accounts, key=lambda item: (item["platform"], item["account"]))

    def remember_account(self, platform: str, account: str) -> None:
        with self._lock:
            state = self._read()
            existing = next(
                (
                    item
                    for item in state["accounts"]
                    if item["platform"] == platform and item["account"] == account
                ),
                None,
            )
            if existing:
                existing["updated_at"] = utc_now()
            else:
                state["accounts"].append(
                    {"platform": platform, "account": account, "updated_at": utc_now()}
                )
            self._write(state)

    def delete_account(self, platform: str, account: str) -> dict[str, Any]:
        """Forget an account only when no browser task still depends on its Cookie."""
        with self._lock:
            state = self._read()
            account_entry = next(
                (
                    item
                    for item in state["accounts"]
                    if item["platform"] == platform and item["account"] == account
                ),
                None,
            )
            if not account_entry:
                raise KeyError(f"账号不存在：{account}")

            has_active_job = any(
                job["platform"] == platform
                and job["account"] == account
                and job["status"] not in TERMINAL_STATUSES
                for job in state["jobs"].values()
            )
            if has_active_job:
                raise ValueError("该店铺仍有排队中或执行中的任务，不能删除 Cookie")

            state["accounts"] = [
                item
                for item in state["accounts"]
                if not (item["platform"] == platform and item["account"] == account)
            ]
            self._write(state)
            return dict(account_entry)

    def create_job(
        self,
        *,
        kind: str,
        platform: str,
        account: str,
        payload: dict[str, Any],
        batch_id: str | None = None,
        source_row: int | None = None,
        message: str | None = None,
        remember_account: bool = True,
    ) -> dict[str, Any]:
        return self.create_jobs(
            [
                {
                    "kind": kind,
                    "platform": platform,
                    "account": account,
                    "payload": payload,
                    "batch_id": batch_id,
                    "source_row": source_row,
                    "message": message,
                    "remember_account": remember_account,
                }
            ]
        )[0]

    def create_jobs(self, definitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Persist a validated batch in one atomic state-file replacement."""
        jobs = []
        created_at = utc_now()
        accounts_to_remember = {
            (definition["platform"], definition["account"])
            for definition in definitions
            if definition.get("remember_account", True)
        }
        for definition in definitions:
            jobs.append(
                {
                    "id": uuid.uuid4().hex,
                    "kind": definition["kind"],
                    "platform": definition["platform"],
                    "account": definition["account"],
                    "payload": definition["payload"],
                    "status": "queued",
                    "message": definition.get("message")
                    or "任务已进入队列，等待此店铺的前序任务完成",
                    "error": "",
                    "result": {},
                    "batch_id": definition.get("batch_id"),
                    "source_row": definition.get("source_row"),
                    "created_at": created_at,
                    "started_at": None,
                    "finished_at": None,
                }
            )
        with self._lock:
            state = self._read()
            for platform, account in accounts_to_remember:
                existing = next(
                    (
                        item
                        for item in state["accounts"]
                        if item["platform"] == platform and item["account"] == account
                    ),
                    None,
                )
                if existing:
                    existing["updated_at"] = created_at
                else:
                    state["accounts"].append(
                        {
                            "platform": platform,
                            "account": account,
                            "updated_at": created_at,
                        }
                    )
            for job in jobs:
                state["jobs"][job["id"]] = job
            self._write(state)
        return jobs

    def prune_terminal_jobs(
        self, *, max_count: int = 2000, older_than_days: int = 90
    ) -> list[dict[str, Any]]:
        """Bound ordinary terminal history while retaining uncertain jobs for review."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, older_than_days))
        with self._lock:
            state = self._read()
            candidates = [
                job
                for job in state["jobs"].values()
                if job.get("status") in {"succeeded", "failed", "cancelled"}
            ]
            candidates.sort(
                key=lambda job: job.get("finished_at") or job.get("created_at") or "",
                reverse=True,
            )
            removable_ids = {
                job["id"]
                for index, job in enumerate(candidates)
                if index >= max(0, max_count)
                or self._job_finished_before(job, cutoff)
            }
            if not removable_ids:
                return []
            removed = [state["jobs"].pop(job_id) for job_id in removable_ids]
            self._write(state)
            return [dict(job) for job in removed]

    @staticmethod
    def _job_finished_before(job: dict[str, Any], cutoff: datetime) -> bool:
        raw = job.get("finished_at") or job.get("created_at")
        try:
            value = datetime.fromisoformat(raw) if raw else None
        except (TypeError, ValueError):
            return False
        if value is None:
            return False
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value < cutoff

    def list_jobs(self, limit: int | None = 100, offset: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            jobs = list(self._read()["jobs"].values())
            jobs.sort(key=lambda item: item["created_at"], reverse=True)
            if limit is None:
                return jobs[offset:]
            return jobs[offset : offset + limit]

    def job_summary(self) -> dict[str, Any]:
        with self._lock:
            jobs = list(self._read()["jobs"].values())
        statuses: dict[str, int] = {}
        for job in jobs:
            status = str(job.get("status", "unknown"))
            statuses[status] = statuses.get(status, 0) + 1
        return {"total": len(jobs), "statuses": statuses}

    def list_active_jobs(self, platform: str | None = None, account: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            jobs = list(self._read()["jobs"].values())
        return [
            dict(job)
            for job in jobs
            if job.get("status") not in TERMINAL_STATUSES
            and (platform is None or job.get("platform") == platform)
            and (account is None or job.get("account") == account)
        ]

    def recover_interrupted_jobs(self) -> list[str]:
        """Resume never-started work and terminalize work interrupted mid-browser action."""
        resumable: list[dict[str, Any]] = []
        with self._lock:
            state = self._read()
            changed = False
            for job in state["jobs"].values():
                status = job.get("status")
                if status == "queued":
                    resumable.append(job)
                elif status == "running":
                    interrupted_status = (
                        "uncertain" if job.get("kind") == "publish" else "failed"
                    )
                    job.update(
                        status=interrupted_status,
                        message="服务在任务执行期间中断；平台提交结果可能不确定，请人工核对后再决定是否重试",
                        error="服务进程中断，任务未能确认最终结果",
                        finished_at=utc_now(),
                    )
                    changed = True
                elif status == "cancelling":
                    job.update(
                        status="cancelled",
                        message="服务重启时完成了中断状态收敛",
                        finished_at=utc_now(),
                    )
                    changed = True
            if changed:
                self._write(state)
        resumable.sort(key=lambda job: (job.get("created_at", ""), job["id"]))
        return [job["id"] for job in resumable]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._read()["jobs"].get(job_id)
            return dict(job) if job else None

    def delete_job(self, job_id: str) -> dict[str, Any]:
        """Delete a completed task record without interrupting browser work."""
        with self._lock:
            state = self._read()
            job = state["jobs"].get(job_id)
            if not job:
                raise KeyError(f"任务不存在：{job_id}")
            if job["status"] not in TERMINAL_STATUSES:
                raise ValueError("仅已完成或失败的任务可以删除")
            deleted = state["jobs"].pop(job_id)
            self._write(state)
            return dict(deleted)

    def update_job(self, job_id: str, **changes: Any) -> dict[str, Any]:
        with self._lock:
            state = self._read()
            job = state["jobs"].get(job_id)
            if not job:
                raise KeyError(f"任务不存在：{job_id}")
            job.update(changes)
            self._write(state)
            return dict(job)

    def update_job_volatile(self, job_id: str, **changes: Any) -> dict[str, Any]:
        """Update lease-only state without fsync; the next durable mutation includes it."""
        with self._lock:
            job = self._state["jobs"].get(job_id)
            if not job:
                raise KeyError(f"任务不存在：{job_id}")
            job.update(changes)
            return deepcopy(job)
