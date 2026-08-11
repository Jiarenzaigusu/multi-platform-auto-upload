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


BatchColumn = tuple[str, str, bool]


@dataclass(frozen=True, slots=True)
class BatchRowError:
    row: int
    field: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"row": self.row, "field": self.field, "message": self.message}


class BatchValidationError(ValueError):
    def __init__(self, message: str, errors: list[BatchRowError]):
        super().__init__(message)
        self.errors = errors


@dataclass(frozen=True, slots=True)
class BatchPublishRow:
    """A validated publishing request and the Excel row that supplied it."""

    row_number: int
    request: PublishRequest


def cell_text(value: Any) -> str:
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
    return re.sub(r"[\s_]+", "", cell_text(value)).lower()


def open_batch_workbook(content: bytes, platform_label: str):
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
    """Find a supported header in the first rows while preserving the row iterator."""
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

        if all(field in positions for field in required_fields):
            if errors:
                raise BatchValidationError(
                    f"Excel 表头不符合{template_label}批量模板", errors
                )
            return positions, candidate_row_number
        if candidate_row_number >= max_header_rows:
            break

    required_labels = "、".join(
        label for _field, label, required in columns if required
    )
    raise BatchValidationError(
        f"Excel 表头不符合{template_label}批量模板",
        [BatchRowError(1, "表头", f"请填写必填表头：{required_labels}")],
    )


def row_value(values: tuple[Any, ...], positions: dict[str, int], field: str) -> str:
    index = positions.get(field)
    if index is None or index >= len(values):
        return ""
    return cell_text(values[index])


def resolve_video_path(raw_path: str, base_dir: Path) -> Path:
    """Resolve a batch video strictly inside the authenticated user's media root."""
    try:
        video_path = Path(raw_path)
    except RuntimeError as exc:
        raise ValueError("视频路径无法解析") from exc
    if video_path.is_absolute():
        raise ValueError("视频路径必须填写当前用户素材目录内的相对路径")
    root = base_dir.resolve()
    resolved = (root / video_path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("视频路径不能超出当前用户素材目录") from exc
    return resolved
