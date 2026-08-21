# -*- coding: utf-8 -*-
"""京东京麦图文批量发布：独立 Excel 字段、模板和解析。"""
from __future__ import annotations

from webapp.api.batch import BatchPublishRow, BatchRowError, BatchValidationError, find_header_row, open_batch_workbook, resolve_local_path, row_value
from webapp.api.batch_template_workbook import build_content_template
from webapp.api.models import JD_CREATOR_DECLARATIONS, JD_ARTICLE_IMAGE_EXTENSIONS, validate_publish_request

JD_ARTICLE_BATCH_COLUMNS = (
    ("image_folder_path", "图片文件夹路径", True), ("title", "标题", True),
    ("description", "正文内容", False), ("goods_id", "商品ID", False),
    ("activity_topic", "参与话题", False), ("schedule", "定时发布", False), ("original", "自主原创", False),
    ("creator_declaration", "创作者声明", False),
)
JD_ARTICLE_SAMPLE_ROW = ("/Users/your-name/Pictures/jd-graphic", "夏日好物分享", "真实体验与使用场景分享。", "123456789", "数码先锋", "", "否", "内容无需标注")
JD_ARTICLE_COLUMN_ALIASES = {
    "image_folder_path": {"图片文件夹路径", "图片文件夹", "图片路径", "imagefolderpath", "images"},
    "title": {"标题", "title"}, "description": {"正文内容", "正文", "文案", "description"},
    "goods_id": {"商品id", "商品编号", "goodsid"}, "activity_topic": {"参与话题", "话题", "activitytopic"},
    "schedule": {"定时发布", "发布时间", "schedule"}, "original": {"自主原创", "原创", "original"},
    "creator_declaration": {"创作者声明", "内容声明", "creatordeclaration"},
}

_TRUE_VALUES = {"是", "true", "1", "yes", "y"}
_FALSE_VALUES = {"", "否", "false", "0", "no", "n"}


def build_jd_article_template() -> bytes:
    return build_content_template(
        sheet_title="京东图文批量发布", columns=JD_ARTICLE_BATCH_COLUMNS, sample_row=JD_ARTICLE_SAMPLE_ROW,
        list_validations=(
            ("original", ("是", "否"), "无效的自主原创", '请填写"是"或"否"'),
            ("creator_declaration", JD_CREATOR_DECLARATIONS, "无效的创作者声明", "请从下拉列表中选择预定义的创作者声明"),
        ),
    )


def _resolve_images(raw: str) -> tuple:
    folder = resolve_local_path(raw, "图片文件夹路径")
    if not folder.is_dir():
        raise ValueError("图片文件夹路径不是文件夹")
    images = tuple(sorted((path for path in folder.iterdir() if path.is_file() and path.suffix.lower() in JD_ARTICLE_IMAGE_EXTENSIONS), key=lambda p: (p.name.casefold(), p.name)))
    if not images:
        raise ValueError("图片文件夹中没有 JPG 或 PNG 图片")
    return images


def _parse_original(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ValueError("请填写“是”或“否”")


def parse_jd_article_batch_workbook(content: bytes, *, account: str, dry_run: bool, headed: bool, max_rows: int = 200) -> list[BatchPublishRow]:
    workbook = open_batch_workbook(content, "京东")
    try:
        rows = workbook.active.iter_rows(values_only=True)
        positions, header_row = find_header_row(rows, columns=JD_ARTICLE_BATCH_COLUMNS, column_aliases=JD_ARTICLE_COLUMN_ALIASES, template_label="京东图文")
        errors, parsed = [], []
        for row_number, values in enumerate(rows, start=header_row + 1):
            row_values = {field: row_value(values, positions, field) for field, _label, _required in JD_ARTICLE_BATCH_COLUMNS}
            if not any(row_values.values()):
                continue
            if len(parsed) + len(errors) >= max_rows:
                errors.append(BatchRowError(row_number, "整行", f"单次最多导入 {max_rows} 条内容")); break
            try:
                original = _parse_original(row_values["original"])
            except ValueError as exc:
                errors.append(BatchRowError(row_number, "自主原创", str(exc))); continue
            try:
                images = _resolve_images(row_values["image_folder_path"])
                request = validate_publish_request(
                    platform="jd", account=account, content_type="article", image_paths=images, original_filename=images[0].name,
                    title=row_values["title"], description=row_values["description"], goods_id=row_values["goods_id"],
                    activity_topic=row_values["activity_topic"], raw_schedule=row_values["schedule"],
                    original=original, raw_creator_declaration=row_values["creator_declaration"] or "内容无需标注",
                    dry_run=dry_run, headed=headed,
                )
            except (ValueError, OSError) as exc:
                errors.append(BatchRowError(row_number, "内容", str(exc))); continue
            parsed.append(BatchPublishRow(row_number=row_number, request=request))
        if not parsed and not errors:
            errors.append(BatchRowError(header_row + 1, "整行", "至少需要填写一条发布内容"))
        if errors:
            raise BatchValidationError("Excel 内容校验失败，未创建任何发布任务", errors)
        return parsed
    finally:
        workbook.close()
