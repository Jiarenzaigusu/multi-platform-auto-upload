# -*- coding: utf-8 -*-
"""
webapp.api.models 模块

发布请求的校验与数据类定义。

提供：
- 常量：支持的平台、视频/图片扩展名、账号名正则、创作者声明枚举等
- ValidationError: 校验异常
- PublishRequest: 校验后的发布请求数据类
- 校验函数：validate_platform / validate_account_name / parse_tags / parse_goods_ids /
            parse_schedule / validate_publish_request

校验逻辑保证请求在进入浏览器自动化前就符合平台约束，避免无效请求占用浏览器进程。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


# 支持的平台
SUPPORTED_PLATFORMS = {"tmall", "jd"}
# 支持的视频和图文内容类型。
SUPPORTED_CONTENT_TYPES = {"video", "article"}
# 支持的视频扩展名
SUPPORTED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".m4v", ".avi", ".webm"}
# 支持的封面图片扩展名
SUPPORTED_COVER_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
JD_ARTICLE_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
MAX_JD_ARTICLE_IMAGES = 20
MAX_JD_ARTICLE_IMAGE_BYTES = 5 * 1024 * 1024
# 账号名正则：字母数字下划线连字符，1-64 字符
ACCOUNT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# 定时发布时间标准格式
SCHEDULE_FORMAT = "%Y-%m-%d %H:%M"
# 定时发布时间至少晚于当前的时间（与平台要求一致）
MIN_SCHEDULE_LEAD_TIME = timedelta(hours=2)
# 音乐名称最大长度
MAX_MUSIC_NAME_LENGTH = 100
# 天猫一次最多关联的商品 ID 数
MAX_TMALL_GOODS_IDS = 6
# 京东链接导入一次最多关联的商品 ID 数（平台页面显示 0/10）
MAX_JD_GOODS_IDS = 10
# 天猫创作者声明的真实页面选项。
TMALL_CREATOR_DECLARATIONS = (
    "内容无需标注",
    "内容含营销信息",
    "含AI生成内容",
    "含虚构演绎内容",
    "内容为转载",
    "个人观点，仅供参考",
)
# 京东创作者声明的真实页面选项。与天猫独立维护，禁止跨平台文案映射。
JD_CREATOR_DECLARATIONS = (
    "内容无需标注",
    "内容含营销广告",
    "含AI生成内容",
    "含虚构演绎内容",
    "内容为转载",
    "个人观点，仅供参考",
)
# 仅天猫兼容已下载旧模板的营销声明；京东始终要求其后台真实字段。
TMALL_CREATOR_DECLARATION_ALIASES = {"内容含营销广告": "内容含营销信息"}
# 保留旧名称，供天猫模块和第三方调用方继续使用。
CREATOR_DECLARATIONS = TMALL_CREATOR_DECLARATIONS


class ValidationError(ValueError):
    """Web 表单无法安全映射到上传器请求时抛出。"""


@dataclass(frozen=True, slots=True)
class PublishRequest:
    """校验后的发布请求（不可变）。

    frozen=True 使实例不可变，slots=True 减少内存占用。
    所有平台字段统一在此结构中，京东不需要的字段（如 tags/music_name）留空。
    """

    platform: str               # 平台 tmall/jd
    content_type: str           # 内容类型 video/article
    account: str                # 店铺账号标识
    video_path: Path | None     # 视频文件绝对路径（仅视频）
    image_paths: tuple[Path, ...]  # 图片绝对路径（仅图文，顺序即发布顺序）
    cover_image_path: Path | None  # 视频自定义封面图片路径（可为 None）
    original_filename: str      # 原始文件名（用于日志/结果）
    title: str                  # 标题
    description: str            # 描述/文案（天猫有，京东无）
    tags: tuple[str, ...]       # 话题标签（天猫有，京东无）
    goods_id: str               # 商品 ID 字符串（逗号分隔）
    activity_topic: str         # 参与话题（天猫活动话题 / 京东话题）
    music_name: str             # 音乐名称（天猫有，京东无）
    creator_declaration: str    # 创作者声明
    schedule: datetime | None   # 定时发布时间，None 立即发布
    original: bool              # 是否开启自主原创（仅京东）
    dry_run: bool               # 是否流程验证模式
    headed: bool                # 是否显示浏览器
    managed_upload: bool        # 是否由 Web 应用管理上传文件副本


def validate_platform(platform: str) -> str:
    """校验平台标识，返回归一化后的值。"""
    normalized = platform.strip().lower()
    if normalized not in SUPPORTED_PLATFORMS:
        raise ValidationError("当前仅支持天猫光合（tmall）和京东京麦（jd）")
    return normalized


def validate_content_type(content_type: str) -> str:
    """校验内容类型，返回归一化后的值。"""
    normalized = content_type.strip().lower()
    if normalized not in SUPPORTED_CONTENT_TYPES:
        raise ValidationError("当前仅支持视频（video）或图文（article）内容类型")
    return normalized


def validate_account_name(account: str) -> str:
    """校验账号名，返回去除首尾空白后的值。"""
    normalized = account.strip()
    if not ACCOUNT_NAME_PATTERN.fullmatch(normalized):
        raise ValidationError("账号标识只能包含字母、数字、下划线和连字符，长度为 1-64")
    return normalized


def parse_tags(raw_tags: str) -> tuple[str, ...]:
    """解析话题标签字符串，逗号分隔，去 # 前缀，过滤空项。

    :raises ValidationError: 标签数超过 4 个
    """
    tags = tuple(tag.strip().lstrip("#") for tag in raw_tags.split(",") if tag.strip().lstrip("#"))
    if len(tags) > 4:
        raise ValidationError("天猫最多支持 4 个标签")
    return tags


def parse_goods_ids(raw_goods_ids: str) -> tuple[str, ...]:
    """解析商品 ID 字符串，支持逗号/空白/换行分隔，去除首尾引号并去重保序。

    :raises ValidationError: 任一 ID 不是纯数字
    """
    quote_chars = "'\"‘’“”"
    values = (
        value.strip(quote_chars)
        for value in re.split(r"[,，\s]+", raw_goods_ids.strip())
        if value.strip(quote_chars)
    )
    goods_ids = tuple(dict.fromkeys(values))
    if any(not goods_id.isdigit() for goods_id in goods_ids):
        raise ValidationError("商品 ID 必须为纯数字，多个 ID 请使用逗号或换行分隔")
    return goods_ids


# 中文定时格式正则："2030年12月31日 14点30分"
_SCHEDULE_CN_PATTERN = re.compile(
    r"^(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
    r"\s+(\d{1,2})\s*点\s*(\d{1,2})\s*分\s*$"
)

SCHEDULE_FORMATS_HELP = "YYYY年MM月DD日 HH点MM分 或 YYYY-MM-DD HH:MM"


def _build_schedule(raw_value: str, schedule: datetime) -> datetime:
    """校验定时时间晚于当前至少 2 小时。"""
    if schedule <= datetime.now() + MIN_SCHEDULE_LEAD_TIME:
        raise ValidationError("定时发布时间必须至少晚于当前时间 2 小时")
    return schedule


def parse_schedule(raw_schedule: str) -> datetime | None:
    """解析定时发布时间字符串，支持标准格式与中文格式。

    :returns: datetime 或 None（空字符串）
    :raises ValidationError: 格式无效或时间不足 2 小时提前量
    """
    value = raw_schedule.strip()
    if not value:
        return None

    # 1) 标准格式 YYYY-MM-DD HH:MM
    try:
        dt = datetime.strptime(value, SCHEDULE_FORMAT)
    except ValueError:
        dt = None

    if dt is not None:
        return _build_schedule(value, dt)

    # 2) 中文格式 2030年12月31日 14点30分
    cn_match = _SCHEDULE_CN_PATTERN.match(value)
    if cn_match:
        year, month, day, hour, minute = map(int, cn_match.groups())
        try:
            return _build_schedule(value, datetime(year, month, day, hour, minute))
        except ValueError as exc:
            raise ValidationError("定时发布时间日期无效") from exc

    raise ValidationError(f"定时发布时间格式应为 {SCHEDULE_FORMATS_HELP}")


def validate_publish_request(
    *,
    platform: str,
    account: str,
    content_type: str = "video",
    video_path: Path | None = None,
    image_paths: tuple[Path, ...] = (),
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
) -> PublishRequest:
    """校验并构造 PublishRequest。

    包含通用校验（平台/账号/素材/封面/创作者声明）与平台专属校验：
    - 天猫：标题最多 30 字，文案+标签最多 1000 字，不支持自主原创，最多 6 个商品 ID
    - 京东：视频标题 5-27 字且无独立文案；图文标题 5-20 字并支持正文；二者均不支持标签/音乐，支持一个可选话题、自主原创，最多 10 个商品 ID

    :returns: 校验后的 PublishRequest
    :raises ValidationError: 任一校验失败
    """
    selected_platform = validate_platform(platform)
    selected_content_type = validate_content_type(content_type)
    selected_account = validate_account_name(account)
    normalized_title = title.strip()
    normalized_description = description.strip()
    goods_ids = parse_goods_ids(goods_id)
    normalized_activity_topic = activity_topic.strip()
    music_name = raw_music_name.strip()
    creator_declaration = raw_creator_declaration.strip()
    if selected_platform == "tmall":
        creator_declaration = TMALL_CREATOR_DECLARATION_ALIASES.get(
            creator_declaration, creator_declaration
        )

    normalized_video_path: Path | None = None
    normalized_image_paths: tuple[Path, ...] = ()
    if selected_content_type == "video":
        if video_path is None or not video_path.is_file():
            raise ValidationError("视频文件不存在或上传未完成")
        try:
            if video_path.stat().st_size == 0:
                raise ValidationError("视频文件为空")
        except OSError as exc:
            raise ValidationError("无法读取视频文件") from exc
        if video_path.suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS:
            raise ValidationError("仅支持 MP4、MOV、MKV、M4V、AVI 或 WebM 视频")
        normalized_video_path = video_path.resolve()
    else:
        max_images = 9 if selected_platform == "tmall" else MAX_JD_ARTICLE_IMAGES
        if not 1 <= len(image_paths) <= max_images:
            raise ValidationError(
                "天猫图文必须上传 1-9 张图片" if selected_platform == "tmall"
                else "京东图文必须上传 1-20 张图片"
            )
        normalized_paths: list[Path] = []
        for image_path in image_paths:
            if not image_path.is_file():
                raise ValidationError("图文图片不存在或上传未完成")
            try:
                if image_path.stat().st_size == 0:
                    raise ValidationError("图文图片不能为空")
            except OSError as exc:
                raise ValidationError("无法读取图文图片") from exc
            allowed_extensions = (
                SUPPORTED_COVER_IMAGE_EXTENSIONS if selected_platform == "tmall"
                else JD_ARTICLE_IMAGE_EXTENSIONS
            )
            if image_path.suffix.lower() not in allowed_extensions:
                raise ValidationError(
                    "图文图片仅支持 JPG、PNG 或 WebP 格式" if selected_platform == "tmall"
                    else "京东图文图片仅支持 JPG 或 PNG 格式"
                )
            if selected_platform == "jd" and image_path.stat().st_size > MAX_JD_ARTICLE_IMAGE_BYTES:
                raise ValidationError("京东图文单张图片不能超过 5 MiB")
            normalized_paths.append(image_path.resolve())
        normalized_image_paths = tuple(normalized_paths)

    # 视频自定义封面校验（天猫与京东均支持）
    if cover_image_path is not None:
        if selected_content_type != "video":
            raise ValidationError("自定义封面图片仅支持视频发布")
        if not cover_image_path.is_file():
            raise ValidationError("封面图片不存在或上传未完成")
        try:
            if cover_image_path.stat().st_size == 0:
                raise ValidationError("封面图片为空")
        except OSError as exc:
            raise ValidationError("无法读取封面图片") from exc
        if cover_image_path.suffix.lower() not in SUPPORTED_COVER_IMAGE_EXTENSIONS:
            raise ValidationError("封面图片仅支持 JPG、PNG 或 WebP 格式")

    # 标题校验
    if not normalized_title:
        raise ValidationError("标题不能为空")

    # 创作者声明校验
    platform_creator_declarations = (
        TMALL_CREATOR_DECLARATIONS
        if selected_platform == "tmall"
        else JD_CREATOR_DECLARATIONS
    )
    if creator_declaration not in platform_creator_declarations:
        raise ValidationError("请选择有效的创作者声明")

    # 音乐名称长度校验
    if len(music_name) > MAX_MUSIC_NAME_LENGTH:
        raise ValidationError(f"音乐名称最多 {MAX_MUSIC_NAME_LENGTH} 个字符")

    # 定时发布时间解析
    schedule = parse_schedule(raw_schedule)
    tags = parse_tags(raw_tags) if selected_platform == "tmall" else ()

    # 平台专属校验
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
        # 京东专属校验
        title_max = 27 if selected_content_type == "video" else 20
        if not 5 <= len(normalized_title) <= title_max:
            raise ValidationError(f"京东{'视频' if selected_content_type == 'video' else '图文'}标题长度必须为 5-{title_max} 个字符")
        if selected_content_type == "video" and normalized_description:
            raise ValidationError("当前京东视频发布器没有独立文案字段，请清空文案")
        if selected_content_type == "article" and len(normalized_description) > 1001:
            raise ValidationError("京东图文正文最多 1001 个字符")
        if raw_tags.strip():
            raise ValidationError("当前京东发布器不支持标签字段")
        if music_name:
            raise ValidationError("当前京东发布器不支持音乐字段")
        if len(goods_ids) > MAX_JD_GOODS_IDS:
            raise ValidationError(f"京东一次最多关联 {MAX_JD_GOODS_IDS} 个商品 ID")

    return PublishRequest(
        platform=selected_platform,
        content_type=selected_content_type,
        account=selected_account,
        video_path=normalized_video_path,
        image_paths=normalized_image_paths,
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
