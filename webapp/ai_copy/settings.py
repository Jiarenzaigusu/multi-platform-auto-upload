# -*- coding: utf-8 -*-
"""webapp.ai_copy.settings 模块：AI 文案功能的配置参数。"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AiCopySettings:
    """AI 文案功能配置（不可变）。

    所有默认值均经过实测调优，覆盖：商品页面抓取超时与大小、缓存新鲜度、
    京东重试策略、天猫账号尝试次数、卖点 Excel 限制与目录 TTL。
    """

    product_timeout_seconds: float = 20        # 商品页抓取超时
    max_product_page_bytes: int = 1_500_000    # 商品页最大字节数（1.5MB）
    product_cache_seconds: float = 600          # 商品缓存新鲜期（10 分钟）
    product_stale_cache_seconds: float = 3_600  # 商品缓存兜底期（1 小时）
    jd_request_attempts: int = 5                # 京东请求重试次数
    jd_retry_base_seconds: float = 0.15         # 京东重试退避基数
    tmall_account_attempts: int = 2             # 天猫账号尝试次数
    max_selling_point_workbook_bytes: int = 5 * 1024 * 1024  # 卖点 Excel 最大 5MB
    max_selling_point_rows: int = 5000          # 卖点 Excel 最多 5000 行
    selling_point_catalog_ttl_seconds: float = 12 * 60 * 60  # 卖点目录 TTL 12 小时
    max_selling_point_catalogs: int = 8         # 内存最多保留 8 个卖点目录
