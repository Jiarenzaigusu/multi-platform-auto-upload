# -*- coding: utf-8 -*-
"""Batch workbook adapters for paths that exist only on a paired desktop agent.

The pure-local parsers remain the canonical implementation and validate files on
the machine that reads the workbook.  In the server-plus-agent deployment the
workbook is uploaded to the server, while those absolute paths belong to the
user's Windows computer.  This module preserves the same workbook fields and
metadata validation, then defers file-system checks and image-folder expansion
to that paired computer immediately before browser automation starts.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory

from webapp.api.batch import (
    BatchPublishRow,
    BatchRowError,
    BatchValidationError,
    find_header_row,
    open_batch_workbook,
    resolve_local_path,
    row_value,
)
from webapp.api.batch_douyin_article import (
    DOUYIN_ARTICLE_BATCH_COLUMNS,
    DOUYIN_ARTICLE_COLUMN_ALIASES,
)
from webapp.api.batch_douyin_video import (
    DOUYIN_VIDEO_BATCH_COLUMNS,
    DOUYIN_VIDEO_COLUMN_ALIASES,
)
from webapp.api.batch_jd_video import (
    JD_VIDEO_BATCH_COLUMNS,
    JD_VIDEO_COLUMN_ALIASES,
    _parse_original,
)
from webapp.api.batch_jd_article import (
    JD_ARTICLE_BATCH_COLUMNS,
    JD_ARTICLE_COLUMN_ALIASES,
    _parse_original as _parse_jd_article_original,
)
from webapp.api.batch_tmall_article import (
    TMALL_ARTICLE_BATCH_COLUMNS,
    TMALL_ARTICLE_COLUMN_ALIASES,
)
from webapp.api.batch_tmall_video import (
    TMALL_VIDEO_BATCH_COLUMNS,
    TMALL_VIDEO_COLUMN_ALIASES,
)
from webapp.api.batch_xiaohongshu_article import (
    XIAOHONGSHU_ARTICLE_BATCH_COLUMNS,
    XIAOHONGSHU_ARTICLE_COLUMN_ALIASES,
)
from webapp.api.batch_xiaohongshu_video import (
    XIAOHONGSHU_VIDEO_BATCH_COLUMNS,
    XIAOHONGSHU_VIDEO_COLUMN_ALIASES,
)
from webapp.api.models import PublishRequest, validate_publish_request


@dataclass(frozen=True, slots=True)
class AgentBatchPublishRow:
    """Server-only transport metadata for a workbook row executed on an agent."""

    row_number: int
    request: PublishRequest
    image_folder_path: Path | None = None


def _fixture_file(directory: Path, suffix: str, stem: str) -> Path:
    """Create a tiny local file solely for shared metadata validation."""
    path = directory / f"{stem}{suffix.lower()}"
    path.write_bytes(b"agent-batch-validation")
    return path


def _parse_rows(
    content: bytes,
    *,
    platform_label: str,
    template_label: str,
    columns: tuple,
    aliases: dict[str, set[str]],
    account: str,
    dry_run: bool,
    headed: bool,
    max_rows: int,
    request_from_values,
) -> list[AgentBatchPublishRow]:
    workbook = open_batch_workbook(content, platform_label)
    try:
        rows = workbook.active.iter_rows(values_only=True)
        positions, header_row_number = find_header_row(
            rows,
            columns=columns,
            column_aliases=aliases,
            template_label=template_label,
        )
        errors: list[BatchRowError] = []
        parsed_rows: list[AgentBatchPublishRow] = []
        for row_number, values in enumerate(rows, start=header_row_number + 1):
            values_by_field = {
                field: row_value(values, positions, field)
                for field, _label, _required in columns
            }
            if not any(values_by_field.values()):
                continue
            if len(parsed_rows) + len(errors) >= max_rows:
                errors.append(BatchRowError(row_number, "整行", f"单次最多导入 {max_rows} 条内容"))
                break
            try:
                parsed = request_from_values(values_by_field, positions)
            except ValueError as exc:
                errors.append(BatchRowError(row_number, "内容", str(exc)))
                continue
            if isinstance(parsed, tuple):
                request, image_folder_path = parsed
            else:
                request, image_folder_path = parsed, None
            parsed_rows.append(
                AgentBatchPublishRow(
                    row_number=row_number,
                    request=request,
                    image_folder_path=image_folder_path,
                )
            )
        if not parsed_rows and not errors:
            errors.append(BatchRowError(header_row_number + 1, "整行", "至少需要填写一条发布内容"))
        if errors:
            raise BatchValidationError("Excel 内容校验失败，未创建任何发布任务", errors)
        return parsed_rows
    finally:
        workbook.close()


def _remote_video_request(
    values: dict[str, str],
    positions: dict[str, int],
    *,
    platform: str,
    account: str,
    dry_run: bool,
    headed: bool,
) -> PublishRequest:
    video_path = resolve_local_path(values["video_path"], "视频路径")
    cover_path = (
        resolve_local_path(values["cover_image_path"], "自定义封面路径")
        if "cover_image_path" in values and values["cover_image_path"]
        else None
    )
    original = _parse_original(values["original"]) if platform == "jd" else False
    with TemporaryDirectory(prefix="mpau-agent-batch-") as temporary_directory:
        directory = Path(temporary_directory)
        fixture_video = _fixture_file(directory, video_path.suffix, "video")
        fixture_cover = (
            _fixture_file(directory, cover_path.suffix, "cover") if cover_path else None
        )
        request = validate_publish_request(
            platform=platform,
            account=account,
            content_type="video",
            video_path=fixture_video,
            cover_image_path=fixture_cover,
            original_filename=video_path.name,
            title=values["title"],
            description=values.get("description", ""),
            raw_tags=values.get("tags", "").replace("，", ","),
            goods_id=values.get("goods_id", ""),
            activity_topic=values.get("activity_topic", ""),
            raw_music_name=values.get("music_name", ""),
            raw_creator_declaration=(
                values["creator_declaration"]
                if "creator_declaration" in positions
                else "内容无需标注"
                if platform in {"tmall", "jd"}
                else ""
            ),
            raw_schedule=values.get("schedule", ""),
            original=original,
            dry_run=dry_run,
            headed=headed,
        )
    return replace(request, video_path=video_path, cover_image_path=cover_path)


def parse_remote_tmall_video_batch_workbook(
    content: bytes, *, account: str, dry_run: bool, headed: bool, max_rows: int = 200
) -> list[BatchPublishRow]:
    """Parse a Tmall video workbook whose media paths live on the agent PC."""
    return _parse_rows(
        content,
        platform_label="天猫",
        template_label="天猫视频",
        columns=TMALL_VIDEO_BATCH_COLUMNS,
        aliases=TMALL_VIDEO_COLUMN_ALIASES,
        account=account,
        dry_run=dry_run,
        headed=headed,
        max_rows=max_rows,
        request_from_values=lambda values, positions: _remote_video_request(
            values, positions, platform="tmall", account=account, dry_run=dry_run, headed=headed
        ),
    )


def parse_remote_jd_video_batch_workbook(
    content: bytes, *, account: str, dry_run: bool, headed: bool, max_rows: int = 200
) -> list[BatchPublishRow]:
    """Parse a JD video workbook whose media paths live on the agent PC."""
    return _parse_rows(
        content,
        platform_label="京东",
        template_label="京东视频",
        columns=JD_VIDEO_BATCH_COLUMNS,
        aliases=JD_VIDEO_COLUMN_ALIASES,
        account=account,
        dry_run=dry_run,
        headed=headed,
        max_rows=max_rows,
        request_from_values=lambda values, positions: _remote_video_request(
            values, positions, platform="jd", account=account, dry_run=dry_run, headed=headed
        ),
    )


def parse_remote_tmall_article_batch_workbook(
    content: bytes, *, account: str, dry_run: bool, headed: bool, max_rows: int = 200
) -> list[BatchPublishRow]:
    """Parse a Tmall article workbook and defer folder enumeration to the agent."""
    def make_request(
        values: dict[str, str], positions: dict[str, int]
    ) -> tuple[PublishRequest, Path]:
        folder_path = resolve_local_path(values["image_folder_path"], "图片文件夹路径")
        with TemporaryDirectory(prefix="mpau-agent-batch-") as temporary_directory:
            fixture_image = _fixture_file(Path(temporary_directory), ".jpg", "image")
            request = validate_publish_request(
                platform="tmall",
                account=account,
                content_type="article",
                image_paths=(fixture_image,),
                original_filename=folder_path.name or "article.jpg",
                title=values["title"],
                description=values["description"],
                raw_tags=values["tags"].replace("，", ","),
                goods_id=values["goods_id"],
                activity_topic=values["activity_topic"],
                raw_music_name=values["music_name"],
                raw_creator_declaration=(
                    values["creator_declaration"]
                    if "creator_declaration" in positions
                    else "内容无需标注"
                ),
                raw_schedule=values["schedule"],
                dry_run=dry_run,
                headed=headed,
            )
        return replace(request, image_paths=()), folder_path

    return _parse_rows(
        content,
        platform_label="天猫",
        template_label="天猫图文",
        columns=TMALL_ARTICLE_BATCH_COLUMNS,
        aliases=TMALL_ARTICLE_COLUMN_ALIASES,
        account=account,
        dry_run=dry_run,
        headed=headed,
        max_rows=max_rows,
        request_from_values=make_request,
    )


def parse_remote_jd_article_batch_workbook(
    content: bytes, *, account: str, dry_run: bool, headed: bool, max_rows: int = 200
) -> list[BatchPublishRow]:
    """Parse a JD article workbook and resolve its image folder on the agent."""
    def make_request(
        values: dict[str, str], positions: dict[str, int]
    ) -> tuple[PublishRequest, Path]:
        folder_path = resolve_local_path(values["image_folder_path"], "图片文件夹路径")
        original = _parse_jd_article_original(values["original"])
        with TemporaryDirectory(prefix="mpau-agent-batch-") as temporary_directory:
            fixture_image = _fixture_file(Path(temporary_directory), ".jpg", "image")
            request = validate_publish_request(
                platform="jd",
                account=account,
                content_type="article",
                image_paths=(fixture_image,),
                original_filename=folder_path.name or "article.jpg",
                title=values["title"],
                description=values["description"],
                goods_id=values["goods_id"],
                activity_topic=values["activity_topic"],
                raw_creator_declaration=(
                    values["creator_declaration"]
                    if "creator_declaration" in positions
                    else "内容无需标注"
                ),
                raw_schedule=values["schedule"],
                original=original,
                dry_run=dry_run,
                headed=headed,
            )
        return replace(request, image_paths=()), folder_path

    return _parse_rows(
        content,
        platform_label="京东",
        template_label="京东图文",
        columns=JD_ARTICLE_BATCH_COLUMNS,
        aliases=JD_ARTICLE_COLUMN_ALIASES,
        account=account,
        dry_run=dry_run,
        headed=headed,
        max_rows=max_rows,
        request_from_values=make_request,
    )


def parse_remote_xiaohongshu_video_batch_workbook(
    content: bytes, *, account: str, dry_run: bool, headed: bool, max_rows: int = 200
) -> list[BatchPublishRow]:
    """Parse a Xiaohongshu video workbook whose media paths live on the agent PC."""
    return _parse_rows(
        content,
        platform_label="小红书",
        template_label="小红书视频",
        columns=XIAOHONGSHU_VIDEO_BATCH_COLUMNS,
        aliases=XIAOHONGSHU_VIDEO_COLUMN_ALIASES,
        account=account,
        dry_run=dry_run,
        headed=headed,
        max_rows=max_rows,
        request_from_values=lambda values, positions: _remote_video_request(
            values, positions, platform="xiaohongshu", account=account, dry_run=dry_run, headed=headed
        ),
    )


def parse_remote_douyin_video_batch_workbook(
    content: bytes, *, account: str, dry_run: bool, headed: bool, max_rows: int = 200
) -> list[BatchPublishRow]:
    """Parse a Douyin video workbook whose media paths live on the agent PC."""
    return _parse_rows(
        content,
        platform_label="抖音",
        template_label="抖音视频",
        columns=DOUYIN_VIDEO_BATCH_COLUMNS,
        aliases=DOUYIN_VIDEO_COLUMN_ALIASES,
        account=account,
        dry_run=dry_run,
        headed=headed,
        max_rows=max_rows,
        request_from_values=lambda values, positions: _remote_video_request(
            values, positions, platform="douyin", account=account, dry_run=dry_run, headed=headed
        ),
    )


def _remote_social_article_request(
    values: dict[str, str],
    _positions: dict[str, int],
    *,
    platform: str,
    account: str,
    dry_run: bool,
    headed: bool,
) -> tuple[PublishRequest, Path]:
    folder_path = resolve_local_path(values["image_folder_path"], "图片文件夹路径")
    with TemporaryDirectory(prefix="mpau-agent-batch-") as temporary_directory:
        fixture_image = _fixture_file(Path(temporary_directory), ".jpg", "image")
        request = validate_publish_request(
            platform=platform,
            account=account,
            content_type="article",
            image_paths=(fixture_image,),
            original_filename=folder_path.name or "article.jpg",
            title=values["title"],
            description=values.get("description", ""),
            raw_tags=values.get("tags", "").replace("，", ","),
            raw_creator_declaration="",
            raw_schedule=values.get("schedule", ""),
            dry_run=dry_run,
            headed=headed,
        )
    return replace(request, image_paths=()), folder_path


def parse_remote_xiaohongshu_article_batch_workbook(
    content: bytes, *, account: str, dry_run: bool, headed: bool, max_rows: int = 200
) -> list[BatchPublishRow]:
    """Parse a Xiaohongshu article workbook and defer folder enumeration to the agent."""
    return _parse_rows(
        content,
        platform_label="小红书",
        template_label="小红书图文",
        columns=XIAOHONGSHU_ARTICLE_BATCH_COLUMNS,
        aliases=XIAOHONGSHU_ARTICLE_COLUMN_ALIASES,
        account=account,
        dry_run=dry_run,
        headed=headed,
        max_rows=max_rows,
        request_from_values=lambda values, positions: _remote_social_article_request(
            values, positions, platform="xiaohongshu", account=account, dry_run=dry_run, headed=headed
        ),
    )


def parse_remote_douyin_article_batch_workbook(
    content: bytes, *, account: str, dry_run: bool, headed: bool, max_rows: int = 200
) -> list[BatchPublishRow]:
    """Parse a Douyin article workbook and defer folder enumeration to the agent."""
    return _parse_rows(
        content,
        platform_label="抖音",
        template_label="抖音图文",
        columns=DOUYIN_ARTICLE_BATCH_COLUMNS,
        aliases=DOUYIN_ARTICLE_COLUMN_ALIASES,
        account=account,
        dry_run=dry_run,
        headed=headed,
        max_rows=max_rows,
        request_from_values=lambda values, positions: _remote_social_article_request(
            values, positions, platform="douyin", account=account, dry_run=dry_run, headed=headed
        ),
    )
