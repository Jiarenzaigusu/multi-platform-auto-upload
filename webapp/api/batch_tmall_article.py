# -*- coding: utf-8 -*-
"""天猫光合图文批量发布：Excel 字段、模板与行解析。"""
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
from webapp.api.models import (
    CREATOR_DECLARATIONS,
    SUPPORTED_COVER_IMAGE_EXTENSIONS,
    validate_publish_request,
)


def _normalize_tmall_cover_ratio(value: str) -> str:
    normalized = value.strip()
    if normalized == "原始":
        return "original"
    if normalized in {"3:4", "1:1"}:
        return normalized
    raise ValueError("天猫封面比例必须为原始、3:4 或 1:1")


TMALL_ARTICLE_BATCH_COLUMNS = (
    ("image_folder_path", "图片文件夹路径", True),
    ("cover_ratio", "封面比例", True),
    ("title", "标题", True),
    ("description", "发布文案", False),
    ("tags", "标签", False),
    ("goods_id", "商品ID", False),
    ("activity_topic", "活动话题", False),
    ("music_name", "音乐名称", False),
    ("schedule", "定时发布", False),
    ("creator_declaration", "创作者声明", False),
)
TMALL_ARTICLE_SAMPLE_ROW = (
    "/Users/your-name/Pictures/summer-shoes",
    "3:4",
    "夏季女鞋图文",
    "轻盈舒适，适合通勤与日常穿搭。",
    "女鞋,夏季穿搭",
    "123456789",
    "夏日上新",
    "默契",
    "2030年12月31日 14点30分",
    "内容含营销信息",
)
TMALL_ARTICLE_COLUMN_ALIASES = {
    "image_folder_path": {
        "图片文件夹路径",
        "图片文件夹",
        "imagefolderpath",
        "imagepaths",
        "images",
    },
    "cover_ratio": {"封面比例"},
    "title": {"标题", "title"},
    "description": {"发布文案", "文案", "description"},
    "tags": {"标签", "tags"},
    "goods_id": {"商品id", "商品编号", "goodsid"},
    "activity_topic": {"活动话题", "话题", "activitytopic"},
    "music_name": {"音乐名称", "音乐", "歌曲名称", "musicname", "music"},
    "creator_declaration": {"创作者声明", "内容声明", "creatordeclaration"},
    "schedule": {"定时发布", "发布时间", "schedule"},
}


def build_tmall_article_template() -> bytes:
    """生成字段与单条图文发布一致的天猫图文批量模板。"""
    return build_content_template(
        sheet_title="天猫图文批量发布",
        columns=TMALL_ARTICLE_BATCH_COLUMNS,
        sample_row=TMALL_ARTICLE_SAMPLE_ROW,
        list_validations=(
            ("cover_ratio", ("原始", "3:4", "1:1"), "无效的封面比例", "请选择原始、3:4 或 1:1"),
            ("creator_declaration", CREATOR_DECLARATIONS, "无效的创作者声明", "请从下拉列表中选择预定义的创作者声明"),
        ),
    )


def _resolve_image_paths(raw_folder_path: str) -> tuple:
    """读取图片文件夹中的平台支持图片，按文件名升序保留发布顺序。"""
    folder_path = resolve_local_path(raw_folder_path, "图片文件夹路径")
    if not folder_path.is_dir():
        raise ValueError("图片文件夹路径不是文件夹")
    try:
        image_paths = sorted(
            (
                path for path in folder_path.iterdir()
                if path.is_file() and path.suffix.lower() in SUPPORTED_COVER_IMAGE_EXTENSIONS
            ),
            key=lambda path: (path.name.casefold(), path.name),
        )
    except OSError as exc:
        raise ValueError("无法读取图片文件夹") from exc
    if not image_paths:
        raise ValueError("图片文件夹中没有可发布的 JPG、PNG 或 WebP 图片")
    return tuple(image_paths)


def parse_tmall_article_batch_workbook(
    content: bytes, *, account: str, dry_run: bool, headed: bool, max_rows: int = 200
) -> list[BatchPublishRow]:
    """校验并解析天猫图文批量工作簿。"""
    workbook = open_batch_workbook(content, "天猫")
    try:
        rows = workbook.active.iter_rows(values_only=True)
        positions, header_row_number = find_header_row(
            rows,
            columns=TMALL_ARTICLE_BATCH_COLUMNS,
            column_aliases=TMALL_ARTICLE_COLUMN_ALIASES,
            template_label="天猫图文",
        )
        errors: list[BatchRowError] = []
        parsed_rows: list[BatchPublishRow] = []
        for row_number, values in enumerate(rows, start=header_row_number + 1):
            row_values = {field: row_value(values, positions, field) for field, _label, _required in TMALL_ARTICLE_BATCH_COLUMNS}
            if not any(row_values.values()):
                continue
            if len(parsed_rows) + len(errors) >= max_rows:
                errors.append(BatchRowError(row_number, "整行", f"单次最多导入 {max_rows} 条内容"))
                break
            try:
                image_paths = _resolve_image_paths(row_values["image_folder_path"])
                request = validate_publish_request(
                    platform="tmall", account=account, content_type="article", image_paths=image_paths,
                    cover_ratio=_normalize_tmall_cover_ratio(row_values["cover_ratio"]), original_filename=image_paths[0].name, title=row_values["title"],
                    description=row_values["description"], raw_tags=row_values["tags"].replace("，", ","),
                    goods_id=row_values["goods_id"], activity_topic=row_values["activity_topic"],
                    raw_music_name=row_values["music_name"],
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
