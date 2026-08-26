# -*- coding: utf-8 -*-
"""抖音视频批量发布：独立 Excel 字段、模板与行解析。"""
from __future__ import annotations

from webapp.api.batch import (
    BatchPublishRow,
    BatchRowError,
    BatchValidationError,
    find_header_row,
    open_batch_workbook,
    resolve_local_path,
    resolve_video_path,
    row_value,
)
from webapp.api.batch_template_workbook import build_content_template
from webapp.api.models import validate_publish_request


DOUYIN_VIDEO_BATCH_COLUMNS = (
    ("video_path", "视频路径", True),
    ("cover_image_path", "横版封面", False),
    ("title", "标题", True),
    ("description", "视频描述", False),
    ("tags", "标签", False),
    ("schedule", "定时发布", False),
)
DOUYIN_VIDEO_SAMPLE_ROW = (
    "/Users/your-name/Videos/example.mp4",
    "/Users/your-name/Pictures/cover.png",
    "夏季女鞋穿搭",
    "轻盈舒适，适合通勤与日常穿搭。",
    "女鞋,夏季穿搭",
    "2030年12月31日 14点30分",
)
DOUYIN_VIDEO_COLUMN_ALIASES = {
    "video_path": {"视频路径", "视频文件", "videopath", "video"},
    "cover_image_path": {"横版封面", "自定义封面", "封面路径", "封面图片", "coverimagepath", "cover"},
    "title": {"标题", "title"},
    "description": {"视频描述", "正文", "文案", "发布文案", "description"},
    "tags": {"标签", "话题", "tags"},
    "schedule": {"定时发布", "发布时间", "schedule"},
}


def build_douyin_video_template() -> bytes:
    return build_content_template(
        sheet_title="抖音视频批量发布",
        columns=DOUYIN_VIDEO_BATCH_COLUMNS,
        sample_row=DOUYIN_VIDEO_SAMPLE_ROW,
    )


def parse_douyin_video_batch_workbook(
    content: bytes, *, account: str, dry_run: bool, headed: bool, max_rows: int = 200
) -> list[BatchPublishRow]:
    workbook = open_batch_workbook(content, "抖音")
    try:
        rows = workbook.active.iter_rows(values_only=True)
        positions, header_row_number = find_header_row(
            rows,
            columns=DOUYIN_VIDEO_BATCH_COLUMNS,
            column_aliases=DOUYIN_VIDEO_COLUMN_ALIASES,
            template_label="抖音视频",
        )
        errors: list[BatchRowError] = []
        parsed_rows: list[BatchPublishRow] = []
        for row_number, values in enumerate(rows, start=header_row_number + 1):
            row_values = {
                field: row_value(values, positions, field)
                for field, _label, _required in DOUYIN_VIDEO_BATCH_COLUMNS
            }
            if not any(row_values.values()):
                continue
            if len(parsed_rows) + len(errors) >= max_rows:
                errors.append(BatchRowError(row_number, "整行", f"单次最多导入 {max_rows} 条内容"))
                break
            try:
                video_path = resolve_video_path(row_values["video_path"])
                cover_image_path = (
                    resolve_local_path(row_values["cover_image_path"], "横版封面路径")
                    if row_values["cover_image_path"]
                    else None
                )
                request = validate_publish_request(
                    platform="douyin",
                    account=account,
                    content_type="video",
                    video_path=video_path,
                    cover_image_path=cover_image_path,
                    original_filename=video_path.name,
                    title=row_values["title"],
                    description=row_values["description"],
                    raw_tags=row_values["tags"].replace("，", ","),
                    raw_creator_declaration="",
                    raw_schedule=row_values["schedule"],
                    dry_run=dry_run,
                    headed=headed,
                )
            except ValueError as exc:
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
