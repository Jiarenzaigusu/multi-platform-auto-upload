# -*- coding: utf-8 -*-
"""天猫光合视频批量发布：Excel 字段、模板与行解析。"""
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
from webapp.api.models import CREATOR_DECLARATIONS, validate_publish_request

TMALL_COVER_RATIO_OPTIONS = ("原始", "3:4", "1:1")
TMALL_COVER_RATIO_VALUES = dict(zip(TMALL_COVER_RATIO_OPTIONS, ("original", "3:4", "1:1")))


def _normalize_tmall_cover_ratio(value: str) -> str:
    try:
        return TMALL_COVER_RATIO_VALUES[value.strip()]
    except KeyError as exc:
        raise ValueError("天猫封面比例必须为原始、3:4 或 1:1") from exc


TMALL_VIDEO_BATCH_COLUMNS = (
    ("video_path", "视频路径", True),
    ("cover_image_path", "自定义封面", False),
    ("cover_ratio", "封面比例", True),
    ("title", "标题", True),
    ("description", "文案", False),
    ("tags", "标签", False),
    ("goods_id", "商品ID", False),
    ("activity_topic", "活动话题", False),
    ("music_name", "音乐名称", False),
    ("schedule", "定时发布", False),
    ("creator_declaration", "创作者声明", False),
)
TMALL_VIDEO_SAMPLE_ROW = (
    "/Users/your-name/Videos/example.mp4",
    "/Users/your-name/Pictures/cover.png",
    "3:4",
    "夏季女鞋穿搭",
    "轻盈舒适，适合通勤与日常穿搭。",
    "女鞋,夏季穿搭",
    "123456789",
    "夏日上新",
    "默契",
    "2030年12月31日 14点30分",
    "内容含营销信息",
)
TMALL_VIDEO_COLUMN_ALIASES = {
    "video_path": {"视频路径", "视频文件", "videopath", "video"},
    "cover_image_path": {"自定义封面", "封面路径", "封面图片", "coverimagepath", "cover"},
    "cover_ratio": {"封面比例"},
    "title": {"标题", "title"},
    "description": {"文案", "发布文案", "description"},
    "tags": {"标签", "tags"},
    "goods_id": {"商品id", "商品编号", "goodsid"},
    "activity_topic": {"活动话题", "话题", "activitytopic"},
    "music_name": {"音乐名称", "音乐", "歌曲名称", "musicname", "music"},
    "creator_declaration": {"创作者声明", "内容声明", "creatordeclaration"},
    "schedule": {"定时发布", "发布时间", "schedule"},
}


def build_tmall_video_template() -> bytes:
    """生成天猫视频批量发布模板。"""
    return build_content_template(
        sheet_title="天猫视频批量发布",
        columns=TMALL_VIDEO_BATCH_COLUMNS,
        sample_row=TMALL_VIDEO_SAMPLE_ROW,
        list_validations=(
            ("creator_declaration", CREATOR_DECLARATIONS, "无效的创作者声明", "请从下拉列表中选择预定义的创作者声明"),
        ),
        conditional_list_validations=(
            ("cover_ratio", "cover_image_path", TMALL_COVER_RATIO_OPTIONS, "无效的封面比例", "有自定义封面时请选择原始、3:4 或 1:1；未上传封面时请留空"),
        ),
    )


def parse_tmall_video_batch_workbook(
    content: bytes, *, account: str, dry_run: bool, headed: bool, max_rows: int = 200
) -> list[BatchPublishRow]:
    """校验并解析天猫视频批量工作簿。"""
    workbook = open_batch_workbook(content, "天猫")
    try:
        rows = workbook.active.iter_rows(values_only=True)
        positions, header_row_number = find_header_row(
            rows,
            columns=TMALL_VIDEO_BATCH_COLUMNS,
            column_aliases=TMALL_VIDEO_COLUMN_ALIASES,
            template_label="天猫视频",
        )
        errors: list[BatchRowError] = []
        parsed_rows: list[BatchPublishRow] = []
        for row_number, values in enumerate(rows, start=header_row_number + 1):
            row_values = {field: row_value(values, positions, field) for field, _label, _required in TMALL_VIDEO_BATCH_COLUMNS}
            if not any(row_values.values()):
                continue
            if len(parsed_rows) + len(errors) >= max_rows:
                errors.append(BatchRowError(row_number, "整行", f"单次最多导入 {max_rows} 条内容"))
                break
            try:
                video_path = resolve_video_path(row_values["video_path"])
                cover_image_path = resolve_local_path(row_values["cover_image_path"], "自定义封面路径") if row_values["cover_image_path"] else None
                raw_cover_ratio = row_values["cover_ratio"]
                if cover_image_path is None and raw_cover_ratio:
                    raise ValueError("未上传自定义封面时，封面比例必须留空")
                cover_ratio = _normalize_tmall_cover_ratio(raw_cover_ratio) if cover_image_path else None
                request = validate_publish_request(
                    platform="tmall", account=account, content_type="video", video_path=video_path,
                    cover_image_path=cover_image_path, cover_ratio=cover_ratio, original_filename=video_path.name,
                    title=row_values["title"], description=row_values["description"],
                    raw_tags=row_values["tags"].replace("，", ","), goods_id=row_values["goods_id"],
                    activity_topic=row_values["activity_topic"], raw_music_name=row_values["music_name"],
                    raw_creator_declaration=row_values["creator_declaration"] if "creator_declaration" in positions else "内容无需标注",
                    raw_schedule=row_values["schedule"], dry_run=dry_run, headed=headed,
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
