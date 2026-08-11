from __future__ import annotations

from pathlib import Path

from webapp.api.batch import (
    BatchPublishRow,
    BatchRowError,
    BatchValidationError,
    find_header_row,
    open_batch_workbook,
    resolve_video_path,
    row_value,
)
from webapp.api.models import validate_publish_request


TMALL_BATCH_COLUMNS = (
    ("video_path", "视频路径", True),
    ("title", "标题", True),
    ("description", "文案", False),
    ("tags", "标签", False),
    ("goods_id", "商品ID", False),
    ("activity_topic", "活动话题", False),
    ("music_name", "音乐名称", False),
    ("schedule", "定时发布", False),
    ("creator_declaration", "创作者声明", True),
)

TMALL_COLUMN_ALIASES = {
    "video_path": {"视频路径", "视频文件", "videopath", "video"},
    "title": {"标题", "title"},
    "description": {"文案", "发布文案", "description"},
    "tags": {"标签", "tags"},
    "goods_id": {"商品id", "商品编号", "goodsid"},
    "activity_topic": {"活动话题", "话题", "activitytopic"},
    "music_name": {"音乐名称", "音乐", "歌曲名称", "musicname", "music"},
    "creator_declaration": {"创作者声明", "内容声明", "creatordeclaration"},
    "schedule": {"定时发布", "发布时间", "schedule"},
}


def parse_tmall_batch_workbook(
    content: bytes,
    *,
    account: str,
    dry_run: bool,
    headed: bool,
    base_dir: Path,
    max_rows: int = 200,
) -> list[BatchPublishRow]:
    """Validate all Tmall rows before the API queues any task."""
    workbook = open_batch_workbook(content, "天猫")
    try:
        worksheet = workbook.active
        rows = worksheet.iter_rows(values_only=True)
        positions, header_row_number = find_header_row(
            rows,
            columns=TMALL_BATCH_COLUMNS,
            column_aliases=TMALL_COLUMN_ALIASES,
            template_label="天猫",
        )

        errors: list[BatchRowError] = []
        parsed_rows: list[BatchPublishRow] = []
        populated_rows = 0
        for row_number, values in enumerate(rows, start=header_row_number + 1):
            row_values = {
                field: row_value(values, positions, field)
                for field, _label, _required in TMALL_BATCH_COLUMNS
            }
            if not any(row_values.values()):
                continue

            populated_rows += 1
            if populated_rows > max_rows:
                errors.append(BatchRowError(row_number, "整行", f"单次最多导入 {max_rows} 条内容"))
                break

            try:
                video_path = resolve_video_path(row_values["video_path"], base_dir)
                request = validate_publish_request(
                    platform="tmall",
                    account=account,
                    video_path=video_path,
                    original_filename=video_path.name,
                    title=row_values["title"],
                    description=row_values["description"],
                    raw_tags=row_values["tags"].replace("，", ","),
                    goods_id=row_values["goods_id"],
                    activity_topic=row_values["activity_topic"],
                    raw_music_name=row_values["music_name"],
                    raw_creator_declaration=row_values["creator_declaration"],
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
