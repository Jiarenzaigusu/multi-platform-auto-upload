# -*- coding: utf-8 -*-
"""webapp.ai_copy.errors 模块：AI 文案功能的统一异常体系。

每类异常绑定 HTTP 状态码，路由层统一映射为对应状态码响应。
"""


class AiCopyError(Exception):
    """AI 文案功能所有异常的基类，默认 500。"""

    status_code = 500


class ProductLookupError(AiCopyError):
    """商品链接读取失败，502。"""
    status_code = 502


class LLMResponseError(AiCopyError):
    """LLM 响应不符合契约（格式/长度/内容违规），502。"""
    status_code = 502


class SellingPointCatalogError(AiCopyError):
    """卖点 Excel 上传/解析错误，400。"""
    status_code = 400


class SellingPointCatalogNotFoundError(SellingPointCatalogError):
    """卖点目录不存在或已过期，404。"""
    status_code = 404
