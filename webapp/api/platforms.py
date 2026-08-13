from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from uploader.jd_uploader.main import (
    JDVideo,
    cookie_auth as jd_cookie_auth,
    jd_setup,
)
from uploader.tmall_uploader.main import (
    TMALL_PUBLISH_STRATEGY_IMMEDIATE,
    TMALL_PUBLISH_STRATEGY_SCHEDULED,
    TmallVideo,
    cookie_auth as tmall_cookie_auth,
    tmall_setup,
)
from webapp.workspaces.paths import UserDataPaths

if TYPE_CHECKING:
    from uploader.jd_uploader.session import JdSessionPool
    from uploader.tmall_uploader.session import TmallSessionPool


@dataclass(slots=True)
class TmallVideoUploadRequest:
    account_name: str
    video_file: Path
    title: str
    description: str
    tags: list[str]
    cover_image_file: Path | None = None
    goods_id: str = ""
    activity_topic: str = ""
    music_name: str = ""
    creator_declaration: str = ""
    schedule: datetime | None = None
    publish_strategy: str = TMALL_PUBLISH_STRATEGY_IMMEDIATE
    debug: bool = True
    headless: bool = True
    dry_run: bool = False


@dataclass(slots=True)
class JdVideoUploadRequest:
    account_name: str
    video_file: Path
    title: str
    goods_id: str = ""
    schedule: datetime | None = None
    original: bool = False
    creator_declaration: str = ""
    debug: bool = True
    headless: bool = True
    dry_run: bool = False


def resolve_account_file(paths: UserDataPaths, platform: str, account_name: str) -> Path:
    """Resolve one account state inside its authenticated user's workspace."""
    account_file = paths.cookie_file(platform, account_name)
    account_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    account_file.parent.chmod(0o700)
    secure_account_file(account_file)
    return account_file


def secure_account_file(account_file: Path) -> None:
    """Keep browser storage state readable only by the current OS user."""
    try:
        account_file.chmod(0o600)
    except FileNotFoundError:
        return


def delete_account_cookie(
    paths: UserDataPaths, platform: str, account_name: str
) -> bool:
    """Remove only the selected platform/account cookie file when it exists."""
    account_file = resolve_account_file(paths, platform, account_name)
    try:
        account_file.unlink()
    except FileNotFoundError:
        return False
    return True


async def login_tmall_account(
    account_name: str,
    headless: bool = True,
    *,
    paths: UserDataPaths,
    session_pool: TmallSessionPool,
) -> dict:
    account_file = resolve_account_file(paths, "tmall", account_name)
    try:
        async with session_pool.lease(
            account_file,
            headless=headless,
        ) as session:
            return await tmall_setup(
                str(account_file),
                handle=True,
                return_detail=True,
                session=session,
            )
    finally:
        secure_account_file(account_file)


async def check_tmall_account(
    account_name: str,
    *,
    paths: UserDataPaths,
    session_pool: TmallSessionPool,
) -> bool:
    account_file = resolve_account_file(paths, "tmall", account_name)
    if not account_file.exists():
        return False
    async with session_pool.lease(
        account_file,
        headless=True,
        preserve_existing_mode=True,
    ) as session:
        return await tmall_cookie_auth(str(account_file), session=session)


async def upload_tmall_video(
    request: TmallVideoUploadRequest,
    *,
    paths: UserDataPaths,
    session_pool: TmallSessionPool,
) -> dict:
    account_file = resolve_account_file(paths, "tmall", request.account_name)
    uploader = TmallVideo(
        file_path=str(request.video_file),
        cover_image_path=(
            str(request.cover_image_file) if request.cover_image_file else None
        ),
        title=request.title,
        desc=request.description,
        account_file=str(account_file),
        tags=request.tags,
        goods_id=request.goods_id,
        activity_topic=request.activity_topic,
        music_name=request.music_name,
        creator_declaration=request.creator_declaration,
        schedule=request.schedule,
        publish_strategy=request.publish_strategy,
        debug=request.debug,
        dry_run=request.dry_run,
    )
    try:
        async with session_pool.lease(
            account_file,
            headless=request.headless,
        ) as session:
            if not await tmall_setup(
                str(account_file),
                handle=False,
                session=session,
                auth_cache_seconds=5 * 60,
            ):
                raise RuntimeError("天猫 Cookie 不存在或已失效，请先在 Web 页面执行登录")
            result = await uploader.upload_in_session(session)
    finally:
        secure_account_file(account_file)
    return result if isinstance(result, dict) else {}


async def login_jd_account(
    account_name: str,
    headless: bool = True,
    *,
    paths: UserDataPaths,
    session_pool: JdSessionPool,
) -> dict:
    account_file = resolve_account_file(paths, "jd", account_name)
    try:
        async with session_pool.lease(
            account_file,
            headless=headless,
        ) as session:
            return await jd_setup(
                str(account_file),
                handle=True,
                return_detail=True,
                session=session,
            )
    finally:
        secure_account_file(account_file)


async def check_jd_account(
    account_name: str,
    *,
    paths: UserDataPaths,
    session_pool: JdSessionPool,
) -> bool:
    account_file = resolve_account_file(paths, "jd", account_name)
    if not account_file.exists():
        return False
    async with session_pool.lease(
        account_file,
        headless=True,
        preserve_existing_mode=True,
    ) as session:
        return await jd_cookie_auth(str(account_file), session=session)


async def upload_jd_video(
    request: JdVideoUploadRequest,
    *,
    paths: UserDataPaths,
    session_pool: JdSessionPool,
) -> dict:
    account_file = resolve_account_file(paths, "jd", request.account_name)
    uploader = JDVideo(
        file_path=str(request.video_file),
        title=request.title,
        account_file=str(account_file),
        goods_id=request.goods_id,
        schedule=request.schedule,
        original=request.original,
        creator_declaration=request.creator_declaration,
        debug=request.debug,
        dry_run=request.dry_run,
    )
    try:
        async with session_pool.lease(
            account_file,
            headless=request.headless,
        ) as session:
            if not await jd_setup(
                str(account_file),
                handle=False,
                session=session,
                auth_cache_seconds=5 * 60,
            ):
                raise RuntimeError("京东 Cookie 不存在或已失效，请先在 Web 页面执行登录")
            result = await uploader.upload_in_session(session)
    finally:
        secure_account_file(account_file)
    return result if isinstance(result, dict) else {}


def tmall_publish_strategy(schedule: datetime | None) -> str:
    return TMALL_PUBLISH_STRATEGY_SCHEDULED if schedule else TMALL_PUBLISH_STRATEGY_IMMEDIATE
