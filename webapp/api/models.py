from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


SUPPORTED_PLATFORMS = {"tmall", "jd"}
SUPPORTED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".m4v", ".avi", ".webm"}
SUPPORTED_COVER_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
ACCOUNT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SCHEDULE_FORMAT = "%Y-%m-%d %H:%M"
MIN_SCHEDULE_LEAD_TIME = timedelta(hours=2)
MAX_MUSIC_NAME_LENGTH = 100
MAX_TMALL_GOODS_IDS = 6
CREATOR_DECLARATIONS = (
    "内容无需标注",
    "内容含营销广告",
    "含AI生成内容",
    "含虚构演绎内容",
    "内容为转载",
    "个人观点，仅供参考",
)


class ValidationError(ValueError):
    """Raised when a web form cannot safely be mapped to an uploader request."""


@dataclass(frozen=True, slots=True)
class PublishRequest:
    platform: str
    account: str
    video_path: Path
    cover_image_path: Path | None
    original_filename: str
    title: str
    description: str
    tags: tuple[str, ...]
    goods_id: str
    activity_topic: str
    music_name: str
    creator_declaration: str
    schedule: datetime | None
    original: bool
    dry_run: bool
    headed: bool
    managed_upload: bool


def validate_platform(platform: str) -> str:
    normalized = platform.strip().lower()
    if normalized not in SUPPORTED_PLATFORMS:
        raise ValidationError("当前仅支持天猫光合（tmall）和京东京麦（jd）")
    return normalized


def validate_account_name(account: str) -> str:
    normalized = account.strip()
    if not ACCOUNT_NAME_PATTERN.fullmatch(normalized):
        raise ValidationError("账号标识只能包含字母、数字、下划线和连字符，长度为 1-64")
    return normalized


def parse_tags(raw_tags: str) -> tuple[str, ...]:
    tags = tuple(tag.strip().lstrip("#") for tag in raw_tags.split(",") if tag.strip().lstrip("#"))
    if len(tags) > 4:
        raise ValidationError("天猫最多支持 4 个标签")
    return tags


def parse_goods_ids(raw_goods_ids: str) -> tuple[str, ...]:
    """Parse comma-, whitespace-, or newline-separated IDs without duplicates."""
    values = (
        value
        for value in re.split(r"[,，\s]+", raw_goods_ids.strip())
        if value
    )
    goods_ids = tuple(dict.fromkeys(values))
    if any(not goods_id.isdigit() for goods_id in goods_ids):
        raise ValidationError("商品 ID 必须为纯数字，多个 ID 请使用逗号或换行分隔")
    return goods_ids


def parse_schedule(raw_schedule: str) -> datetime | None:
    value = raw_schedule.strip()
    if not value:
        return None
    try:
        schedule = datetime.strptime(value, SCHEDULE_FORMAT)
    except ValueError as exc:
        raise ValidationError("定时发布时间格式应为 YYYY-MM-DD HH:MM") from exc
    if schedule <= datetime.now() + MIN_SCHEDULE_LEAD_TIME:
        raise ValidationError("定时发布时间必须至少晚于当前时间 2 小时")
    return schedule


def validate_publish_request(
    *,
    platform: str,
    account: str,
    video_path: Path,
    cover_image_path: Path | None = None,
    original_filename: str,
    title: str,
    description: str = "",
    raw_tags: str = "",
    goods_id: str = "",
    activity_topic: str = "",
    raw_music_name: str = "",
    raw_creator_declaration: str = "内容无需标注",
    raw_schedule: str = "",
    original: bool = False,
    dry_run: bool = False,
    headed: bool = True,
    managed_upload: bool = False,
    verify_video_file: bool = True,
) -> PublishRequest:
    selected_platform = validate_platform(platform)
    selected_account = validate_account_name(account)
    normalized_title = title.strip()
    normalized_description = description.strip()
    goods_ids = parse_goods_ids(goods_id)
    normalized_activity_topic = activity_topic.strip()
    music_name = raw_music_name.strip()
    creator_declaration = raw_creator_declaration.strip()

    if verify_video_file:
        if not video_path.is_file():
            raise ValidationError("视频文件不存在或上传未完成")
        try:
            if video_path.stat().st_size == 0:
                raise ValidationError("视频文件为空")
        except OSError as exc:
            raise ValidationError("无法读取视频文件") from exc
    if video_path.suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS:
        raise ValidationError("仅支持 MP4、MOV、MKV、M4V、AVI 或 WebM 视频")
    if cover_image_path is not None:
        if selected_platform != "tmall":
            raise ValidationError("当前仅天猫光合支持自定义封面图片")
        if not cover_image_path.is_file():
            raise ValidationError("封面图片不存在或上传未完成")
        try:
            if cover_image_path.stat().st_size == 0:
                raise ValidationError("封面图片为空")
        except OSError as exc:
            raise ValidationError("无法读取封面图片") from exc
        if cover_image_path.suffix.lower() not in SUPPORTED_COVER_IMAGE_EXTENSIONS:
            raise ValidationError("封面图片仅支持 JPG、PNG 或 WebP 格式")
    if not normalized_title:
        raise ValidationError("标题不能为空")
    if creator_declaration not in CREATOR_DECLARATIONS:
        raise ValidationError("请选择有效的创作者声明")
    if len(music_name) > MAX_MUSIC_NAME_LENGTH:
        raise ValidationError(f"音乐名称最多 {MAX_MUSIC_NAME_LENGTH} 个字符")

    schedule = parse_schedule(raw_schedule)
    tags = parse_tags(raw_tags) if selected_platform == "tmall" else ()

    if selected_platform == "tmall":
        if len(normalized_title) > 30:
            raise ValidationError("天猫标题最多 30 个字符")
        tag_text = "".join(f" #{tag}" for tag in tags)
        if len(normalized_description + tag_text) > 1000:
            raise ValidationError("天猫文案与标签合计最多 1000 个字符")
        if original:
            raise ValidationError("天猫发布不支持自主原创开关")
        if len(goods_ids) > MAX_TMALL_GOODS_IDS:
            raise ValidationError(f"天猫一次最多关联 {MAX_TMALL_GOODS_IDS} 个商品 ID")
    else:
        if not 5 <= len(normalized_title) <= 27:
            raise ValidationError("京东标题长度必须为 5-27 个字符")
        if normalized_description:
            raise ValidationError("当前京东发布器没有独立文案字段，请清空文案")
        if raw_tags.strip():
            raise ValidationError("当前京东发布器不支持标签字段")
        if normalized_activity_topic:
            raise ValidationError("当前京东发布器不支持活动话题")
        if music_name:
            raise ValidationError("当前京东发布器不支持音乐字段")
        if len(goods_ids) > 1:
            raise ValidationError("京东一次只能关联 1 个商品 ID")

    return PublishRequest(
        platform=selected_platform,
        account=selected_account,
        video_path=video_path.resolve(),
        cover_image_path=cover_image_path.resolve() if cover_image_path else None,
        original_filename=original_filename,
        title=normalized_title,
        description=normalized_description,
        tags=tags,
        goods_id=",".join(goods_ids),
        activity_topic=normalized_activity_topic,
        music_name=music_name,
        creator_declaration=creator_declaration,
        schedule=schedule,
        original=original,
        dry_run=dry_run,
        headed=headed,
        managed_upload=managed_upload,
    )
