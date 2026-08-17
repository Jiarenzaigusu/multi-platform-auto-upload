# -*- coding: utf-8 -*-
"""京东京麦视频批量发布：Excel 字段、模板与行解析。"""
from __future__ import annotations

from webapp.api.batch import (
    BatchPublishRow,
    BatchRowError,
    BatchValidationError,
    find_header_row,
    open_batch_workbook,
    resolve_video_path,
    row_value,
)
from webapp.api.batch_template_workbook import build_content_template
from webapp.api.models import CREATOR_DECLARATIONS, validate_publish_request


JD_VIDEO_BATCH_COLUMNS = (
    ("video_path", "视频路径", True),
    ("title", "标题", True),
    ("goods_id", "商品ID", False),
    ("schedule", "定时发布", False),
    ("original", "自主原创", False),
    ("creator_declaration", "创作者声明", False),
)
JD_VIDEO_SAMPLE_ROW = (
    "/Users/your-name/Videos/example.mp4", "夏季女鞋穿搭", "123456789",
    "2030年12月31日 14点30分", "否", "内容含营销广告",
)
JD_VIDEO_COLUMN_ALIASES = {
    "video_path": {"视频路径", "视频文件", "videopath", "video"},
    "title": {"标题", "title"},
    "goods_id": {"商品id", "商品编号", "goodsid"},
    "schedule": {"定时发布", "发布时间", "schedule"},
    "original": {"自主原创", "原创", "original"},
    "creator_declaration": {"创作者声明", "内容声明", "creatordeclaration"},
}
_TRUE_VALUES = {"是", "true", "1", "yes", "y"}
_FALSE_VALUES = {"", "否", "false", "0", "no", "n"}


def build_jd_video_template() -> bytes:
    """生成京东视频批量发布模板。"""
    return build_content_template(
        sheet_title="京东批量发布",
        columns=JD_VIDEO_BATCH_COLUMNS,
        sample_row=JD_VIDEO_SAMPLE_ROW,
        list_validations=(
            ("original", ("是", "否"), "无效的自主原创", '请填写"是"或"否"'),
            ("creator_declaration", CREATOR_DECLARATIONS, "无效的创作者声明", "请从下拉列表中选择预定义的创作者声明"),
        ),
    )


def _parse_original(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ValueError("请填写“是”或“否”")


def parse_jd_video_batch_workbook(
    content: bytes, *, account: str, dry_run: bool, headed: bool, max_rows: int = 200
) -> list[BatchPublishRow]:
    """校验并解析京东视频批量工作簿。"""
    workbook = open_batch_workbook(content, "京东")
    try:
        rows = workbook.active.iter_rows(values_only=True)
        positions, header_row_number = find_header_row(
            rows, columns=JD_VIDEO_BATCH_COLUMNS, column_aliases=JD_VIDEO_COLUMN_ALIASES, template_label="京东视频"
        )
        errors: list[BatchRowError] = []
        parsed_rows: list[BatchPublishRow] = []
        for row_number, values in enumerate(rows, start=header_row_number + 1):
            row_values = {field: row_value(values, positions, field) for field, _label, _required in JD_VIDEO_BATCH_COLUMNS}
            if not any(row_values.values()):
                continue
            if len(parsed_rows) + len(errors) >= max_rows:
                errors.append(BatchRowError(row_number, "整行", f"单次最多导入 {max_rows} 条内容"))
                break
            try:
                original = _parse_original(row_values["original"])
            except ValueError as exc:
                errors.append(BatchRowError(row_number, "自主原创", str(exc)))
                continue
            try:
                video_path = resolve_video_path(row_values["video_path"])
                request = validate_publish_request(
                    platform="jd", account=account, content_type="video", video_path=video_path,
                    original_filename=video_path.name, title=row_values["title"], goods_id=row_values["goods_id"],
                    raw_schedule=row_values["schedule"], original=original,
                    raw_creator_declaration=row_values["creator_declaration"] if "creator_declaration" in positions else "内容无需标注",
                    dry_run=dry_run, headed=headed,
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
