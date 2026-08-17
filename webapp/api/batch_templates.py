# -*- coding: utf-8 -*-
"""批量 Excel 模板的路由分发器。

模板字段、示例数据与渲染逻辑分别内聚在对应的平台内容模块中。本模块不保存
任何内容字段，唯一职责是依据平台和内容类型选择模板实现。
"""
from __future__ import annotations

from webapp.api.batch_jd_article import build_jd_article_template
from webapp.api.batch_jd_video import build_jd_video_template
from webapp.api.batch_tmall_article import build_tmall_article_template
from webapp.api.batch_tmall_video import build_tmall_video_template


def build_tmall_template(content_type: str = "video") -> bytes:
    """生成天猫指定内容类型的批量模板。"""
    if content_type == "video":
        return build_tmall_video_template()
    if content_type == "article":
        return build_tmall_article_template()
    raise ValueError("天猫批量发布仅支持视频或图文")


def build_jd_template() -> bytes:
    """生成京东视频批量模板。"""
    return build_jd_video_template()


def build_batch_template(platform: str, content_type: str = "video") -> bytes:
    """返回指定平台和内容类型的批量模板。"""
    if platform == "tmall":
        return build_tmall_template(content_type)
    if platform == "jd":
        if content_type == "video":
            return build_jd_template()
        if content_type == "article":
            return build_jd_article_template()
    raise ValueError(f"不支持的平台或内容类型: {platform}/{content_type}")
