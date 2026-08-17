# -*- coding: utf-8 -*-
"""京东京麦图文批量发布的能力边界。

京东图文批量发布尚未实现。独立模块集中维护这项产品限制，避免视频模块或路由
隐式承担图文分支。
"""
from __future__ import annotations


JD_ARTICLE_BATCH_UNAVAILABLE_MESSAGE = "京东图文批量发布尚未开放"


def reject_jd_article_batch() -> None:
    """统一抛出京东图文批量未开放错误。"""
    raise ValueError(JD_ARTICLE_BATCH_UNAVAILABLE_MESSAGE)


def build_jd_article_template() -> bytes:
    """京东图文尚未开放，因此没有可下载模板。"""
    reject_jd_article_batch()
