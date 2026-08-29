# -*- coding: utf-8 -*-
"""
uploader.tmall_video_uploader.main 模块

淘宝光合平台（creator.guanghe.taobao.com）视频发布器核心实现。

主要功能：
1. Cookie 校验：访问光合首页判断是否仍处于登录态
2. 手动登录：打开可见浏览器，等待用户完成扫码/密码/短信等验证后保存 storage_state
3. 视频发布：上传视频 → 设置封面 → 填写标题/描述/话题 → 参与活动 → 添加音乐 →
            关联商品 → 设置定时/立即发布 → 选择创作者声明 → 点击发布按钮 → 等待确认

注意事项：
- 不绕过任何平台安全验证，验证码需人工完成
- 图片库、裁剪层、花字层以独立 frame 呈现；所有操作都按其可见控件定位
- 发布结果可能"不确定"（按钮已点但 30 秒内无明确信号），
  此时会抛出 PublishResultUncertainError，由任务管理器标记为 uncertain 状态
"""
from __future__ import annotations

import asyncio
from io import BytesIO
import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from patchright.async_api import (
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from uploader.errors import PublishResultUncertainError
from utils.config import DEBUG_MODE
from uploader.base_video import BaseVideoUploader
from uploader.tmall_session import TmallBrowserSession
from utils.log import tmall_logger


# 淘宝光合创作者中心首页 URL，用于 Cookie 校验与登录入口
TMALL_CREATOR_HOME_URL = "https://creator.guanghe.taobao.com/page/"
# 淘宝光合视频发布页 URL，发布任务在此页面执行
TMALL_VIDEO_PUBLISH_URL = "https://creator.guanghe.taobao.com/page/pubNew/video?pub_url=https%3A%2F%2Fhuodong.taobao.com%2Fwow%2Fz%2Fguang%2Fgg_publish%2Fgg-video%3Fugc_scene%3Dpc_newcreator_video%26pageType%3Dvideo%26site%3Dguangguang&pub_scene=gg"
# 登录成功后的目标 host，URL 命中此 host 表示已进入光合后台
TMALL_LOGIN_SUCCESS_HOST = "creator.guanghe.taobao.com"
# 登录/鉴权相关 host，命中表示用户正在登录中（中间态）
TMALL_AUTH_HOSTS = {"passport.taobao.com"}
# 发布策略常量
TMALL_PUBLISH_STRATEGY_IMMEDIATE = "immediate"
TMALL_PUBLISH_STRATEGY_SCHEDULED = "scheduled"
# 天猫一次最多关联的商品 ID 数量
TMALL_MAX_GOODS_IDS = 6
# 天猫自定义封面支持的图片格式
TMALL_COVER_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
# 天猫封面编辑器展示的比例顺序与平台页面一致。
TMALL_COVER_RATIOS = ("original", "3:4", "1:1")
# 天猫自定义封面最大字节数（20 MiB）
TMALL_MAX_COVER_IMAGE_BYTES = 20 * 1024 * 1024
# 商品搜索结果为空时平台给出的提示文案（用于判定"无结果"而非"还在加载"）
TMALL_EMPTY_PRODUCT_RESULT_HINTS = (
    "暂无数据",
    "没有找到",
    "没有搜到",
    "暂无商品",
    "无结果",
    "暂无结果",
)


class TmallAuthenticationError(RuntimeError):
    """天猫 Cookie 已失效异常。

    当发布或校验流程中发现页面被重定向到登录页/鉴权页时抛出，
    上层会捕获此异常并将会话标记为未认证。
    """
    pass


def _msg(emoji: str, text: str) -> str:
    """统一日志格式：emoji + 文本。"""
    return f"{emoji} {text}"


def _cover_picker_upload_name(source_path: Path, now: datetime | None = None) -> str:
    """生成天猫素材库可见的唯一封面文件名。

    Excel 批量任务可能引用多个同名本地文件，而天猫素材库会长期保留历史素材。
    时间便于人工追溯，随机标识避免同一秒的并发任务发生碰撞。
    """
    timestamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    suffix = source_path.suffix.lower()
    return f"mpau-cover-{timestamp}-{uuid.uuid4().hex[:12]}{suffix}"


async def _click_visible_frame_button(
    frames: tuple,
    names: tuple[str, ...],
    *,
    description: str,
    timeout_seconds: int = 15,
    top_overlay_only: bool = False,
) -> None:
    """点击任一 frame 中当前可见的指定按钮，避免依赖浮层坐标。

    光合的图片库、裁剪和花字模块会动态新建 iframe，且按钮的 class 会随版本
    变化。调用方传入当前步骤所属的精确 frame，防止点击到已隐藏图库或底层表单中
    的同名按钮；按可访问名称定位比固定视口坐标更能适应缩放与页面布局变化。
    """
    for _ in range(timeout_seconds * 2):
        for candidate in frames:
            scope = candidate
            if top_overlay_only:
                opened = candidate.locator(".next-overlay-wrapper.opened")
                if await opened.count() == 0:
                    continue
                scope = opened.last
            buttons = scope.locator('button, [role="button"], a')
            for index in range(await buttons.count()):
                button = buttons.nth(index)
                if not await button.is_visible() or not await button.is_enabled():
                    continue
                actual_name = (await button.inner_text()).strip()
                if not actual_name:
                    actual_name = (await button.get_attribute("aria-label") or "").strip()
                # 图库确认会显示“确定（1）”，括号数字是已选素材数，不属于操作名称。
                normalized_name = re.sub(r"\s+", "", actual_name)
                normalized_name = re.sub(r"[（(]\d+[）)]$", "", normalized_name).strip()
                if normalized_name in names:
                    # Next 组件的文字节点偶尔覆盖按钮命中区域；仅在已确认可用后
                    # 强制点击实际按钮，不会绕过禁用态。
                    await button.click(force=True)
                    return
        await asyncio.sleep(0.5)
    # 平台改版时保留可见操作文案，便于只调整语义名称，不需要恢复坐标点击。
    visible_actions: list[str] = []
    for candidate in frames:
        try:
            scope = candidate
            if top_overlay_only:
                opened = candidate.locator(".next-overlay-wrapper.opened")
                if await opened.count() == 0:
                    continue
                scope = opened.last
            actions = await scope.evaluate(
                """() => [...document.querySelectorAll('button, [role="button"], a')]
                    .filter(element => {
                        const style = getComputedStyle(element);
                        const rect = element.getBoundingClientRect();
                        return style.display !== 'none' && style.visibility !== 'hidden'
                            && rect.width > 0 && rect.height > 0;
                    })
                    .map(element => (element.innerText || element.getAttribute('aria-label') || '')
                        .trim().replace(/\\s+/g, ' '))
                    .filter(Boolean).slice(0, 30)"""
            )
            if actions:
                visible_actions.extend(actions)
        except Exception:
            continue
    expected = "、".join(f"“{name}”" for name in names)
    available = "、".join(dict.fromkeys(visible_actions)) or "无可访问操作文案"
    raise RuntimeError(
        f"未找到可点击的{description}按钮（期望 {expected}；当前可见操作：{available}）"
    )


def _is_cover_card_gray(red: int, green: int, blue: int) -> bool:
    """返回截图中光合未选中比例卡片的背景色是否命中。"""
    return max(red, green, blue) - min(red, green, blue) <= 4 and 207 <= red <= 225


def _is_tmall_orange(red: int, green: int, blue: int) -> bool:
    """返回截图中光合主操作按钮、选中边框的橙色是否命中。"""
    return red >= 225 and 35 <= green <= 145 and blue <= 95 and red - green >= 105


def _find_cover_ratio_cards(screenshot: bytes) -> tuple[tuple[float, float, float, float], ...] | None:
    """从当前截图找到“原始 / 3:4 / 1:1”三个比例卡片。

    这层由平台渲染在 Patchright 不能访问的页面层中，不能通过 DOM 文本定位。
    这里仅识别三张并排、同尺寸的灰色卡片，不使用固定的屏幕坐标；页面缩放、
    窗口大小或弹窗位置变化时，点击位置会随截图重新计算。
    """
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - dependency is declared in pyproject.toml
        raise RuntimeError("缺少 Pillow，无法识别天猫封面比例卡片") from exc

    image = Image.open(BytesIO(screenshot)).convert("RGB")
    width, height = image.size
    min_card_width = max(24, width // 45)
    candidates: list[tuple[int, tuple[tuple[int, int], ...]]] = []

    # 比例卡片位于弹窗内容区，排除顶部导航与底部按钮区域可降低误判概率。
    for y in range(height // 4, height * 4 // 5, max(1, height // 700)):
        runs: list[tuple[int, int]] = []
        start = None
        for x in range(width):
            if _is_cover_card_gray(*image.getpixel((x, y))):
                if start is None:
                    start = x
            elif start is not None:
                if x - start >= min_card_width:
                    runs.append((start, x - 1))
                start = None
        if start is not None and width - start >= min_card_width:
            runs.append((start, width - 1))

        for index in range(len(runs) - 2):
            group = tuple(runs[index:index + 3])
            card_widths = [end - start + 1 for start, end in group]
            average_width = sum(card_widths) / 3
            gaps = [group[item + 1][0] - group[item][1] - 1 for item in range(2)]
            # 真实卡片是三个近似等宽、间隔适中的相邻矩形；同时限制在中右侧
            # 以避免把封面预览、页面侧边栏等灰色区域识别为比例设置。
            if (
                min(card_widths) >= average_width * 0.75
                and max(card_widths) <= average_width * 1.3
                and all(average_width * 0.15 <= gap <= average_width * 1.5 for gap in gaps)
                and group[0][0] >= width * 0.3
                and group[-1][1] <= width * 0.93
            ):
                candidates.append((y, group))

    if not candidates:
        return None

    # 同一组卡片会在许多相邻扫描行重复命中。选出横向总宽度最大的完整三卡组，
    # 可避开文字和图标把卡片背景切成小段的扫描行，再用所有同组行的中位数确定
    # 纵向点击位置，确保落在实心区域而不是边框上。
    candidate_y, candidate_runs = max(
        candidates,
        key=lambda item: sum(end - start + 1 for start, end in item[1]),
    )
    same_runs_y = sorted(
        y
        for y, runs in candidates
        if all(
            abs(runs[index][0] - candidate_runs[index][0]) <= 2
            and abs(runs[index][1] - candidate_runs[index][1]) <= 2
            for index in range(3)
        )
    )
    if same_runs_y:
        candidate_y = same_runs_y[len(same_runs_y) // 2]
    return tuple((float(start), float(candidate_y), float(end), float(candidate_y)) for start, end in candidate_runs)


def _find_cover_next_button(screenshot: bytes) -> tuple[float, float] | None:
    """从截图定位“下一步”的橙色实心按钮中心，避免以固定坐标点击。"""
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - dependency is declared in pyproject.toml
        raise RuntimeError("缺少 Pillow，无法识别天猫封面下一步按钮") from exc

    image = Image.open(BytesIO(screenshot)).convert("RGB")
    width, height = image.size
    min_segment_width = max(28, width // 70)
    rows: list[tuple[int, int, int]] = []
    for y in range(height // 2, height):
        longest = (0, 0)
        start = None
        for x in range(width):
            if _is_tmall_orange(*image.getpixel((x, y))):
                if start is None:
                    start = x
            elif start is not None:
                if x - start > longest[1] - longest[0]:
                    longest = (start, x)
                start = None
        if start is not None and width - start > longest[1] - longest[0]:
            longest = (start, width)
        if longest[1] - longest[0] >= min_segment_width:
            rows.append((y, longest[0], longest[1]))

    # 选中比例卡片只有细橙色边框；“下一步”是连续多行的实心橙色区域。
    groups: list[list[tuple[int, int, int]]] = []
    for row in rows:
        if groups and row[0] <= groups[-1][-1][0] + 2:
            groups[-1].append(row)
        else:
            groups.append([row])
    filled_groups = [group for group in groups if len(group) >= max(8, height // 100)]
    if not filled_groups:
        return None
    button_rows = max(filled_groups, key=len)
    return (
        sum((start + end) / 2 for _, start, end in button_rows) / len(button_rows),
        sum(y for y, _, _ in button_rows) / len(button_rows),
    )


def _ratio_card_is_selected(
    screenshot: bytes,
    card: tuple[float, float, float, float],
) -> bool:
    """确认动态定位到的目标比例卡片周边出现了平台的橙色选中边框。"""
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - dependency is declared in pyproject.toml
        raise RuntimeError("缺少 Pillow，无法确认天猫封面比例") from exc

    image = Image.open(BytesIO(screenshot)).convert("RGB")
    width, height = image.size
    left, center_y, right, _ = card
    margin = max(4, int((right - left + 1) * 0.08))
    top = max(0, int(center_y - (right - left + 1) * 0.8))
    bottom = min(height, int(center_y + (right - left + 1) * 0.8))
    orange_pixels = 0
    for y in range(top, bottom):
        for x in range(max(0, int(left) - margin), min(width, int(right) + margin + 1)):
            if _is_tmall_orange(*image.getpixel((x, y))):
                orange_pixels += 1
                if orange_pixels >= max(10, int(right - left + 1) // 3):
                    return True
    return False


async def _select_cover_ratio_and_continue(
    page: Page,
    ratio: str,
) -> None:
    """按平台比例卡片顺序确认比例并继续；原始比例可沿用平台默认值。"""
    if ratio not in TMALL_COVER_RATIOS:
        raise ValueError(f"不支持的天猫封面比例: {ratio}")
    screenshot = await page.screenshot(type="png")
    viewport = await page.evaluate(
        "() => ({ width: window.innerWidth, height: window.innerHeight })"
    )
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - dependency is declared in pyproject.toml
        raise RuntimeError("缺少 Pillow，无法识别天猫封面比例卡片") from exc
    image_width, image_height = Image.open(BytesIO(screenshot)).size
    selected_screenshot = screenshot
    if ratio != "original":
        cards = _find_cover_ratio_cards(screenshot)
        if cards is None:
            raise RuntimeError("未识别到天猫封面比例卡片，已停止避免使用固定坐标")
        # 平台展示顺序固定为“原始 / 3:4 / 1:1”。
        target_card = cards[TMALL_COVER_RATIOS.index(ratio)]
        target_x = (
            (target_card[0] + target_card[2]) / 2
            * viewport["width"]
            / image_width
        )
        target_y = target_card[1] * viewport["height"] / image_height
        await page.mouse.click(target_x, target_y, delay=150)
        await asyncio.sleep(1)
        selected_screenshot = await page.screenshot(type="png")
        if not _ratio_card_is_selected(selected_screenshot, target_card):
            raise RuntimeError(f"未确认已选择 {ratio} 封面比例，已停止避免继续错误裁剪")

    next_button = _find_cover_next_button(selected_screenshot)
    if next_button is None:
        raise RuntimeError("未识别到天猫封面裁剪页的“下一步”按钮，已停止避免使用固定坐标")
    next_x = next_button[0] * viewport["width"] / image_width
    next_y = next_button[1] * viewport["height"] / image_height
    await page.mouse.click(next_x, next_y, delay=150)
    # 平台切换至花字确认层需要短暂动画；不再为该等待重复截图。
    await asyncio.sleep(1.5)


async def _upload_picker_file(page: Page, picker_frame, cover_path: Path) -> None:
    """点击图库 iframe 内的“本地上传”控件并向文件选择器设置封面。"""
    upload_button = picker_frame.get_by_text("本地上传", exact=True).first
    try:
        async with page.expect_file_chooser(timeout=10000) as chooser_info:
            await upload_button.click(timeout=10000)
        file_chooser = await chooser_info.value
        await file_chooser.set_files(str(cover_path))
        return
    except PlaywrightTimeoutError:
        # 视频封面和图文一样，部分账号的首次“本地上传”只打开“上传素材”
        # 二级弹窗；真正的 file chooser 在其中的批量导入控件上。
        nested_upload_button = picker_frame.locator("#sucai-tu-upload")
        try:
            await nested_upload_button.wait_for(state="visible", timeout=10000)
            async with page.expect_file_chooser(timeout=10000) as chooser_info:
                await nested_upload_button.click(force=True, timeout=10000)
            file_chooser = await chooser_info.value
            await file_chooser.set_files(str(cover_path))
        except PlaywrightTimeoutError as nested_exc:
            raise RuntimeError("天猫图片库的“本地上传”控件未打开文件选择器") from nested_exc


def _build_login_result(
    success: bool,
    status: str,
    message: str,
    account_file: str,
    current_url: str = "",
) -> dict:
    """构造登录流程的统一返回结构。"""
    return {
        "success": success,
        "status": status,
        "message": message,
        "account_file": str(account_file),
        "current_url": current_url,
    }


def _url_host(url: str) -> str:
    """提取 URL 的 host 部分，解析失败返回空字符串。"""
    try:
        return urlparse(url).hostname or ""
    except Exception:
        return ""


def _is_login_page_url(url: str) -> str:
    """判断 URL 是否为淘宝登录页（login.taobao.com 或 path 以 /login/ 开头）。"""
    host = _url_host(url)
    path = urlparse(url).path if url else ""
    return host == "login.taobao.com" or path.startswith("/login/")


def _is_auth_page_url(url: str) -> bool:
    """判断 URL 是否处于鉴权中间态（如 passport.taobao.com）。"""
    return _url_host(url) in TMALL_AUTH_HOSTS


def _is_tmall_creator_home(url: str) -> bool:
    """判断 URL 是否已进入光合后台（host == creator.guanghe.taobao.com）。"""
    return _url_host(url) == TMALL_LOGIN_SUCCESS_HOST


def _contains_exact_product_id(markup: str, product_id: str) -> bool:
    """检查商品卡片的 HTML 标记中是否包含精确的商品 ID（避免部分匹配）。

    使用前后负向断言确保 product_id 不被更长数字串包含。
    """
    return re.search(rf"(?<!\d){re.escape(product_id)}(?!\d)", markup) is not None


def _product_href_has_exact_id(href: str | None, product_id: str) -> bool:
    """匹配淘宝商品链接的 id 查询参数，避免部分 ID 匹配。

    商品卡片标记不再暴露 ID 时，通过商品链接的 ?id=xxx 参数精确匹配。
    """
    if not href:
        return False
    try:
        return product_id in parse_qs(urlparse(href).query).get("id", [])
    except ValueError:
        return False


def _has_explicit_empty_product_result(text: str) -> bool:
    """检查结果区文本是否包含明确的"无结果"提示。

    注意："没有更多了"只表示非空列表的末尾，不算"无结果"。
    """
    return any(hint in text for hint in TMALL_EMPTY_PRODUCT_RESULT_HINTS)


def _normalize_option_text(text: str) -> str:
    """去除所有空白字符，用于创作者声明选项的模糊匹配。"""
    return re.sub(r"\s+", "", text)


def _normalized_goods_ids(value: str) -> tuple[str, ...]:
    """解析商品 ID 字符串，支持逗号、中文逗号、空格、换行分隔。

    去重并保持原顺序，返回元组。
    """
    parts = (part for part in re.split(r"[,，\s]+", value.strip()) if part)
    return tuple(dict.fromkeys(parts))


def _two_character_chunks(text: str) -> tuple[str, ...]:
    """将文本按每 2 个字符一组切分，用于音乐搜索的增量输入。

    平台音乐搜索框对快速输入响应不佳，每次追加 2 个字符并等待异步搜索
    完成后再输入下一组，可提高搜索命中率。
    """
    return tuple(text[index : index + 2] for index in range(0, len(text), 2))


async def _cookie_auth_in_context(context: BrowserContext) -> bool:
    """在指定 BrowserContext 中校验天猫 Cookie 是否有效。

    访问光合首页，最多等待 5 轮（每轮 2 秒）观察 URL 是否进入光合后台。
    若停留在登录页则返回 False，进入后台返回 True。
    """
    page = await context.new_page()
    try:
        await page.goto(TMALL_CREATOR_HOME_URL, wait_until="domcontentloaded")
        await asyncio.sleep(5)

        current_url = page.url
        if _is_login_page_url(current_url):
            return False
        if _is_tmall_creator_home(current_url):
            return True

        # 初始 URL 未定型时，轮询最多 5 次（共 10 秒）等待 JS 跳转稳定
        for _ in range(5):
            await asyncio.sleep(1)
            current_url = page.url
            if _is_login_page_url(current_url):
                return False
            if _is_tmall_creator_home(current_url):
                return True

        tmall_logger.warning(_msg("⚠️", f"cookie 校验未进入目标页: {current_url}"))
        return False
    finally:
        await page.close()


async def cookie_auth(
    account_file,
    *,
    session: TmallBrowserSession,
    max_age_seconds: float = 0,
):
    """验证淘宝光合平台 cookie 是否有效。

    加载 Playwright storage_state 后访问光合平台首页，如果仍停留在淘宝登录页，
    或页面未能进入 creator.guanghe.taobao.com，则按 cookie 失效处理。

    :param account_file: 账号 Cookie 文件路径
    :param session: 浏览器会话
    :param max_age_seconds: 鉴权缓存有效期，<=0 不使用缓存
    :returns: True 有效，False 失效
    """
    # 优先复用鉴权缓存，避免每次任务都打开页面校验
    if session.auth_is_fresh(max_age_seconds):
        return True
    context = await session.ensure_open()
    try:
        authenticated = await _cookie_auth_in_context(context)
    except Exception as exc:
        tmall_logger.warning(_msg("😵", f"cookie 校验时出错，按失效处理: {exc}"))
        authenticated = False
    session.mark_authenticated(authenticated)
    return authenticated


async def tmall_setup(
    account_file,
    handle=False,
    return_detail=False,
    *,
    session: TmallBrowserSession,
    auth_cache_seconds: float = 0,
):
    """检查淘宝光合平台 cookie 有效性，失效且 handle=True 时打开浏览器让用户手动登录。

    :param account_file: 账号 Cookie 文件路径
    :param handle: True 时若 Cookie 失效则打开可见浏览器引导用户登录
    :param return_detail: True 返回完整结果 dict，False 返回布尔
    :param session: 浏览器会话
    :param auth_cache_seconds: 鉴权缓存有效期
    :returns: 取决于 return_detail，返回 dict 或布尔
    """
    if not os.path.exists(account_file) or not await cookie_auth(
        account_file,
        session=session,
        max_age_seconds=auth_cache_seconds,
    ):
        if not handle:
            # 不引导登录时直接返回失效结果
            result = _build_login_result(False, "cookie_invalid", "cookie文件不存在或已失效", account_file)
            return result if return_detail else False

        tmall_logger.info(_msg("🥹", "cookie 失效了，准备打开浏览器让用户手动登录淘宝光合平台"))
        result = await tmall_cookie_gen(
            account_file,
            session=session,
        )
        return result if return_detail else result["success"]

    result = _build_login_result(True, "cookie_valid", "cookie有效", account_file)
    return result if return_detail else True


async def tmall_cookie_gen(
    account_file,
    poll_interval: int = 3,
    max_checks: int = 200,
    *,
    session: TmallBrowserSession,
):
    """打开淘宝光合平台入口，等待用户手动完成登录并进入光合平台。

    不自动输入账号密码，也不绕过任何安全验证。用户在可见浏览器里完成扫码、
    密码、短信或其它淘宝安全验证后，本函数保存 storage_state。

    :param account_file: 账号 Cookie 文件路径
    :param poll_interval: 轮询间隔秒数
    :param max_checks: 最大轮询次数（默认 200 次，约 10 分钟）
    :param session: 浏览器会话
    :returns: 登录结果 dict
    """
    async def run_with_context(context: BrowserContext):
        """在指定上下文中执行登录流程（便于异常时关闭 page）。"""
        result = _build_login_result(False, "failed", "淘宝光合平台登录失败", account_file)
        page = None

        try:
            page = await context.new_page()
            await page.goto(TMALL_CREATOR_HOME_URL, wait_until="domcontentloaded")
            tmall_logger.info(_msg("🧍", "已打开淘宝光合平台入口，请在浏览器中完成登录和验证"))

            # 轮询等待用户完成登录，最多 max_checks 次
            for _ in range(max_checks):
                current_url = page.url

                # 已进入光合后台 → 登录成功
                if _is_tmall_creator_home(current_url):
                    tmall_logger.info(_msg("🥳", f"检测到已进入淘宝光合平台: {current_url}"))
                    break

                # 处于鉴权中间态（passport.taobao.com）→ 继续等待
                if _is_auth_page_url(current_url):
                    await asyncio.sleep(poll_interval)
                    continue

                # 既不在登录页也不在鉴权页也不在后台 → 尝试跳转光合首页
                if not _is_login_page_url(current_url):
                    try:
                        await page.goto(TMALL_CREATOR_HOME_URL, wait_until="domcontentloaded")
                    except Exception as exc:
                        tmall_logger.warning(_msg("⚠️", f"跳转光合平台时出错，继续等待: {exc}"))
                    await asyncio.sleep(poll_interval)
                    if _is_tmall_creator_home(page.url):
                        tmall_logger.info(_msg("🥳", f"检测到已进入淘宝光合平台: {page.url}"))
                        break

                await asyncio.sleep(poll_interval)
            else:
                # for...else：循环正常结束（未 break）表示超时
                result = _build_login_result(
                    False,
                    "timeout",
                    "等待淘宝光合平台登录超时",
                    account_file,
                    page.url,
                )
                return result

            # 等待 3 秒让页面稳定后保存 storage_state
            await asyncio.sleep(3)
            await context.storage_state(path=account_file)
            tmall_logger.info(_msg("💾", f"cookie 已保存: {account_file}"))

            tmall_logger.success(_msg("🥳", "淘宝光合平台登录成功，cookie 验证通过"))
            result = _build_login_result(True, "success", "淘宝光合平台登录成功", account_file, page.url)
            session.mark_authenticated(True)
        except Exception as exc:
            result = _build_login_result(
                False,
                "failed",
                str(exc),
                account_file,
                current_url=page.url if page else "",
            )
        finally:
            if not result["success"]:
                tmall_logger.error(_msg("😢", f"登录失败: {result['message']}"))
            if page:
                await page.close()

        return result

    context = await session.ensure_open()
    return await run_with_context(context)


class TmallBaseUploader(BaseVideoUploader):
    """天猫上传器基类。

    提供账号文件存在性校验，被 TmallVideo 继承。
    """

    def __init__(
        self,
        account_file,
        debug: bool = DEBUG_MODE,
    ):
        """初始化基类。

        :param account_file: 账号 Cookie 文件路径
        :param debug: 是否调试模式
        """
        self.account_file = account_file
        self.debug = debug

    async def validate_base_args(self):
        """校验账号 Cookie 文件存在。"""
        if not os.path.exists(self.account_file):
            raise RuntimeError(f"cookie文件不存在，请先完成淘宝光合平台登录: {self.account_file}")


class TmallVideo(TmallBaseUploader):
    """淘宝光合视频发布器。

    完整发布流程：
    1. validate_upload_args: 校验所有参数（视频、标题、描述、商品、定时时间等）
    2. 打开发布页 → 定位发布 iframe → 上传视频文件
    3. 等待视频上传完成 → 设置自定义封面（可选）
    4. 填写标题/描述/话题标签
    5. 参与活动话题（可选）→ 添加音乐（可选）→ 关联商品
    6. 设置定时/立即发布 → 选择创作者声明 → 点击发布按钮
    7. 等待平台成功/失败/跳转信号，30 秒内无信号则抛 PublishResultUncertainError
    """

    def __init__(
        self,
        file_path,
        title: str,
        desc: str | None,
        account_file,
        cover_ratio: str,
        cover_image_path: str | None = None,
        tags: list[str] | None = None,
        goods_id: str | None = None,
        activity_topic: str | None = None,
        music_name: str | None = None,
        creator_declaration: str = "",
        schedule: datetime | None = None,
        publish_strategy: str = TMALL_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        dry_run: bool = False,
    ):
        """初始化发布参数。

        :param file_path: 视频文件路径
        :param title: 视频标题（最多 30 字）
        :param desc: 视频描述/文案（最多 1000 字，含话题标签）
        :param account_file: 账号 Cookie 文件路径
        :param cover_image_path: 自定义封面图片路径（可选）
        :param cover_ratio: 封面比例（original、3:4 或 1:1）
        :param tags: 话题标签列表（最多 4 个）
        :param goods_id: 商品 ID 字符串（多个用逗号/空格/换行分隔，最多 6 个）
        :param activity_topic: 活动话题关键词（可选，留空表示不参加）
        :param music_name: 音乐名称（可选，留空跳过）
        :param creator_declaration: 创作者声明（必填）
        :param schedule: 定时发布时间（None 立即发布）
        :param publish_strategy: 发布策略 immediate/scheduled
        :param debug: 调试模式
        :param dry_run: True 只走流程不点发布按钮（流程验证）
        """
        super().__init__(account_file=account_file, debug=debug)
        self.file_path = file_path
        self.cover_image_path = cover_image_path
        self.cover_ratio = cover_ratio
        self.title = title
        self.desc = desc or ""
        self.tags = tags or []
        # 商品 ID 解析为元组（去重保序）
        self.goods_ids = _normalized_goods_ids(goods_id or "")
        # 兼容外部读取的字符串形式
        self.goods_id = ",".join(self.goods_ids)
        self.activity_topic = activity_topic or ""
        self.music_name = (music_name or "").strip()
        self.creator_declaration = creator_declaration.strip()
        self.dry_run = dry_run
        self.schedule = schedule
        self.publish_strategy = publish_strategy

    async def validate_upload_args(self):
        """校验所有发布参数，在执行浏览器自动化前完成基础校验。"""
        await self.validate_base_args()
        # 校验视频文件
        self.file_path = str(self.validate_video_file(self.file_path))
        # 校验封面图片（可选）
        if self.cover_image_path:
            cover_path = Path(self.cover_image_path)
            if not cover_path.is_file():
                raise ValueError("天猫封面图片不存在或上传未完成")
            if cover_path.suffix.lower() not in TMALL_COVER_IMAGE_EXTENSIONS:
                raise ValueError("天猫封面图片仅支持 JPG、PNG 或 WebP 格式")
            if cover_path.stat().st_size == 0:
                raise ValueError("天猫封面图片为空")
            if cover_path.stat().st_size > TMALL_MAX_COVER_IMAGE_BYTES:
                raise ValueError("天猫封面图片不能超过 20 MiB")
            self.cover_image_path = str(cover_path.resolve())
        if self.cover_ratio not in TMALL_COVER_RATIOS:
            raise ValueError("天猫封面比例必须为原始、3:4 或 1:1")
        if not self.cover_image_path and self.cover_ratio != "original":
            raise ValueError("未上传自定义封面时，封面比例必须为原始比例")
        # 标题校验
        if not self.title:
            raise ValueError("天猫光合视频标题不能为空")
        if len(self.title) > 30:
            raise ValueError("天猫光合视频标题不能超过30字")
        # 描述 + 话题总长度校验
        if not self.goods_id and not self.tags:
            desc_for_check = self.desc or ""
        else:
            # 描述框最终内容 = 描述 + 每个话题前一个空格 + "#" + tag
            tag_text = "".join(f" #{t}" for t in self._normalized_tags())
            desc_for_check = (self.desc or "") + tag_text
        if len(desc_for_check) > 1000:
            raise ValueError("天猫光合视频描述不能超过1000字")
        # 话题标签最多 4 个，超出截取前 4 个
        if len(self.tags) > 4:
            tmall_logger.warning(_msg("⚠️", f"话题标签最多4个，已自动截取前4个（传入了 {len(self.tags)} 个）"))
            self.tags = self.tags[:4]
        # 商品 ID 校验
        if len(self.goods_ids) > TMALL_MAX_GOODS_IDS:
            raise ValueError(f"天猫一次最多关联 {TMALL_MAX_GOODS_IDS} 个商品ID")
        if any(not goods_id.isdigit() for goods_id in self.goods_ids):
            raise ValueError("天猫光合商品ID必须为数字，多个ID请使用逗号或换行分隔")
        # 音乐名称长度校验
        if len(self.music_name) > 100:
            raise ValueError("天猫音乐名称不能超过100个字符")
        # 定时发布时间校验
        if self.schedule:
            self.validate_publish_date(self.schedule)
        # 创作者声明必填
        if not self.creator_declaration:
            raise ValueError("天猫创作者声明不能为空")

    async def _find_publish_frame(self, page: Page):
        """定位淘宝光合视频发布 iframe。

        发布页通过 iframe 嵌入真实表单，iframe URL 含 gg_publish/gg-video。
        最多等待 30 秒。
        """
        for _ in range(30):
            for frame in page.frames:
                if "gg_publish/gg-video" in frame.url:
                    return frame
            await asyncio.sleep(1)
        raise RuntimeError("未找到淘宝光合视频发布 iframe")

    async def _wait_for_upload_ready(self, frame, timeout_seconds: int = 180):
        """等待视频上传完成，发布表单可编辑。

        判定信号：页面出现"重新上传"且包含"视频封面"字样。
        若出现"上传失败"或"失败"字样则抛出异常。
        超时 180 秒。
        """
        for i in range(timeout_seconds // 2):
            body = await frame.locator("body").inner_text(timeout=3000)
            if "上传失败" in body or "失败" in body:
                raise RuntimeError("视频上传失败，请检查页面提示")
            if "重新上传" in body and "视频封面" in body:
                tmall_logger.success(_msg("🥳", "视频上传完成，发布表单已可编辑"))
                return
            if i % 5 == 0:
                tmall_logger.info(_msg("🏃", "小人正在等待视频上传完成"))
            await asyncio.sleep(1)
        raise RuntimeError("等待视频上传完成超时")

    async def _set_custom_cover(self, frame, page: Page) -> None:
        """通过创作者页面的本地上传工作流设置自定义封面。

        流程：
        1. 点击"编辑"封面按钮（等待智能封面生成完成）
        2. 在弹窗中点击"本地上传" → 通过跨层图库选择本地文件
        3. 等待上传完成 → 点击"完成" → 选中刚上传的图片
        4. 点击"确定" → 按所选比例处理裁剪页 → 处理花字确认层
        5. 回到主表单

        图片库、裁剪层、花字层以独立 frame 呈现；通过可见控件的语义定位完成
        操作。新增图片和选中状态必须在图库 iframe 内确认，不能依赖素材卡片或
        视口坐标。
        """
        cover_path = Path(self.cover_image_path)
        tmall_logger.info(_msg("🖼️", f"准备设置自定义封面: {cover_path.name}"))
        # 等待"编辑"封面按钮可点击
        edit_button = frame.locator('[data-autolog-container="coverOperate_edit"]').first
        await edit_button.wait_for(state="visible", timeout=90000)

        # "编辑"提前显示时智能封面可能仍在慢加载，点击会被忽略
        smart_cover_loading = frame.get_by_text("智能封面图生成中", exact=False).first
        try:
            await smart_cover_loading.wait_for(state="hidden", timeout=90000)
        except PlaywrightTimeoutError:
            tmall_logger.warning(_msg("⚠️", "智能封面生成超时，尝试继续设置本地封面"))
        await asyncio.sleep(3)

        # 弹窗内部 class 会变，但顶层弹窗始终有 .next-overlay-wrapper.opened 遮罩层。
        # 通过 opened 数量变化判断点击是否生效，避免重复点击。
        opened_overlays = frame.locator(".next-overlay-wrapper.opened")

        async def wait_for_new_overlay(previous_count: int, timeout_seconds: int) -> int | None:
            """等待新的遮罩层出现（表示弹窗已打开）。"""
            for _ in range(timeout_seconds * 2):
                current_count = await opened_overlays.count()
                if current_count > previous_count:
                    return current_count
                await asyncio.sleep(0.5)
            return None

        initial_overlay_count = await opened_overlays.count()
        cover_overlay_count = None
        # 最多重试 3 次点击"编辑"按钮
        for attempt in range(3):
            current_overlay_count = await opened_overlays.count()
            if current_overlay_count > initial_overlay_count:
                cover_overlay_count = current_overlay_count
                break
            try:
                await edit_button.click(timeout=10000)
            except PlaywrightTimeoutError as exc:
                current_overlay_count = await opened_overlays.count()
                if current_overlay_count > initial_overlay_count:
                    cover_overlay_count = current_overlay_count
                    break
                if attempt == 2:
                    raise RuntimeError("天猫封面编辑入口点击后未打开弹窗") from exc

            cover_overlay_count = await wait_for_new_overlay(initial_overlay_count, 15)
            if cover_overlay_count is not None:
                break
            if attempt < 2:
                tmall_logger.info(_msg("🏃", "封面模块仍在加载，15 秒后重试编辑"))
                await asyncio.sleep(15)
        if cover_overlay_count is None:
            raise RuntimeError("天猫封面编辑入口点击后未打开弹窗")

        # nth() 固定本次新增的遮罩层；顶层弹窗关闭后该 locator 变 hidden
        cover_dialog = opened_overlays.nth(initial_overlay_count)

        # 第一个入口"本地上传"打开平台图片库（不直接打开系统文件选择器）
        await cover_dialog.get_by_text("本地上传", exact=False).last.click()

        # 图库嵌在独立 iframe 中，其内部控件及素材卡片均通过 frame DOM 识别。
        await asyncio.sleep(1)

        picker_frame = None
        for _ in range(20):
            picker_frame = next(
                (
                    candidate
                    for candidate in page.frames
                    if "sucai-selector-ng" in candidate.url
                ),
                None,
            )
            if picker_frame is not None:
                break
            await asyncio.sleep(0.5)
        if picker_frame is None:
            raise RuntimeError("未找到天猫图片库 iframe，无法上传自定义封面")

        # 图片库会永久保存原始文件名。先用受管临时副本上传，让素材库中展示的
        # 名称始终包含时间和随机任务标识，批量 Excel 里的同名 cover.png 也不会
        # 与历史素材混淆。上传结果确认前保留副本，避免浏览器尚未读取完文件。
        with tempfile.TemporaryDirectory(prefix="mpau-cover-") as staging_dir:
            staged_cover_path = Path(staging_dir) / _cover_picker_upload_name(cover_path)
            shutil.copy2(cover_path, staged_cover_path)
            await _upload_picker_file(page, picker_frame, staged_cover_path)

            # 文件上传成功后先进入"上传结果"，点击"完成"才会回到图片库。
            await asyncio.sleep(4.5)
            await _click_visible_frame_button(
                (picker_frame,), ("完成",), description="图片上传完成"
            )
            expected_cover_stem = staged_cover_path.stem.casefold()
        await asyncio.sleep(1)

        # 与图文上传相同，图片库会保留历史素材且可能重挂载卡片。封面暂存文件名
        # 含任务唯一标识，必须按图库展示的完整文件名精确找到唯一 label 卡片，不能
        # 用缩略图 URL 差集推断（后者会把预加载或历史缓存图片误当作新素材）。
        selected_cover = None
        for _ in range(120):
            selected_cover = await picker_frame.evaluate(
                r"""(expectedStem) => {
                    const cards = [...document.querySelectorAll('label')].filter(card =>
                        card.querySelector('.PicList_pic_imgBox__c0HXw img')
                        && card.querySelector('input[type="checkbox"], input[type="radio"]')
                    );
                    const matches = cards.filter(card => {
                        const cardText = (card.parentElement?.innerText || card.innerText || '')
                            .toLocaleLowerCase();
                        return cardText.includes(expectedStem);
                    });
                    const matchingCards = matches.map(card =>
                        (card.parentElement?.innerText || card.innerText || '')
                            .replace(/\s+/g, ' ').trim()
                    );
                    if (matches.length !== 1) {
                        return { count: matches.length, matchingCards };
                    }
                    const control = matches[0].querySelector(
                        'input[type="checkbox"], input[type="radio"]'
                    );
                    if (!control) return { count: 0, matchingCards };
                    control.setAttribute('data-mpau-new-cover-control', 'true');
                    return { count: 1, checked: control.checked, matchingCards };
                }""",
                expected_cover_stem,
            )
            if selected_cover and selected_cover.get("count") == 1:
                break
            await asyncio.sleep(0.5)
        if not selected_cover or selected_cover.get("count") != 1:
            count = selected_cover.get("count", 0) if selected_cover else 0
            observed = " / ".join(selected_cover.get("matchingCards", []) if selected_cover else [])
            raise RuntimeError(
                f"图片库未精确识别本次上传的封面“{expected_cover_stem}”（匹配 {count} 张）；"
                f"图库匹配详情：{observed or '未获得'}，已停止避免选择错误图片。"
            )
        selected_control = picker_frame.locator('[data-mpau-new-cover-control="true"]')
        if not selected_cover.get("checked"):
            # 与图文流程使用相同的受控 checkbox 点击方式。此 input 从已按文件名
            # 锁定的 label 卡片取得，避免误选预加载缩略图或历史同类图片。
            await selected_control.evaluate("(control) => control.click()")
        if not await selected_control.is_checked():
            raise RuntimeError("本次上传封面未进入选中状态，已停止避免选择错误图片。")
        tmall_logger.info(_msg("🖼️", "已精确选中本次上传的封面素材"))

        await _click_visible_frame_button(
            (picker_frame,), ("确定",), description="图片库确认"
        )
        await asyncio.sleep(1)

        # 原始比例保持平台默认选中态，直接识别并点击下一步；其他比例从实时页面
        # 截图动态识别卡片位置，绝不能回退为固定坐标。
        await _select_cover_ratio_and_continue(page, self.cover_ratio)
        await asyncio.sleep(1)

        # 进入可选的花字确认层；不选模板也要确认，封面才会写回主表单。
        await _click_visible_frame_button(
            tuple(reversed(page.frames)),
            ("下一步", "完成", "确定"),
            description="花字确认",
        )
        await asyncio.sleep(1)
        await cover_dialog.wait_for(state="hidden", timeout=10000)
        tmall_logger.success(_msg("🖼️", f"自定义封面已设置: {cover_path.name}"))

    def _build_description(self) -> str:
        """返回纯描述文本。

        话题标签单独通过键盘输入触发平台的话题下拉建议，不再拼接到描述末尾
        （那样只是纯文本，不会成为平台识别的话题）。
        """
        return self.desc or ""

    def _normalized_tags(self) -> list[str]:
        """清洗 tags：去 # 前缀、去空白、过滤空项。"""
        cleaned = []
        for tag in self.tags:
            t = tag.strip().lstrip("#")
            if t:
                cleaned.append(t)
        return cleaned

    async def _fill_title_and_desc(self, frame, page: Page):
        """填写视频标题与描述，并逐个输入话题标签。

        描述区是淘宝"仓颉"富文本编辑器（contenteditable div），不是真正的 textarea。
        直接用 fill() 改 textarea.value 不会触发 hashtag 识别，必须 click 聚焦后
        逐字符 type。话题标签通过 #xxx + 空格触发平台话题下拉建议并选中首项。
        """
        # 填写标题
        title_input = frame.locator('input[placeholder="加个标题让内容更吸引人"]').first
        await title_input.wait_for(state="visible", timeout=10000)
        await title_input.fill(self.title[:30])
        tmall_logger.info(_msg("✍️", f"视频标题已填写: {self.title[:30]}"))

        # 定位仓颉富文本编辑器
        desc_editor = frame.locator('div[data-cangjie-content="true"]').first
        await desc_editor.wait_for(state="visible", timeout=10000)
        await desc_editor.click()

        # 清空已有内容（草稿可能自动保留上次输入）
        await page.keyboard.press("Meta+A")
        await page.keyboard.press("Delete")

        # 输入描述文本
        desc = self._build_description()
        if desc:
            await page.keyboard.type(desc[:1000])
            tmall_logger.info(_msg("✍️", f"视频描述已填写: {desc[:30]}"))

        # 逐个输入话题标签
        tags = self._normalized_tags()
        if not tags:
            return

        # 描述末尾逐个敲话题。contenteditable 富文本会识别 "#xxx" 并把话题染蓝
        # （与用户手写 #狗粮 变蓝是同一机制）。用空格分隔每个话题。
        for index, tag in enumerate(tags, start=1):
            tmall_logger.info(_msg("🏷️", f"小人正在添加第 {index} 个话题: #{tag}"))
            await page.keyboard.type(f" #{tag}")
            await asyncio.sleep(1)
            # 空格确认选中下拉建议里的第一项（若下拉未弹出则作为普通分隔符）
            await page.keyboard.press("Space")
            await asyncio.sleep(1)
        tmall_logger.info(_msg("🏷️", f"小人一共贴了 {len(tags)} 个话题"))

    async def _add_goods(self, frame):
        """通过商品 ID 依次关联商品。

        流程：点"添加商品" → 打开关联商品对话框 → 逐个搜索商品 ID →
        等待搜索结果 → 精确匹配商品卡片 → 勾选 → 全部完成后点"确定"。

        商品卡片匹配策略：
        1. 优先在卡片 HTML 标记中查找精确 ID
        2. 卡片标记不再暴露 ID 时，通过商品链接的 ?id=xxx 参数匹配
        3. 30 秒内未匹配则报错（避免关联错误商品）

        注意：第一次搜索后需要重新定位 result_area，因为对话框打开时
        "推荐"tab 在 DOM 中排在"搜索结果"tab 前面，bare .first 会错误
        地锁定到推荐面板。
        """
        if not self.goods_ids:
            return

        tmall_logger.info(
            _msg("🛒", f"小人准备添加 {len(self.goods_ids)} 个商品: {', '.join(self.goods_ids)}")
        )
        # 图文页和视频页的文字节点可能被子元素覆盖，强制点击语义入口以兼容两种布局。
        await frame.get_by_text("添加商品", exact=True).first.click(force=True)
        dialog = frame.locator(".next-dialog").filter(has_text="关联商品").first
        await dialog.wait_for(state="visible", timeout=10000)

        LOADING_HINTS = ("加载中", "loading")

        for goods_index, goods_id in enumerate(self.goods_ids, start=1):
            # 记录搜索前结果区文本，用于检测结果是否变化
            result_area = dialog.locator(
                '[role="tabpanel"].active, [class*="tab-content"], [class*="content--"]'
            ).first
            before_result_text = await result_area.inner_text(timeout=3000)

            # 填入商品 ID 并触发搜索。
            search = dialog.locator(
                'input[placeholder="输入商品关键词或商品ID"], '
                'input[placeholder="搜索"], input[type="search"]'
            ).first
            await search.wait_for(state="visible", timeout=5000)
            await search.click()
            await search.fill(goods_id)
            # 首次搜索的“本店商品”列表尚未初始化：先用 Enter 提交输入值，
            # 等平台接收该值后再点搜索图标，避免第一次搜索未触发。
            await search.press("Enter")
            if goods_index == 1:
                await asyncio.sleep(1.5)
                search_icon = dialog.locator(
                    'i[role="button"][aria-label="搜索"].next-search-icon'
                ).first
                await search_icon.wait_for(state="visible", timeout=5000)
                await search_icon.click()
            tmall_logger.info(
                _msg(
                    "🔎",
                    f"正在搜索第 {goods_index}/{len(self.goods_ids)} 个商品ID: {goods_id}",
                )
            )

            # 第一次搜索后重新定位 result_area：
            # 对话框打开时"推荐"tab 在 DOM 中排在"搜索结果"tab 前面，
            # bare .first 会锁定到过期的推荐面板。第一次搜索后平台切换到
            # "搜索结果"tab 并标记为 active，后续搜索不再有竞争的推荐面板。
            if goods_index == 1:
                await asyncio.sleep(1.5)
                result_area = dialog.locator("[role=\"tabpanel\"].active").first
                if await result_area.count() == 0:
                    result_area = dialog.locator(
                        '[class*="tab-content"], [class*="content--"]'
                    ).first

            matched_item = None
            # 轮询等待搜索结果（最多 30 秒）
            for _ in range(30):
                await asyncio.sleep(1)
                result_text = await result_area.inner_text(timeout=3000)
                if any(hint in result_text.lower() for hint in LOADING_HINTS):
                    continue

                # 策略 1：在卡片 HTML 标记中查找精确 ID
                candidates = result_area.locator('[class*="item--"]').filter(has_text="¥")
                candidate_count = await candidates.count()
                for candidate_index in range(candidate_count):
                    candidate = candidates.nth(candidate_index)
                    markup = await candidate.evaluate("el => el.outerHTML || ''")
                    if _contains_exact_product_id(markup, goods_id):
                        matched_item = candidate
                        break
                # 策略 2：卡片标记不再暴露 ID 时，通过商品链接 ?id=xxx 匹配
                if matched_item is None:
                    product_links = result_area.locator("a[href]")
                    for link_index in range(await product_links.count()):
                        product_link = product_links.nth(link_index)
                        href = await product_link.get_attribute("href")
                        if not _product_href_has_exact_id(href, goods_id):
                            continue
                        # 反向定位到包含 checkbox 的祖先元素
                        matched_item = product_link.locator(
                            "xpath=ancestor::*[.//input[@type='checkbox']][1]"
                        )
                        break
                if matched_item is not None:
                    break
                # 明确无结果提示 + 结果区文本变化 + 候选数为 0 → 商品不存在
                if (
                    result_text.strip() != before_result_text.strip()
                    and candidate_count == 0
                    and _has_explicit_empty_product_result(result_text)
                ):
                    raise ValueError(
                        f"商品ID {goods_id} 在本店商品库中搜索不到。"
                        "请核实：1) ID是否正确；2) 商品是否上架；3) 商品是否属于该账号的店铺。"
                    )
            else:
                raise RuntimeError(
                    f"商品ID {goods_id} 搜索结果无法精确确认。"
                    "为避免关联错误商品，任务已停止；请用显示浏览器模式核对平台搜索结果。"
                )

            # 勾选匹配到的商品
            checkbox = matched_item.locator(
                'label[class*="checkbox"], label.next-checkbox-wrapper'
            ).first
            checkbox_input = checkbox.locator('input[type="checkbox"]').first
            already_selected = (
                await checkbox_input.count() > 0 and await checkbox_input.is_checked()
            )
            if not already_selected:
                await checkbox.click()
            await asyncio.sleep(1)

            # 校验勾选成功
            if await checkbox_input.count() > 0:
                for _ in range(10):
                    if await checkbox_input.is_checked():
                        break
                    await asyncio.sleep(0.1)
                else:
                    raise RuntimeError(f"商品 {goods_id} 勾选后未进入选中状态")
            else:
                # 部分 DOM 没有 checkbox input，通过"已选商品"文案兜底校验
                selected_text = await dialog.inner_text(timeout=3000)
                if "已选商品" not in selected_text:
                    raise RuntimeError(f"商品 {goods_id} 勾选失败")
            tmall_logger.info(
                _msg("✅", f"已勾选第 {goods_index}/{len(self.goods_ids)} 个商品: {goods_id}")
            )

        # 全部商品勾选完成，点"确定"关闭对话框
        await dialog.get_by_role("button", name="确定").click()
        await dialog.wait_for(state="hidden", timeout=10000)
        tmall_logger.success(
            _msg("🛒", f"{len(self.goods_ids)} 个商品全部添加完成: {', '.join(self.goods_ids)}")
        )

    async def _add_activity_topic(self, frame, page: Page):
        """参与话题活动。

        activity_topic 为空时不参加活动；有值时打开话题选择对话框 → 搜索关键词 →
        选搜索结果第一张卡片 → 确认提交。
        若搜索无结果 → 报错终止。
        """
        if not self.activity_topic:
            tmall_logger.info(_msg("📣", "未指定话题活动，保持不参加"))
            return

        tmall_logger.info(_msg("📣", f"准备搜索话题活动: {self.activity_topic}"))

        # 点"点击添加话题"区域打开对话框
        add_btn = frame.locator('[class*="topic-v2--select--"]').first
        await add_btn.click()

        dialog = frame.locator(".next-dialog").filter(has_text="话题选择").first
        await dialog.wait_for(state="visible", timeout=10000)

        # 搜索关键词
        search_input = dialog.locator('input[placeholder="输入关键词搜索"]').first
        await search_input.click()
        await page.keyboard.type(self.activity_topic)
        await asyncio.sleep(0.5)

        search_btn = dialog.locator(".next-btn-primary").filter(has_text="搜索").first
        await search_btn.click()
        tmall_logger.info(_msg("🔎", f"已搜索话题关键词: {self.activity_topic}"))
        await asyncio.sleep(3)

        # 找搜索结果第一张可点击卡片
        # 结构：.topic-card--xxx > .topic-card-select--xxx（cursor:pointer）
        result_card = dialog.locator('[class*="topic-card-select--"]').first
        count = await result_card.count()
        if count == 0:
            # 无结果时取消对话框并报错
            await dialog.locator(".next-btn-normal").filter(has_text="取消").first.click()
            raise ValueError(
                f"话题活动搜索关键词 '{self.activity_topic}' 无结果。"
                "请核实关键词是否正确，或不传 --activity-topic 使用平台推荐话题。"
            )

        # 提取话题名称用于日志
        topic_name = await result_card.evaluate(
            "el => (el.getAttribute('data-autolog') || el.innerText || '').split(':').pop().split('\\n')[0].trim()"
        )
        await result_card.click()
        await asyncio.sleep(1)
        tmall_logger.info(_msg("📣", f"已选择话题: {topic_name}"))

        # 确认提交
        submit_btn = dialog.locator(".next-btn-primary").filter(has_text="确认提交").first
        await submit_btn.click()
        await dialog.wait_for(state="hidden", timeout=10000)
        tmall_logger.success(_msg("📣", f"话题活动已参与: {topic_name}"))

    async def _add_music(self, frame) -> None:
        """搜索并添加音乐。

        每次追加两个字符输入，等平台慢加载完成后再输入下一组。
        输入完成后轮询等待搜索结果落地，必须以"暂无结果"提示或音乐卡片
        实际渲染为证据，不能在转圈加载阶段就判定失败。
        最后选中第一个歌曲名完全相同的结果并确认。
        """
        if not self.music_name:
            tmall_logger.info(_msg("🎵", "未指定音乐，跳过添加音乐"))
            return

        tmall_logger.info(_msg("🎵", f"准备添加音乐: {self.music_name}"))
        # 视频上传后标签从"点击添加音乐"变为"更多音乐"
        trigger = frame.locator('[data-autolog*="key=music_selector_card"]').first
        # 与商品选择器一样，正常 locator 点击会等待卡片并滚动到可见位置
        await trigger.click()

        # 搜索会重建对话框可访问性节点，但 Next dialog class 保持稳定
        dialog = frame.locator(".next-dialog").filter(has_text="编辑音乐").first
        await dialog.wait_for(state="visible", timeout=10000)

        # 每次只追加两个字符，等平台慢加载完成后再输入下一组
        chunks = _two_character_chunks(self.music_name)
        for index, chunk in enumerate(chunks):
            search = dialog.locator('input:not([type="hidden"])').first
            await search.wait_for(state="visible", timeout=10000)
            # 通过 JS 设置 value 并触发 input 事件，避免 Playwright type 一次性输入
            await search.evaluate(
                """(element, text) => {
                  const valueSetter = Object.getOwnPropertyDescriptor(
                    HTMLInputElement.prototype,
                    'value'
                  ).set;
                  valueSetter.call(element, `${element.value}${text}`);
                  element.dispatchEvent(new InputEvent('input', {
                    bubbles: true,
                    data: text,
                    inputType: 'insertText',
                  }));
                }""",
                chunk,
            )
            if index < len(chunks) - 1:
                await asyncio.sleep(1)

        # 完整名称输入后等 3 秒让异步搜索请求稳定返回
        await asyncio.sleep(3)

        # 轮询等待搜索结果落地：必须以"暂无结果"提示或音乐卡片实际渲染为证据
        # 不能在转圈加载阶段就判定失败（页面无匹配时仍可能渲染热榜兜底卡片，
        # 但顶部会显示"暂无结果"提示，这是区分"找不到"与"还在搜"的关键标志）
        music_no_result_hints = (
            "暂无结果",
            "未搜到",
            "没有搜到",
            "没有找到",
            "找不到相关",
        )
        search_state: dict | None = None
        for _ in range(20):  # 最多再轮询 10 秒
            state = await dialog.evaluate(
                r"""(element, hints) => {
                  const cards = element.querySelectorAll(
                    '.music-space-card, .music-space-card-active'
                  );
                  const text = (element.innerText || '');
                  const hasNoResultHint = hints.some((hint) => text.includes(hint));
                  const seenSpinner = !!element.querySelector(
                    '.next-loading, .next-loading-component, [class*="loading"], [class*="spin"]'
                  );
                  const cardTitles = [...cards].map((card) => (
                    card.querySelector('.card-right-name-text-1')?.textContent
                    || card.querySelector('.card-right-name')?.textContent
                    || ''
                  ).trim()).filter(Boolean);
                  return {
                    cardCount: cards.length,
                    cardTitles,
                    hasNoResultHint,
                    seenSpinner,
                    snippet: text.replace(/\s+/g, ' ').slice(0, 120),
                  };
                }""",
                list(music_no_result_hints),
            )
            search_state = state
            if state["hasNoResultHint"] or state["cardCount"] > 0:
                break
            await asyncio.sleep(0.5)

        if search_state is None:
            search_state = {
                "cardCount": 0,
                "cardTitles": [],
                "hasNoResultHint": False,
                "seenSpinner": False,
                "snippet": "",
            }

        # 平台可能在“暂无结果”下展示热榜；先从所有可见卡片中找精确匹配。
        # 例如搜索词未命中索引时，热榜仍可能包含用户指定的同名歌曲。
        selection = await dialog.evaluate(
            r"""(element, expectedTitle) => {
              const normalize = (value) => (value || '')
                .replace(/\s+/g, '')
                .replace(/（/g, '(')
                .replace(/）/g, ')');
              const titleFor = (card) => (
                card.querySelector('.card-right-name-text-1')?.textContent
                || card.querySelector('.card-right-name')?.textContent
                || ''
              ).trim();
              const cards = [...element.querySelectorAll(
                '.music-space-card, .music-space-card-active'
              )];
              const card = cards.find((item) => normalize(titleFor(item)) === normalize(expectedTitle));
              if (!card) {
                return {
                  selected: false,
                  availableTitles: cards.map(titleFor).filter(Boolean).slice(0, 12),
                };
              }
              card.scrollIntoView({ block: 'center' });
              card.click();
              return { selected: true, title: titleFor(card) };
            }""",
            self.music_name,
        )
        if not selection["selected"]:
            if search_state["hasNoResultHint"]:
                hint_text = next(
                    (h for h in music_no_result_hints if h in search_state["snippet"]),
                    "暂无结果",
                )
                raise ValueError(
                    f"音乐“{self.music_name}”搜索无结果（页面提示：{hint_text}），"
                    "且热榜中没有同名歌曲卡片。"
                    "请核实音乐名是否正确，或留空 --music 使用平台推荐音乐。"
                )
            available_titles = "、".join(selection["availableTitles"])
            raise ValueError(
                f"音乐“{self.music_name}”搜索后没有同名歌曲卡片。"
                f"当前展示：{available_titles or '无可选歌曲'}。"
            )

        # 等待音乐卡片进入已选中状态
        for _ in range(10):
            selected = await dialog.evaluate(
                r"""(element, expectedTitle) => {
                  const normalize = (value) => (value || '')
                    .replace(/\s+/g, '')
                    .replace(/（/g, '(')
                    .replace(/）/g, ')');
                  const titleFor = (card) => (
                    card.querySelector('.card-right-name-text-1')?.textContent
                    || card.querySelector('.card-right-name')?.textContent
                    || ''
                  ).trim();
                  return [...element.querySelectorAll('.music-space-card-active')].some(
                    (card) => normalize(titleFor(card)) === normalize(expectedTitle)
                  );
                }""",
                self.music_name,
            )
            if selected:
                break
            await asyncio.sleep(0.3)
        else:
            raise RuntimeError(f"音乐“{self.music_name}”卡片未进入已选中状态")

        # 点击"确定"按钮，并校验弹窗关闭
        await dialog.locator("button").filter(has_text="确定").first.click(force=True)
        for _ in range(10):
            if not await dialog.is_visible():
                break
            # 平台未接受选择时会显示"请先选择音乐"警告
            warning = frame.get_by_text("请先选择音乐", exact=True).first
            if await warning.count() and await warning.is_visible():
                raise RuntimeError(
                    f"音乐“{self.music_name}”已显示为选中，但平台未接受该选择"
                )
            await asyncio.sleep(1)
        else:
            raise RuntimeError("已点击音乐确认按钮，但“编辑音乐”弹窗未关闭")
        tmall_logger.success(_msg("🎵", f"已添加音乐: {self.music_name}"))

    async def _set_schedule(self, frame, page: Page):
        """设置定时发布时间，或确认使用立即发布。

        立即发布：无需操作日期组件。
        定时发布：
        1. 点击"定时发布" radio
        2. 打开日历面板，翻月找目标日期
        3. 点击目标日期 cell
        4. 切换到时分滚轮面板，选择小时和分钟
        5. 点"确定"提交时间选择
        6. 校验输入框最终值与期望值一致

        平台限制：天猫只允许选最近若干天的日期，超出范围对应 cell 会 disabled。
        """
        date_picker = frame.locator(".next-date-picker").first
        await date_picker.scroll_into_view_if_needed()
        await asyncio.sleep(0.5)

        if not self.schedule:
            tmall_logger.info(_msg("📅", "立即发布模式，无需设置发布时间"))
            return

        # 必须点击真正的 radio input/label。之前的 evaluate(...click()) 只是触发
        # 非可信 DOM 事件：页面上的日期框会出现，但 radio 仍是“立即发布”，
        # 因而当天时分滚轮整列保持灰色不可选。
        labels = frame.locator("label.next-radio-wrapper")
        schedule_label = None
        for index in range(await labels.count()):
            candidate = labels.nth(index)
            # 光合的结构是 radio label 紧接着文字 span；只认右侧文字，避免
            # 命中别的父容器/隐藏副本导致误判为已切换。
            is_schedule_label = await candidate.evaluate(
                """label => (label.nextElementSibling?.innerText || '').trim() === '定时发布'"""
            )
            if await candidate.is_visible() and is_schedule_label:
                schedule_label = candidate
                break
        if schedule_label is None:
            raise RuntimeError("未找到当前可见的定时发布单选框")
        radio_input = schedule_label.locator('input[type="radio"]').first
        radio_box = await schedule_label.bounding_box()
        if not radio_box:
            raise RuntimeError("未取得定时发布单选框的位置")
        # 按真实鼠标坐标点击单选圆点，严格复现人工点选，而不是 DOM click。
        await page.mouse.click(
            radio_box["x"] + radio_box["width"] / 2,
            radio_box["y"] + radio_box["height"] / 2,
        )
        await asyncio.sleep(1)

        # 两层校验：radio 的 checked 状态和底部按钮文字。只有两者都切到定时
        # 发布，才允许继续选日期和时间，避免在“立即发布”的禁用面板中操作。
        selected = await radio_input.is_checked()
        if not selected:
            raise RuntimeError("真实点击后“定时发布”仍未选中，已停止操作避免进入禁用时间面板")
        publish_button_text = ""
        publish_buttons = frame.locator("button")
        for index in range(await publish_buttons.count()):
            candidate = publish_buttons.nth(index)
            if not await candidate.is_visible():
                continue
            text = (await candidate.inner_text()).strip()
            if text in {"立即发布", "定时发布"}:
                publish_button_text = text
                break
        if publish_button_text != "定时发布":
            raise RuntimeError(
                f"定时发布单选框已点击，但页面主按钮仍为“{publish_button_text or '未知'}”，"
                "未进入可选时间状态。"
            )

        # 验证 date-picker 输入框已启用
        inp = frame.locator('input[placeholder="请选择日期和时间"]').first
        is_disabled = await inp.evaluate("el=>el.disabled")
        if is_disabled:
            raise RuntimeError("点击定时发布 radio 后日期输入框仍为禁用状态")

        # 点日历图标打开面板
        cal = frame.locator("i.next-icon-calendar").first
        await cal.click()
        # 等面板里出现月份 button（确保面板已渲染）
        await frame.locator("button.next-calendar-btn-next-month").first.wait_for(
            state="visible", timeout=8000
        )
        await asyncio.sleep(0.5)

        # 选择日期：不要直接填输入框。Fusion DatePicker 输入框会改变面板页码，
        # 但不等于改变内部选中日期。这里按面板可见日期范围翻页，直到目标日期出现，
        # 然后点击目标格子，让组件内部状态真正更新。
        date_str = self.schedule.strftime("%Y/%m/%d")
        date_input = frame.locator('input[placeholder="YYYY/MM/DD"]').first
        await date_input.wait_for(state="visible", timeout=8000)

        target_date_key = self.schedule.strftime("%Y/%m/%d")
        day_result = None
        # 最多翻 14 次月（覆盖 1 年内任意月份）
        for _ in range(14):
            day_result = await frame.evaluate(
                """(targetTitle) => {
                    const isVisible = (element) => {
                        for (let node = element; node; node = node.parentElement) {
                            const style = window.getComputedStyle(node);
                            if (style.display === 'none' || style.visibility === 'hidden'
                                || Number(style.opacity) === 0) return false;
                        }
                        const rect = element.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0
                            && rect.bottom > 0 && rect.right > 0
                            && rect.top < window.innerHeight && rect.left < window.innerWidth;
                    };
                    const cells = [...document.querySelectorAll('td[title]')];
                    const visibleCells = cells.filter(isVisible);

                    function isDisabled(el) {
                        return el.getAttribute('aria-disabled') === 'true'
                            || el.classList.contains('next-calendar-cell-disabled')
                            || el.classList.contains('next-disabled')
                            || el.querySelector('.next-calendar-date-disabled') !== null;
                    }

                    const visibleTitles = visibleCells.map(el => el.getAttribute('title')).filter(Boolean).sort();
                    const enabledTitles = visibleCells.filter(el => !isDisabled(el))
                        .map(el => el.getAttribute('title')).filter(Boolean).sort();
                    const cellIndex = cells.findIndex(
                        el => isVisible(el) && el.getAttribute('title') === targetTitle
                    );
                    const cell = cells[cellIndex];

                    if (!cell) {
                        return {
                            found: false,
                            disabled: false,
                            first: enabledTitles[0] || '',
                            last: enabledTitles[enabledTitles.length - 1] || '',
                            visibleFirst: visibleTitles[0] || '',
                            visibleLast: visibleTitles[visibleTitles.length - 1] || '',
                        };
                    }
                    if (isDisabled(cell)) {
                        return {
                            found: true,
                            disabled: true,
                            first: enabledTitles[0] || '',
                            last: enabledTitles[enabledTitles.length - 1] || '',
                            visibleFirst: visibleTitles[0] || '',
                            visibleLast: visibleTitles[visibleTitles.length - 1] || '',
                        };
                    }
                    return {
                        found: true,
                        disabled: false,
                        cellIndex,
                        first: enabledTitles[0] || '',
                        last: enabledTitles[enabledTitles.length - 1] || '',
                        visibleFirst: visibleTitles[0] || '',
                        visibleLast: visibleTitles[visibleTitles.length - 1] || '',
                    };
                }""",
                target_date_key,
            )
            if day_result.get("found"):
                break

            # 根据当前面板可见范围决定向前还是向后翻月
            visible_first = day_result.get("visibleFirst") or ""
            visible_last = day_result.get("visibleLast") or ""
            if visible_first and target_date_key < visible_first:
                await frame.locator("button.next-calendar-btn-prev-month").first.click()
            elif visible_last and target_date_key > visible_last:
                await frame.locator("button.next-calendar-btn-next-month").first.click()
            else:
                # in_view 没解析出来，跳出避免死循环
                break
            await asyncio.sleep(0.3)
        if not day_result.get("found"):
            raise RuntimeError(
                f"未在当前日历面板找到目标日期 {self.schedule.strftime('%Y-%m-%d')}。"
                f"当前面板显示范围约为 {day_result.get('visibleFirst') or '未知'}"
                f" 到 {day_result.get('visibleLast') or '未知'}，"
                f"可点击范围约为 {day_result.get('first') or '未知'}"
                f" 到 {day_result.get('last') or '未知'}。"
            )
        if day_result.get("disabled"):
            raise ValueError(
                f"天猫光合平台当前不允许选择定时日期 {self.schedule.strftime('%Y-%m-%d')}。"
                f"当前面板可点击范围约为 {day_result.get('first') or '未知'}"
                f" 到 {day_result.get('last') or '未知'}。"
                "请改用日历中可点击的日期后重试。"
            )

        # 不能在 evaluate 中直接 cell.click()。光合对当天日期会更新可见文字，
        # 但可能不接受非可信事件，随后的时分滚轮便全部变为不可选。
        # 通过 Patchright 发出真实点击，确保组件内部的日期状态同步。
        day_cell = frame.locator("td[title]").nth(day_result["cellIndex"])
        await day_cell.scroll_into_view_if_needed()
        await day_cell.click()

        await asyncio.sleep(0.5)
        selected_date = await inp.evaluate("el=>el.value")
        if date_str not in selected_date:
            raise RuntimeError(
                f"点击日期 {date_str} 后平台未接受真实选择，页面实际为 {selected_date or '空'}。"
            )
        tmall_logger.info(_msg("📅", f"已选择日期: {date_str}"))

        # 点"选择时间"按钮，切换到时:分滚轮面板
        time_btn = frame.locator("button").filter(has_text="选择时间").first
        await time_btn.click()
        await asyncio.sleep(1)

        hour = self.schedule.hour
        minute = self.schedule.minute

        hour_item = frame.locator(
            f'ul.next-time-picker-menu-hour li[title="{hour}"]'
        ).first
        await hour_item.click()
        tmall_logger.info(_msg("🕐", f"已选择小时: {hour}"))
        await asyncio.sleep(0.3)

        minute_item = frame.locator(
            f'ul.next-time-picker-menu-minute li[title="{minute}"]'
        ).first
        await minute_item.click()
        tmall_logger.info(_msg("🕐", f"已选择分钟: {minute}"))
        await asyncio.sleep(0.3)

        # 点"确定"提交时间选择（按钮在面板右下角，用 JS 直接点避免遮挡）
        await frame.evaluate("""()=>{
            const btns = [...document.querySelectorAll('button.next-btn-primary')];
            const ok = btns.find(b => (b.innerText || '').trim() === '确定');
            if (ok) ok.click();
        }""")
        await asyncio.sleep(1)

        # 校验输入框最终值与期望值一致
        final_val = await inp.evaluate("el=>el.value")
        expected_val = self.schedule.strftime("%Y/%m/%d %H:%M")
        if final_val != expected_val:
            raise RuntimeError(
                f"定时发布时间设置后校验失败：期望 {expected_val}，页面实际为 {final_val}。"
                "已停止发布，避免误定时到错误日期。"
            )
        tmall_logger.success(_msg("📅", f"定时发布时间已设置: {final_val}"))

    async def _select_creator_declaration(self, frame) -> None:
        """选择运营人员指定的创作者声明单选项。

        通过归一化文本匹配（去除所有空白字符），避免空格/换行差异导致匹配失败。
        找到后点击 radio 并校验进入选中状态。
        """
        target = self.creator_declaration
        normalized_target = _normalize_option_text(target)
        labels = frame.locator('label.next-radio-wrapper, label[class*="radio"]')
        matched_label = None
        available: list[str] = []

        for index in range(await labels.count()):
            label = labels.nth(index)
            if not await label.is_visible():
                continue
            text = (await label.inner_text()).strip()
            if text:
                available.append(text)
            if _normalize_option_text(text) == normalized_target:
                matched_label = label
                break

        if matched_label is None:
            visible_options = "、".join(dict.fromkeys(available)) or "未读取到可见选项"
            raise RuntimeError(
                f"未找到创作者声明“{target}”。页面当前可见单选项：{visible_options}"
            )

        await matched_label.scroll_into_view_if_needed()
        await matched_label.click()
        radio = matched_label.locator('input[type="radio"]').first
        if await radio.count():
            for _ in range(10):
                if await radio.is_checked():
                    break
                await asyncio.sleep(0.1)
            else:
                raise RuntimeError(f"创作者声明“{target}”点击后未进入选中状态")
        tmall_logger.success(_msg("📋", f"创作者声明已选择：{target}"))

    async def _wait_for_publish_confirmation(
        self,
        page: Page,
        frame,
        *,
        initial_url: str,
        before_text: str,
        timeout_seconds: int = 30,
    ) -> str:
        """等待发布后的平台确认信号。

        30 秒内轮询检测：
        - 成功：页面出现"发布成功"等提示，或页面跳转到非登录/鉴权页
        - 失败：页面出现"发布失败"等提示
        - 无信号：抛出 PublishResultUncertainError

        :returns: 确认信息字符串（用于日志）
        """
        success_hints = ("发布成功", "提交成功", "已提交审核", "审核中", "发布完成")
        failure_hints = ("发布失败", "提交失败", "发布出错", "请修改后重试")

        for _ in range(timeout_seconds):
            await asyncio.sleep(1)
            current_url = page.url
            # 页面跳转到非登录/鉴权页视为成功
            if (
                current_url != initial_url
                and not _is_login_page_url(current_url)
                and not _is_auth_page_url(current_url)
            ):
                return f"页面已跳转：{current_url}"

            # 读取 frame 与 page 的文本，检测成功/失败提示
            try:
                frame_text = await frame.locator("body").inner_text(timeout=3000)
            except Exception:
                frame_text = ""
            try:
                page_text = await page.locator("body").inner_text(timeout=3000)
            except Exception:
                page_text = ""
            current_text = f"{page_text}\n{frame_text}"

            # 先检测失败（避免成功与失败提示同时出现时误判成功）
            for hint in failure_hints:
                if hint in current_text and hint not in before_text:
                    raise RuntimeError(f"平台返回发布失败提示：{hint}")
            for hint in success_hints:
                if hint in current_text and hint not in before_text:
                    return f"检测到平台成功提示：{hint}"

        raise PublishResultUncertainError(
            "已点击天猫发布按钮，但 30 秒内没有检测到明确成功或失败信号"
        )


    async def _upload_in_context(
        self,
        context: BrowserContext,
    ) -> dict:
        """在指定 BrowserContext 中执行完整的发布流程。

        流程：
        1. 校验参数
        2. 打开发布页 → 定位 iframe
        3. 上传视频 → 设置封面 → 填写标题/描述 → 参与活动 → 添加音乐 →
           关联商品 → 设置定时 → 选择创作者声明
        4. dry_run 跳过发布；否则点击发布按钮并等待确认
        5. 成功后保存 storage_state；页面保留供人工复核

        :returns: 发布结果 dict（含 mode/confirmation/final_url）
        :raises TmallAuthenticationError: Cookie 失效
        :raises PublishResultUncertainError: 发布结果不确定
        :raises Exception: 其他发布失败
        """
        tmall_logger.info(_msg("🧍", "小人先检查 cookie 和视频文件"))
        await self.validate_upload_args()
        tmall_logger.info(_msg("🥳", "上传前检查通过"))

        success = False
        submitted = False
        page = None

        try:
            page = await context.new_page()
            await page.goto(TMALL_VIDEO_PUBLISH_URL, wait_until="domcontentloaded")
            if _is_login_page_url(page.url) or _is_auth_page_url(page.url):
                raise TmallAuthenticationError("天猫 Cookie 已失效，请重新登录")
            tmall_logger.info(_msg("🧭", "小人正在赶往淘宝光合发视频页面"))
            try:
                frame = await self._find_publish_frame(page)
            except RuntimeError as exc:
                # iframe 未找到时再次检查是否被踢到登录页
                if _is_login_page_url(page.url) or _is_auth_page_url(page.url):
                    raise TmallAuthenticationError("天猫 Cookie 已失效，请重新登录") from exc
                raise
            await asyncio.sleep(3)

            # 上传视频文件
            tmall_logger.info(_msg("🏃", f"小人开始上传视频: {Path(self.file_path).name}"))
            file_input = frame.locator('input[type="file"]').first
            await file_input.set_input_files(self.file_path)
            await self._wait_for_upload_ready(frame)
            # 各步骤依次执行
            if self.cover_image_path:
                await self._set_custom_cover(frame, page)
            await self._fill_title_and_desc(frame, page)
            await self._add_activity_topic(frame, page)
            await self._add_music(frame)
            await self._add_goods(frame)
            await self._set_schedule(frame, page)
            await self._select_creator_declaration(frame)

            if self.dry_run:
                tmall_logger.info(_msg("🧪", "Dry run 模式：跳过发布，所有基础设置已完成"))
                success = True
                return {"mode": "dry_run"}

            # 真实发布：根据策略点对应按钮
            if self.schedule:
                publish_btn = frame.locator("button.next-btn-primary").filter(has_text="定时发布").first
                tmall_logger.info(_msg("🚀", f"点击定时发布按钮: {self.schedule}"))
            else:
                publish_btn = frame.locator("button.next-btn-primary").filter(has_text="立即发布").first
                tmall_logger.info(_msg("🚀", "点击立即发布按钮"))
            # 记录点击前的页面文本，用于后续检测新出现的成功/失败提示
            before_frame_text = await frame.locator("body").inner_text(timeout=3000)
            before_page_text = await page.locator("body").inner_text(timeout=3000)
            before_submit_text = f"{before_page_text}\n{before_frame_text}"
            initial_url = page.url
            await publish_btn.click()
            submitted = True
            confirmation = await self._wait_for_publish_confirmation(
                page,
                frame,
                initial_url=initial_url,
                before_text=before_submit_text,
            )
            tmall_logger.success(_msg("🥳", f"视频发布已确认（{confirmation}）"))
            success = True
            return {
                "mode": "publish",
                "confirmation": confirmation,
                "final_url": page.url,
            }
        except asyncio.CancelledError as exc:
            # 已点击发布按钮但被中断 → 结果不确定
            if submitted:
                raise PublishResultUncertainError(
                    "天猫发布按钮已经点击，但任务在取得平台确认前被中断"
                ) from exc
            raise
        except Exception as exc:
            tmall_logger.error(_msg("❌", f"UPLOAD_FAILED: {exc}"))
            raise
        finally:
            # 成功后保存 storage_state（更新 Cookie）
            if success:
                await context.storage_state(path=self.account_file)
                tmall_logger.success(_msg("🥳", "cookie 更新完毕"))
            # 页面保留供人工复核
            if page:
                try:
                    if not page.is_closed():
                        tmall_logger.info(
                            _msg(
                                "📌",
                                f"发布页面已保留供人工复核；当前账号共保留 {len(context.pages)} 个页面",
                            )
                        )
                except Exception:
                    pass

    async def upload_in_session(self, session: TmallBrowserSession) -> dict:
        """通过浏览器会话执行发布流程。

        会话校验 Cookie 后调用 _upload_in_context。
        若抛出 TmallAuthenticationError 则标记会话未认证。
        """
        context = await session.ensure_open()
        try:
            result = await self._upload_in_context(context)
        except TmallAuthenticationError:
            session.mark_authenticated(False)
            raise
        session.mark_authenticated(True)
        return result
