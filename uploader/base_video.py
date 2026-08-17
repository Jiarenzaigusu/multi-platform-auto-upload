# -*- coding: utf-8 -*-
"""
uploader.base_video 模块

定义所有平台视频上传器的通用基类 BaseVideoUploader。

该基类不直接执行浏览器自动化，仅提供三类共享校验：
1. 视频文件格式与存在性校验
2. 图片文件格式与存在性校验（天猫自定义封面使用）
3. 定时发布时间的有效性校验（必须晚于当前时间至少 2 小时）

天猫 TmallVideo 与京东 JDVideo 均继承自本基类，复用上述校验逻辑。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path


class BaseVideoUploader:
    """视频上传器基类。

    定义所有平台上传器共享的常量与校验方法。子类（TmallVideo / JDVideo）
    通过调用类方法 validate_video_file / validate_image_file / validate_publish_date
    在执行浏览器自动化前完成基础参数校验，避免无效请求占用浏览器进程。
    """

    # 支持的视频文件扩展名（小写，含点号）。覆盖主流视频容器格式。
    SUPPORTED_VIDEO_EXTENSIONS = {
        ".mp4",
        ".mov",
        ".avi",
        ".mkv",
        ".m4v",
        ".webm",
        ".flv",
        ".wmv",
    }

    # 支持的图片文件扩展名（小写，含点号）。用于天猫自定义封面图片校验。
    SUPPORTED_IMAGE_EXTENSIONS = {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".bmp",
    }

    # 定时发布时间至少晚于当前时间的时长。天猫与京东平台均要求至少 2 小时提前量，
    # 排队后不足 2 小时的任务会在打开发布页前被停止。
    MIN_SCHEDULE_LEAD_TIME = timedelta(hours=2)

    @classmethod
    def validate_video_file(cls, file_path: str | Path) -> Path:
        """校验视频文件存在且扩展名为支持格式。

        :param file_path: 视频文件路径，支持 str 或 Path
        :returns: 解析后的绝对路径（已展开 ~ 与符号链接）
        :raises FileNotFoundError: 文件不存在
        :raises ValueError: 路径不是文件，或扩展名不在 SUPPORTED_VIDEO_EXTENSIONS 中
        """
        path = Path(file_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"视频文件不存在: {path}")
        if not path.is_file():
            raise ValueError(f"视频路径不是文件: {path}")
        if path.suffix.lower() not in cls.SUPPORTED_VIDEO_EXTENSIONS:
            raise ValueError(
                f"不支持的视频格式: {path.suffix}，当前支持: {', '.join(sorted(cls.SUPPORTED_VIDEO_EXTENSIONS))}"
            )

        return path

    @classmethod
    def validate_image_file(cls, file_path: str | Path) -> Path:
        """校验图片文件存在且扩展名为支持格式（用于天猫自定义封面）。

        :param file_path: 图片文件路径，支持 str 或 Path
        :returns: 解析后的绝对路径
        :raises FileNotFoundError: 文件不存在
        :raises ValueError: 路径不是文件，或扩展名不在 SUPPORTED_IMAGE_EXTENSIONS 中
        """
        path = Path(file_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"图片文件不存在: {path}")
        if not path.is_file():
            raise ValueError(f"图片路径不是文件: {path}")
        if path.suffix.lower() not in cls.SUPPORTED_IMAGE_EXTENSIONS:
            raise ValueError(
                f"不支持的图片格式: {path.suffix}，当前支持: {', '.join(sorted(cls.SUPPORTED_IMAGE_EXTENSIONS))}"
            )
        return path

    @classmethod
    def validate_publish_date(cls, publish_date: datetime | int | None) -> datetime | int:
        """校验定时发布时间有效性。

        :param publish_date: datetime 表示定时发布；None 或 0 表示立即发布
        :returns: 原值（None/0 返回 0，datetime 原样返回）
        :raises TypeError: 类型不是 datetime 且不为 None/0
        :raises ValueError: 时间不晚于当前，或晚于当前但不足 2 小时提前量

        说明：当 publish_date 携带时区时使用相同时区的当前时间比较，
        否则使用本地当前时间。
        """
        if publish_date in (None, 0):
            return 0

        if not isinstance(publish_date, datetime):
            raise TypeError("publish_date 必须是 datetime 类型或 0")

        # 与目标时间保持相同时区基准的比较，避免跨时区误判
        now = datetime.now(tz=publish_date.tzinfo) if publish_date.tzinfo else datetime.now()
        if publish_date <= now:
            raise ValueError("定时发布时间必须晚于当前时间")

        min_publish_time = now + cls.MIN_SCHEDULE_LEAD_TIME
        if publish_date <= min_publish_time:
            raise ValueError("定时发布时间必须大于当前时间 2 小时")

        return publish_date
