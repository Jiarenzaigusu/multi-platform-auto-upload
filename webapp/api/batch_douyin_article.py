# -*- coding: utf-8 -*-
"""抖音图文批量发布：独立 Excel 字段、模板与行解析。"""
from __future__ import annotations

from webapp.api.batch import (
    BatchPublishRow,
    BatchRowError,
    BatchValidationError,
    find_header_row,
    open_batch_workbook,
    resolve_local_path,
    row_value,
)
from webapp.api.batch_template_workbook import build_content_template
from webapp.api.models import SUPPORTED_COVER_IMAGE_EXTENSIONS, validate_publish_request


DOUYIN_ARTICLE_BATCH_COLUMNS = (
    ("image_folder_path", "图片文件夹路径", True),
    ("title", "标题", True),
    ("description", "图文描述", False),
    ("tags", "标签", False),
    ("schedule", "定时发布", False),
)
DOUYIN_ARTICLE_SAMPLE_ROW = (
    "/Users/your-name/Pictures/douyin-note",
    "夏季女鞋图文",
    "轻盈舒适，适合通勤与日常穿搭。",
    "女鞋,夏季穿搭",
    "2030年12月31日 14点30分",
)
DOUYIN_ARTICLE_COLUMN_ALIASES = {
    "image_folder_path": {"图片文件夹路径", "图片文件夹", "图片路径", "imagefolderpath", "images"},
    "title": {"标题", "title"},
    "description": {"图文描述", "正文", "文案", "发布文案", "description"},
    "tags": {"标签", "话题", "tags"},
    "schedule": {"定时发布", "发布时间", "schedule"},
}


def build_douyin_article_template() -> bytes:
    return build_content_template(
        sheet_title="抖音图文批量发布",
        columns=DOUYIN_ARTICLE_BATCH_COLUMNS,
        sample_row=DOUYIN_ARTICLE_SAMPLE_ROW,
    )


def _resolve_images(raw: str) -> tuple:
    folder = resolve_local_path(raw, "图片文件夹路径")
    if not folder.is_dir():
        raise ValueError("图片文件夹路径不是文件夹")
    try:
        images = tuple(
            sorted(
                (
                    path for path in folder.iterdir()
                    if path.is_file() and path.suffix.lower() in SUPPORTED_COVER_IMAGE_EXTENSIONS
                ),
                key=lambda path: (path.name.casefold(), path.name),
            )
        )
    except OSError as exc:
        raise ValueError("无法读取图片文件夹") from exc
    if not images:
        raise ValueError("图片文件夹中没有 JPG、PNG 或 WebP 图片")
    return images


def parse_douyin_article_batch_workbook(
    content: bytes, *, account: str, dry_run: bool, headed: bool, max_rows: int = 200
) -> list[BatchPublishRow]:
    workbook = open_batch_workbook(content, "抖音")
    try:
        rows = workbook.active.iter_rows(values_only=True)
        positions, header_row_number = find_header_row(
            rows,
            columns=DOUYIN_ARTICLE_BATCH_COLUMNS,
            column_aliases=DOUYIN_ARTICLE_COLUMN_ALIASES,
            template_label="抖音图文",
        )
        errors: list[BatchRowError] = []
        parsed_rows: list[BatchPublishRow] = []
        for row_number, values in enumerate(rows, start=header_row_number + 1):
            row_values = {
                field: row_value(values, positions, field)
                for field, _label, _required in DOUYIN_ARTICLE_BATCH_COLUMNS
            }
            if not any(row_values.values()):
                continue
            if len(parsed_rows) + len(errors) >= max_rows:
                errors.append(BatchRowError(row_number, "整行", f"单次最多导入 {max_rows} 条内容"))
                break
            try:
                images = _resolve_images(row_values["image_folder_path"])
                request = validate_publish_request(
                    platform="douyin",
                    account=account,
                    content_type="article",
                    image_paths=images,
                    original_filename=images[0].name,
                    title=row_values["title"],
                    description=row_values["description"],
                    raw_tags=row_values["tags"].replace("，", ","),
                    raw_creator_declaration="",
                    raw_schedule=row_values["schedule"],
                    dry_run=dry_run,
                    headed=headed,
                )
            except (ValueError, OSError) as exc:
                errors.append(BatchRowError(row_number, "内容", str(exc)))
                continue
            parsed_rows.append(BatchPublishRow(row_number=row_number, request=request))
        if not parsed_rows and not errors:
            errors.append(BatchRowError(header_row_number + 1, "整行", "至少需要填写一条发布内容"))
        if errors:
            raise BatchValidationError("Excel 内容校验失败，未创建任何发布任务", errors)
        return parsed_rows
    finally:
        workbook.close()
