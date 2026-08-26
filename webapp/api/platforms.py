# -*- coding: utf-8 -*-
"""
webapp.api.platforms 模块

平台适配层：将 Web 层的请求转换为 uploader 调用。

职责：
- 解析账号 Cookie 文件路径（cookies/<platform>_<account>.json）
- 提供 login/check/upload 三类平台函数，统一通过 session_pool 租借会话
- 京东与天猫的视频、图文上传请求分别由独立 DTO 描述
- 上传完成后收紧 Cookie 文件权限为 0600
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from uploader.jd_video_uploader.main import (
    JDVideo,
    cookie_auth as jd_cookie_auth,
    jd_setup,
)
from uploader.jd_article_uploader.main import JDArticle
from uploader.douyin_uploader.main import (
    DOUYIN_PUBLISH_STRATEGY_IMMEDIATE,
    DOUYIN_PUBLISH_STRATEGY_SCHEDULED,
    DouYinNote,
    DouYinVideo,
    cookie_auth as douyin_cookie_auth,
    douyin_setup,
)
from uploader.tmall_video_uploader.main import (
    TMALL_PUBLISH_STRATEGY_IMMEDIATE,
    TMALL_PUBLISH_STRATEGY_SCHEDULED,
    TmallVideo,
    cookie_auth as tmall_cookie_auth,
    tmall_setup,
)
from uploader.tmall_article_uploader.main import TmallArticle
from uploader.xiaohongshu_uploader.main import (
    XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
    XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED,
    XiaoHongShuNote,
    XiaoHongShuVideo,
    cookie_auth as xiaohongshu_cookie_auth,
    xiaohongshu_setup,
)
from webapp.workspaces.paths import UserDataPaths

if TYPE_CHECKING:
    from uploader.douyin_session import DouyinSessionPool
    from uploader.jd_session import JdSessionPool
    from uploader.tmall_session import TmallSessionPool
    from uploader.xiaohongshu_session import XiaohongshuSessionPool


@dataclass(slots=True)
class TmallVideoUploadRequest:
    """天猫视频上传请求（Web 层与 uploader 之间的 DTO）。"""

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
class TmallArticleUploadRequest:
    """天猫图文上传请求（图片顺序即平台发布顺序）。"""

    account_name: str
    image_files: tuple[Path, ...]
    title: str
    description: str
    tags: list[str]
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
    """京东视频上传请求（Web 层与 uploader 之间的 DTO）。"""

    account_name: str
    video_file: Path
    title: str
    cover_image_file: Path | None = None
    goods_id: str = ""
    topic: str = ""
    schedule: datetime | None = None
    original: bool = False
    creator_declaration: str = ""
    debug: bool = True
    headless: bool = True
    dry_run: bool = False


@dataclass(slots=True)
class JdArticleUploadRequest:
    """京东图文上传请求，和视频 DTO 独立维护。"""

    account_name: str
    image_files: tuple[Path, ...]
    title: str
    description: str
    goods_id: str = ""
    topic: str = ""
    schedule: datetime | None = None
    original: bool = False
    creator_declaration: str = ""
    debug: bool = True
    headless: bool = True
    dry_run: bool = False


@dataclass(slots=True)
class XiaohongshuVideoUploadRequest:
    """小红书视频上传请求，独立于电商平台 DTO。"""

    account_name: str
    video_file: Path
    title: str
    description: str
    tags: list[str]
    cover_image_file: Path | None = None
    schedule: datetime | None = None
    publish_strategy: str = XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE
    debug: bool = True
    headless: bool = True
    dry_run: bool = False


@dataclass(slots=True)
class XiaohongshuArticleUploadRequest:
    """小红书图文上传请求。"""

    account_name: str
    image_files: tuple[Path, ...]
    title: str
    description: str
    tags: list[str]
    schedule: datetime | None = None
    publish_strategy: str = XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE
    debug: bool = True
    headless: bool = True
    dry_run: bool = False


@dataclass(slots=True)
class DouyinVideoUploadRequest:
    """抖音视频上传请求，按参考上传器字段保持内聚。"""

    account_name: str
    video_file: Path
    title: str
    description: str
    tags: list[str]
    cover_image_file: Path | None = None
    schedule: datetime | None = None
    publish_strategy: str = DOUYIN_PUBLISH_STRATEGY_IMMEDIATE
    debug: bool = True
    headless: bool = True
    dry_run: bool = False


@dataclass(slots=True)
class DouyinArticleUploadRequest:
    """抖音图文上传请求。"""

    account_name: str
    image_files: tuple[Path, ...]
    title: str
    description: str
    tags: list[str]
    schedule: datetime | None = None
    publish_strategy: str = DOUYIN_PUBLISH_STRATEGY_IMMEDIATE
    debug: bool = True
    headless: bool = True
    dry_run: bool = False


def resolve_account_file(
    paths: UserDataPaths, platform: str, account_name: str
) -> Path:
    """Resolve one account state inside its authenticated user's workspace."""
    account_file = paths.cookie_file(platform, account_name)
    secure_account_file(account_file)
    return account_file


def secure_account_file(account_file: Path) -> None:
    """收紧 Cookie 文件权限为 0600（仅当前 OS 用户可读写）。"""
    try:
        account_file.chmod(0o600)
    except FileNotFoundError:
        return


def delete_account_cookie(
    paths: UserDataPaths, platform: str, account_name: str
) -> bool:
    """删除指定平台/账号的 Cookie 文件。

    :returns: True 已删除，False 文件不存在
    """
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
    """天猫账号登录：打开可见浏览器等待用户手动登录。"""
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
    """天猫 Cookie 校验：复用已有会话（preserve_existing_mode）不打断显示模式。"""
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
    """天猫视频发布：校验 Cookie → 上传视频 → 各步骤 → 等待确认。"""
    account_file = resolve_account_file(paths, "tmall", request.account_name)
    uploader = TmallVideo(
        file_path=str(request.video_file),
        cover_image_path=str(request.cover_image_file) if request.cover_image_file else None,
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
            # 发布前校验 Cookie（5 分钟内缓存有效）
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


async def upload_tmall_article(
    request: TmallArticleUploadRequest,
    *,
    paths: UserDataPaths,
    session_pool: TmallSessionPool,
) -> dict:
    """天猫图文发布：复用账号鉴权与会话，上传有序图片后填写发布表单。"""
    account_file = resolve_account_file(paths, "tmall", request.account_name)
    uploader = TmallArticle(
        image_paths=tuple(str(path) for path in request.image_files),
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
        async with session_pool.lease(account_file, headless=request.headless) as session:
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
    """京东账号登录：打开可见浏览器等待用户手动登录。"""
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
    """京东 Cookie 校验。"""
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
    """京东视频发布：校验 Cookie → 上传视频 → 各步骤 → 等待确认。"""
    account_file = resolve_account_file(paths, "jd", request.account_name)
    uploader = JDVideo(
        file_path=str(request.video_file),
        cover_image_path=str(request.cover_image_file) if request.cover_image_file else None,
        title=request.title,
        account_file=str(account_file),
        goods_id=request.goods_id,
        topic=request.topic,
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


async def upload_jd_article(
    request: JdArticleUploadRequest,
    *,
    paths: UserDataPaths,
    session_pool: JdSessionPool,
) -> dict:
    """京东图文发布：独立图文上传器，复用京东账号会话池。"""
    account_file = resolve_account_file(paths, "jd", request.account_name)
    uploader = JDArticle(
        image_paths=tuple(str(path) for path in request.image_files),
        title=request.title,
        description=request.description,
        account_file=str(account_file),
        goods_id=request.goods_id,
        topic=request.topic,
        schedule=request.schedule,
        original=request.original,
        creator_declaration=request.creator_declaration,
        debug=request.debug,
        dry_run=request.dry_run,
    )
    try:
        async with session_pool.lease(account_file, headless=request.headless) as session:
            # 图文和视频共用京东京麦后台，必须使用同一套登录态判定与缓存策略。
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
    """根据是否有定时时间返回发布策略常量。"""
    return TMALL_PUBLISH_STRATEGY_SCHEDULED if schedule else TMALL_PUBLISH_STRATEGY_IMMEDIATE


def xiaohongshu_publish_strategy(schedule: datetime | None) -> str:
    return XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED if schedule else XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE


def douyin_publish_strategy(schedule: datetime | None) -> str:
    return DOUYIN_PUBLISH_STRATEGY_SCHEDULED if schedule else DOUYIN_PUBLISH_STRATEGY_IMMEDIATE


async def login_xiaohongshu_account(
    account_name: str,
    headless: bool = True,
    *,
    paths: UserDataPaths,
    session_pool: XiaohongshuSessionPool,
) -> dict:
    account_file = resolve_account_file(paths, "xiaohongshu", account_name)
    try:
        async with session_pool.lease(account_file, headless=headless) as session:
            return await xiaohongshu_setup(
                str(account_file),
                handle=True,
                return_detail=True,
                headless=headless,
                session=session,
            )
    finally:
        secure_account_file(account_file)


async def check_xiaohongshu_account(
    account_name: str,
    *,
    paths: UserDataPaths,
    session_pool: XiaohongshuSessionPool,
) -> bool:
    account_file = resolve_account_file(paths, "xiaohongshu", account_name)
    if not account_file.exists():
        return False
    async with session_pool.lease(
        account_file,
        headless=True,
        preserve_existing_mode=True,
    ) as session:
        return await xiaohongshu_cookie_auth(str(account_file), session=session)


async def upload_xiaohongshu_video(
    request: XiaohongshuVideoUploadRequest,
    *,
    paths: UserDataPaths,
    session_pool: XiaohongshuSessionPool,
) -> dict:
    account_file = resolve_account_file(paths, "xiaohongshu", request.account_name)
    uploader = XiaoHongShuVideo(
        title=request.title,
        file_path=str(request.video_file),
        tags=request.tags,
        publish_date=request.schedule or 0,
        account_file=str(account_file),
        thumbnail_path=str(request.cover_image_file) if request.cover_image_file else None,
        desc=request.description,
        publish_strategy=request.publish_strategy,
        debug=request.debug,
        headless=request.headless,
        dry_run=request.dry_run,
    )
    try:
        async with session_pool.lease(account_file, headless=request.headless) as session:
            if not await xiaohongshu_setup(
                str(account_file),
                handle=False,
                session=session,
                auth_cache_seconds=5 * 60,
            ):
                raise RuntimeError("小红书 Cookie 不存在或已失效，请先在 Web 页面执行登录")
            result = await uploader.upload_in_session(session)
    finally:
        secure_account_file(account_file)
    return result if isinstance(result, dict) else {}


async def upload_xiaohongshu_article(
    request: XiaohongshuArticleUploadRequest,
    *,
    paths: UserDataPaths,
    session_pool: XiaohongshuSessionPool,
) -> dict:
    account_file = resolve_account_file(paths, "xiaohongshu", request.account_name)
    uploader = XiaoHongShuNote(
        image_paths=tuple(str(path) for path in request.image_files),
        note=request.description,
        tags=request.tags,
        publish_date=request.schedule or 0,
        account_file=str(account_file),
        title=request.title,
        desc=request.description,
        publish_strategy=request.publish_strategy,
        debug=request.debug,
        headless=request.headless,
        dry_run=request.dry_run,
    )
    try:
        async with session_pool.lease(account_file, headless=request.headless) as session:
            if not await xiaohongshu_setup(
                str(account_file),
                handle=False,
                session=session,
                auth_cache_seconds=5 * 60,
            ):
                raise RuntimeError("小红书 Cookie 不存在或已失效，请先在 Web 页面执行登录")
            result = await uploader.upload_in_session(session)
    finally:
        secure_account_file(account_file)
    return result if isinstance(result, dict) else {}


async def login_douyin_account(
    account_name: str,
    headless: bool = True,
    *,
    paths: UserDataPaths,
    session_pool: DouyinSessionPool,
) -> dict:
    account_file = resolve_account_file(paths, "douyin", account_name)
    try:
        async with session_pool.lease(account_file, headless=headless) as session:
            return await douyin_setup(
                str(account_file),
                handle=True,
                return_detail=True,
                headless=headless,
                session=session,
            )
    finally:
        secure_account_file(account_file)


async def check_douyin_account(
    account_name: str,
    *,
    paths: UserDataPaths,
    session_pool: DouyinSessionPool,
) -> bool:
    account_file = resolve_account_file(paths, "douyin", account_name)
    if not account_file.exists():
        return False
    async with session_pool.lease(
        account_file,
        headless=True,
        preserve_existing_mode=True,
    ) as session:
        return await douyin_cookie_auth(str(account_file), session=session)


async def upload_douyin_video(
    request: DouyinVideoUploadRequest,
    *,
    paths: UserDataPaths,
    session_pool: DouyinSessionPool,
) -> dict:
    account_file = resolve_account_file(paths, "douyin", request.account_name)
    uploader = DouYinVideo(
        title=request.title,
        file_path=str(request.video_file),
        tags=request.tags,
        publish_date=request.schedule or 0,
        account_file=str(account_file),
        thumbnail_landscape_path=(
            str(request.cover_image_file) if request.cover_image_file else None
        ),
        desc=request.description,
        publish_strategy=request.publish_strategy,
        debug=request.debug,
        headless=request.headless,
        dry_run=request.dry_run,
    )
    try:
        async with session_pool.lease(account_file, headless=request.headless) as session:
            if not await douyin_setup(
                str(account_file),
                handle=False,
                session=session,
                auth_cache_seconds=5 * 60,
            ):
                raise RuntimeError("抖音 Cookie 不存在或已失效，请先在 Web 页面执行登录")
            result = await uploader.upload_in_session(session)
    finally:
        secure_account_file(account_file)
    return result if isinstance(result, dict) else {}


async def upload_douyin_article(
    request: DouyinArticleUploadRequest,
    *,
    paths: UserDataPaths,
    session_pool: DouyinSessionPool,
) -> dict:
    account_file = resolve_account_file(paths, "douyin", request.account_name)
    uploader = DouYinNote(
        image_paths=tuple(str(path) for path in request.image_files),
        note=request.description,
        tags=request.tags,
        publish_date=request.schedule or 0,
        account_file=str(account_file),
        title=request.title,
        publish_strategy=request.publish_strategy,
        debug=request.debug,
        headless=request.headless,
        dry_run=request.dry_run,
    )
    try:
        async with session_pool.lease(account_file, headless=request.headless) as session:
            if not await douyin_setup(
                str(account_file),
                handle=False,
                session=session,
                auth_cache_seconds=5 * 60,
            ):
                raise RuntimeError("抖音 Cookie 不存在或已失效，请先在 Web 页面执行登录")
            result = await uploader.upload_in_session(session)
    finally:
        secure_account_file(account_file)
    return result if isinstance(result, dict) else {}
