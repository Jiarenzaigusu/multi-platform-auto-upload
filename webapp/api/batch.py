# -*- coding: utf-8 -*-
"""
webapp.api.batch 模块

批量发布 Excel 工作簿解析的通用工具。

提供：
- BatchRowError / BatchValidationError: 批量校验错误模型
- BatchPublishRow: 已校验的发布请求与对应 Excel 行号
- open_batch_workbook: 打开 .xlsx 工作簿（带异常包装）
- find_header_row: 在前 N 行中查找支持的表头
- cell_text / normalize_header / row_value / resolve_video_path: 单元格工具函数

各平台与内容类型的专属解析逻辑位于 ``batch_<platform>_<content_type>.py``，
共用本模块的工具函数。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable
from zipfile import BadZipFile

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from webapp.api.models import PublishRequest


# 批量列定义：(字段名, 列标题, 是否必填)
BatchColumn = tuple[str, str, bool]


@dataclass(frozen=True, slots=True)
class BatchRowError:
    """批量校验中单行单字段的错误。

    用于在 UI 中精确定位哪个 Excel 行的哪个字段出错。
    """

    row: int          # Excel 行号
    field: str        # 字段名（或"整行"/"表头"/"文件"）
    message: str      # 错误信息

    def to_dict(self) -> dict[str, Any]:
        """转换为前端可消费的 dict 结构。"""
        return {"row": self.row, "field": self.field, "message": self.message}


class BatchValidationError(ValueError):
    """批量校验异常，包含一行或多行错误。

    任一行错误时不会创建任何任务，整张表校验通过才会提交。
    """

    def __init__(self, message: str, errors: list[BatchRowError]):
        """初始化异常。

        :param message: 异常总体信息
        :param errors: 逐行逐字段的错误列表
        """
        super().__init__(message)
        self.errors = errors


@dataclass(frozen=True, slots=True)
class BatchPublishRow:
    """已校验的发布请求与其来源 Excel 行号。"""

    row_number: int          # Excel 行号
    request: PublishRequest  # 校验后的发布请求


def cell_text(value: Any) -> str:
    """将 Excel 单元格值转换为字符串。

    处理：
    - None → 空字符串
    - datetime → "YYYY-MM-DD HH:MM"
    - date → "YYYY-MM-DD 00:00"
    - 整数浮点 → 去掉 .0
    - 其他 → str(value).strip()
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d 00:00")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def normalize_header(value: Any) -> str:
    """归一化表头：去空白与下划线，转小写。用于表头别名匹配。"""
    return re.sub(r"[\s_]+", "", cell_text(value)).lower()


def open_batch_workbook(content: bytes, platform_label: str):
    """从字节流打开 .xlsx 工作簿。

    :param content: Excel 文件字节内容
    :param platform_label: 平台中文名（"天猫"/"京东"），用于错误提示
    :returns: openpyxl Workbook 对象
    :raises BatchValidationError: 文件不是有效的 .xlsx
    """
    try:
        return load_workbook(BytesIO(content), read_only=True, data_only=True)
    except (BadZipFile, EOFError, InvalidFileException, KeyError, OSError, ValueError) as exc:
        raise BatchValidationError(
            f"无法读取 Excel 文件，请上传 .xlsx 格式的{platform_label}批量发布模板",
            [BatchRowError(1, "文件", "不是有效的 .xlsx 文件")],
        ) from exc


def find_header_row(
    rows: Iterable[tuple[Any, ...]],
    *,
    columns: tuple[BatchColumn, ...],
    column_aliases: dict[str, set[str]],
    template_label: str,
    max_header_rows: int = 10,
) -> tuple[dict[str, int], int]:
    """在前 N 行中查找支持的表头。

    :param rows: 行迭代器（values_only 形式）
    :param columns: 批量列定义元组
    :param column_aliases: 字段名→别名集合的映射
    :param template_label: 平台中文名，用于错误提示
    :param max_header_rows: 最多查找多少行作为表头候选
    :returns: (字段→列索引的映射, 表头所在行号)
    :raises BatchValidationError: 未找到所有必填表头，或表头重复

    设计要点：返回行迭代器当前位置，调用方可继续遍历后续数据行。
    """
    # 构建别名→字段名的映射
    aliases = {
        normalize_header(alias): field
        for field, names in column_aliases.items()
        for alias in names
    }
    required_fields = tuple(field for field, _label, required in columns if required)

    for candidate_row_number, candidate in enumerate(rows, start=1):
        positions: dict[str, int] = {}
        errors: list[BatchRowError] = []
        for index, value in enumerate(candidate):
            field = aliases.get(normalize_header(value))
            if not field:
                continue
            if field in positions:
                errors.append(BatchRowError(candidate_row_number, cell_text(value), "表头重复"))
                continue
            positions[field] = index

        # 所有必填字段都找到 → 表头识别成功
        if all(field in positions for field in required_fields):
            if errors:
                raise BatchValidationError(
                    f"Excel 表头不符合{template_label}批量模板", errors
                )
            return positions, candidate_row_number
        if candidate_row_number >= max_header_rows:
            break

    required_labels = "和".join(
        label for _field, label, required in columns if required
    )
    raise BatchValidationError(
        f"Excel 表头不符合{template_label}批量模板",
        [BatchRowError(1, "表头", f"请填写{required_labels}两个必填表头")],
    )


def row_value(values: tuple[Any, ...], positions: dict[str, int], field: str) -> str:
    """从行值元组中按列索引取指定字段的文本值。

    :param values: 行值元组
    :param positions: 字段→列索引映射
    :param field: 字段名
    :returns: 单元格文本（超出范围或不存在返回空字符串）
    """
    index = positions.get(field)
    if index is None or index >= len(values):
        return ""
    return cell_text(values[index])


def resolve_local_path(raw_path: str, field_label: str) -> Path:
    """解析本机素材路径，仅接受非空绝对路径。

    Excel/WPS 用户复制路径时常会附带首尾单引号或双引号，且粘贴操作可能产生
    单双引号混用。它们不是 Windows 路径的一部分，因此在绝对路径校验前移除；
    路径中间的引号保持不变。

    :param raw_path: 原始路径字符串
    :param field_label: 用于面向用户错误提示的字段名
    """
    normalized_path = raw_path.strip()
    path_quote_chars = "'\"‘’“”"
    normalized_path = normalized_path.lstrip(path_quote_chars).rstrip(path_quote_chars).strip()
    if not normalized_path:
        raise ValueError(f"{field_label}不能为空")
    try:
        path = Path(normalized_path)
    except (RuntimeError, ValueError) as exc:
        raise ValueError(f"{field_label}无法解析") from exc
    if not path.is_absolute():
        raise ValueError(f"{field_label}必须填写本机绝对路径")
    return path


def resolve_video_path(raw_path: str) -> Path:
    """解析视频路径，仅接受本机绝对路径。"""
    return resolve_local_path(raw_path, "视频路径")
