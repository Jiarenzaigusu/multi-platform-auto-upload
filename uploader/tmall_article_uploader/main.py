# -*- coding: utf-8 -*-
"""淘宝光合图文发布器。

图文发布拥有独立的认证、字段校验、表单填写、图片上传和确认发布流程。
它只复用天猫账号的浏览器会话池，不依赖视频发布模块或视频业务基类。
"""
from __future__ import annotations

import asyncio
import inspect
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from patchright.async_api import (
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from uploader.errors import PublishResultUncertainError
from uploader.tmall_session import TmallBrowserSession
from utils.config import DEBUG_MODE
from utils.log import tmall_logger

TMALL_CREATOR_HOME_URL = "https://creator.guanghe.taobao.com/page/"
TMALL_ARTICLE_PUBLISH_URL = (
    "https://creator.guanghe.taobao.com/page/pubNew/pic?"
    "pub_url=https%3A%2F%2Fhuodong.taobao.com%2Fwow%2Fz%2Fguang%2F"
    "gg_publish%2Fgg-picture%3Fugc_scene%3Dpc_newcreator_pic%26"
    "pageType%3Darticle%26site%3Dguangguang&pub_scene=gg"
)
TMALL_LOGIN_SUCCESS_HOST = "creator.guanghe.taobao.com"
TMALL_AUTH_HOSTS = {"passport.taobao.com"}
TMALL_PUBLISH_STRATEGY_IMMEDIATE = "immediate"
TMALL_PUBLISH_STRATEGY_SCHEDULED = "scheduled"
TMALL_MAX_GOODS_IDS = 6
TMALL_MAX_ARTICLE_IMAGES = 9
TMALL_ARTICLE_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
TMALL_MAX_ARTICLE_IMAGE_BYTES = 20 * 1024 * 1024
TMALL_EMPTY_PRODUCT_RESULT_HINTS = (
    "暂无数据", "没有找到", "没有搜到", "暂无商品", "无结果", "暂无结果",
)


class TmallAuthenticationError(RuntimeError):
    """Raised when the shared Tmall browser session is no longer authenticated."""


def _msg(emoji: str, text: str) -> str:
    return f"{emoji} {text}"


def _article_image_count_has_updated(body_text: str, expected_images: int) -> bool:
    """兼容图片计数旧版“(N/9)”和新版轮播“1/N”两种显示格式。"""
    return f"({expected_images}/9)" in body_text or bool(
        re.search(rf"(?<!\d)1\s*/\s*{expected_images}(?!\d)", body_text)
    )


async def _click_visible_article_frame_button(
    frames: tuple,
    names: tuple[str, ...],
    *,
    description: str,
    timeout_seconds: int = 30,
) -> None:
    """点击图文图片库当前可见的指定按钮。

    图文图片库是嵌套 iframe，且按钮文字 span 会覆盖 button 的命中区域。
    这里先按可见、可用和文案精确确认目标，再用 force 点击该已确认的实际按钮，
    避免依赖随页面缩放变化的坐标。
    """
    for _ in range(timeout_seconds * 2):
        for candidate in frames:
            buttons = candidate.locator('button, [role="button"], a')
            for index in range(await buttons.count()):
                button = buttons.nth(index)
                if not await button.is_visible():
                    continue
                actual_name = (await button.inner_text()).strip()
                if not actual_name:
                    actual_name = (await button.get_attribute("aria-label") or "").strip()
                normalized_name = re.sub(r"\s+", "", actual_name)
                normalized_name = re.sub(r"[（(]\d+[）)]$", "", normalized_name).strip()
                if normalized_name in names and await button.is_enabled():
                    await button.click(force=True)
                    return
        await asyncio.sleep(0.5)
    expected = "、".join(f"“{name}”" for name in names)
    visible_actions: list[str] = []
    for candidate in frames:
        actions = candidate.locator('button, [role="button"], a')
        for index in range(await actions.count()):
            action = actions.nth(index)
            if not await action.is_visible():
                continue
            action_name = (await action.inner_text()).strip()
            if not action_name:
                action_name = (await action.get_attribute("aria-label") or "").strip()
            if action_name:
                visible_actions.append(
                    f"{re.sub(r'\s+', '', action_name)}（{'可用' if await action.is_enabled() else '禁用'}）"
                )
    available = "、".join(dict.fromkeys(visible_actions)) or "无可见操作"
    raise RuntimeError(
        f"未找到可点击的天猫图文{description}按钮（期望 {expected}；当前 {available}）"
    )


async def _upload_article_picker_files(page: Page, picker_frame, image_paths: list[str]) -> None:
    """在天猫图文图片库中向一次文件选择器传入所有图片。"""
    upload_button = picker_frame.get_by_text("本地上传", exact=True).first
    try:
        async with page.expect_file_chooser(timeout=10000) as chooser_info:
            await upload_button.click(force=True, timeout=10000)
        file_chooser = await chooser_info.value
        await file_chooser.set_files(image_paths)
    except PlaywrightTimeoutError:
        # 当前页面也会出现“本地上传”先打开“上传素材”对话框的状态；此时必须
        # 继续点击其中的批量导入按钮。两种入口均属于同一个天猫图文图片库流程。
        nested_upload_button = picker_frame.locator("#sucai-tu-upload")
        try:
            await nested_upload_button.wait_for(state="visible", timeout=10000)
            async with page.expect_file_chooser(timeout=10000) as chooser_info:
                await nested_upload_button.click(force=True, timeout=10000)
            file_chooser = await chooser_info.value
            await file_chooser.set_files(image_paths)
        except PlaywrightTimeoutError as nested_exc:
            raise RuntimeError("天猫图文图片库的“本地上传”控件未打开文件选择器") from nested_exc

    # 天猫收到文件后会异步把全部图片加载进素材库。等待不足就点“完成”会让
    # 先加载的少数图片进入图库；该阶段按实测固定等待 3.5 秒。
    await asyncio.sleep(3.5)


async def _emit_qrcode_callback(qrcode_callback, payload: dict):
    """触发二维码/登录回调，支持同步与异步回调函数。"""
    if not qrcode_callback:
        return

    callback_result = qrcode_callback(payload)
    if inspect.isawaitable(callback_result):
        await callback_result


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
            await asyncio.sleep(2)
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
    qrcode_callback=None,
    *,
    session: TmallBrowserSession,
    auth_cache_seconds: float = 0,
):
    """检查淘宝光合平台 cookie 有效性，失效且 handle=True 时打开浏览器让用户手动登录。

    :param account_file: 账号 Cookie 文件路径
    :param handle: True 时若 Cookie 失效则打开可见浏览器引导用户登录
    :param return_detail: True 返回完整结果 dict，False 返回布尔
    :param qrcode_callback: 登录回调（保留参数，天猫为手动登录）
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
            qrcode_callback=qrcode_callback,
            session=session,
        )
        return result if return_detail else result["success"]

    result = _build_login_result(True, "cookie_valid", "cookie有效", account_file)
    return result if return_detail else True


async def tmall_cookie_gen(
    account_file,
    qrcode_callback=None,
    poll_interval: int = 3,
    max_checks: int = 200,
    *,
    session: TmallBrowserSession,
):
    """打开淘宝光合平台入口，等待用户手动完成登录并进入光合平台。

    不自动输入账号密码，也不绕过任何安全验证。用户在可见浏览器里完成扫码、
    密码、短信或其它淘宝安全验证后，本函数保存 storage_state。

    :param account_file: 账号 Cookie 文件路径
    :param qrcode_callback: 登录回调
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
            await _emit_qrcode_callback(
                qrcode_callback,
                {
                    "type": "manual_login",
                    "login_url": page.url,
                    "target_url": TMALL_CREATOR_HOME_URL,
                    "account_file": str(account_file),
                },
            )

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




class TmallArticle:
    """淘宝光合图文发布器，完整流程不依赖视频发布器。"""

    MIN_SCHEDULE_LEAD_TIME = timedelta(hours=2)

    def __init__(
        self,
        image_paths: list[str] | tuple[str, ...],
        title: str,
        desc: str | None,
        account_file: str,
        *,
        cover_ratio: str,
        tags: list[str] | None = None,
        goods_id: str | None = None,
        activity_topic: str | None = None,
        music_name: str | None = None,
        creator_declaration: str = "",
        schedule: datetime | None = None,
        publish_strategy: str = TMALL_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        dry_run: bool = False,
    ) -> None:
        self.image_paths = [str(path) for path in image_paths]
        self.title = title
        self.desc = desc or ""
        self.account_file = account_file
        self.tags = tags or []
        self.cover_ratio = cover_ratio
        self.goods_ids = _normalized_goods_ids(goods_id or "")
        self.goods_id = ",".join(self.goods_ids)
        self.activity_topic = activity_topic or ""
        self.music_name = (music_name or "").strip()
        self.creator_declaration = creator_declaration.strip()
        self.schedule = schedule
        self.publish_strategy = publish_strategy
        self.debug = debug
        self.dry_run = dry_run

    @classmethod
    def validate_publish_date(cls, publish_date: datetime | int | None) -> datetime | int:
        if publish_date in (None, 0):
            return 0
        if not isinstance(publish_date, datetime):
            raise TypeError("publish_date 必须是 datetime 类型或 0")
        now = datetime.now(tz=publish_date.tzinfo) if publish_date.tzinfo else datetime.now()
        if publish_date <= now:
            raise ValueError("定时发布时间必须晚于当前时间")
        if publish_date <= now + cls.MIN_SCHEDULE_LEAD_TIME:
            raise ValueError("定时发布时间必须大于当前时间 2 小时")
        return publish_date

    async def validate_upload_args(self) -> None:
        if not os.path.exists(self.account_file):
            raise RuntimeError(f"cookie文件不存在，请先完成淘宝光合平台登录: {self.account_file}")
        if not 1 <= len(self.image_paths) <= TMALL_MAX_ARTICLE_IMAGES:
            raise ValueError("天猫图文必须上传 1-9 张图片")
        normalized_paths: list[str] = []
        for image_path in self.image_paths:
            path = Path(image_path)
            if not path.is_file():
                raise ValueError(f"天猫图文图片不存在或上传未完成: {path}")
            if path.suffix.lower() not in TMALL_ARTICLE_IMAGE_EXTENSIONS:
                raise ValueError("天猫图文图片仅支持 JPG、PNG 或 WebP 格式")
            if path.stat().st_size == 0:
                raise ValueError("天猫图文图片不能为空")
            if path.stat().st_size > TMALL_MAX_ARTICLE_IMAGE_BYTES:
                raise ValueError("天猫图文单张图片不能超过 20 MiB")
            normalized_paths.append(str(path.resolve()))
        self.image_paths = normalized_paths
        if not self.title:
            raise ValueError("天猫光合图文标题不能为空")
        if len(self.title) > 30:
            raise ValueError("天猫光合图文标题不能超过30字")
        if len(self.tags) > 4:
            tmall_logger.warning(
                _msg("⚠️", f"话题标签最多4个，已自动截取前4个（传入了 {len(self.tags)} 个）")
            )
            self.tags = self.tags[:4]
        tag_text = "".join(f" #{tag}" for tag in self._normalized_tags())
        if len(self.desc + tag_text) > 1000:
            raise ValueError("天猫光合图文描述不能超过1000字")
        if len(self.goods_ids) > TMALL_MAX_GOODS_IDS:
            raise ValueError(f"天猫一次最多关联 {TMALL_MAX_GOODS_IDS} 个商品ID")
        if any(not goods_id.isdigit() for goods_id in self.goods_ids):
            raise ValueError("天猫光合商品ID必须为数字，多个ID请使用逗号或换行分隔")
        if len(self.music_name) > 100:
            raise ValueError("天猫音乐名称不能超过100个字符")
        if self.schedule:
            self.validate_publish_date(self.schedule)
        if not self.creator_declaration:
            raise ValueError("天猫创作者声明不能为空")
        if self.cover_ratio not in {"original", "3:4", "1:1"}:
            raise ValueError("天猫图文图片比例必须为原始、3:4 或 1:1")
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
        """填写内容标题与描述，并逐个输入话题标签。

        描述区是淘宝"仓颉"富文本编辑器（contenteditable div），不是真正的 textarea。
        直接用 fill() 改 textarea.value 不会触发 hashtag 识别，必须 click 聚焦后
        逐字符 type。话题标签通过 #xxx + 空格触发平台话题下拉建议并选中首项。
        """
        # 填写标题
        title_input = frame.locator('input[placeholder="加个标题让内容更吸引人"]').first
        await title_input.wait_for(state="visible", timeout=10000)
        await title_input.fill(self.title[:30])
        tmall_logger.info(_msg("✍️", f"内容标题已填写: {self.title[:30]}"))

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
            tmall_logger.info(_msg("✍️", f"内容描述已填写: {desc[:30]}"))

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
        # 不同内容表单的文字节点可能被子元素覆盖，强制点击语义入口以兼容布局差异。
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
        # 表单选中音乐后，入口标签可能从“点击添加音乐”变为“更多音乐”。
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

    async def _find_publish_frame(self, page: Page):
        """定位包含图文发布表单的 gg-picture iframe。"""
        for _ in range(30):
            for frame in page.frames:
                if "gg_publish/gg-picture" in frame.url:
                    return frame
            await asyncio.sleep(1)
        raise RuntimeError("未找到淘宝光合图文发布 iframe")

    async def _upload_images(self, frame, page: Page) -> None:
        """通过天猫图片库上传并精确勾选本次图文图片。

        图文发布器本身不含 file input。真实流程必须经由独立图片库完成：打开
        图库、批量本地上传、在上传结果页确认、只选中本次新增素材、图库确认，
        最后等待主表单回写图片数量。该流程不调用视频封面模块。
        """
        uploader = frame.locator("#picture-upload-wrapper .next-picture-uploader").first
        await uploader.wait_for(state="visible", timeout=15000)
        # 已实际确认该组件的 ::before 会截获普通点击；定位到唯一上传容器后强制点击。
        await uploader.click(force=True)

        picker_frame = None
        for _ in range(40):
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
            raise RuntimeError("未找到天猫图文图片库 iframe，无法上传图片")

        await _upload_article_picker_files(page, picker_frame, self.image_paths)

        # 文件传输完成会先显示上传结果，“完成”后才会返回可勾选的图片库。
        await _click_visible_article_frame_button(
            (picker_frame,), ("完成",), description="图片上传完成", timeout_seconds=120
        )

        expected_images = len(self.image_paths)
        expected_image_stems = [Path(image_path).stem.casefold() for image_path in self.image_paths]
        selected_images = None
        # “完成”后图库仍会逐张回写。持续等待每个唯一文件名真实出现，不能以
        # 固定时长代替这个确认，避免只选到先挂载的一部分图片。
        for _ in range(120):
            selected_images = await picker_frame.evaluate(
                r"""(expectedStems) => {
                    // 图库会在新图片之间插入历史素材，不能按卡片位置选择。Web
                    // 暂存层已将每张图改为含任务唯一标识的文件名；这里必须只接受
                    // 卡片文本中恰好出现一个完整文件名 stem 的素材，命中不唯一就中止。
                    const cards = [...document.querySelectorAll('label')].filter(card =>
                        card.querySelector('.PicList_pic_imgBox__c0HXw img')
                        && card.querySelector('input[type="checkbox"], input[type="radio"]')
                    );
                    const usedCards = new Set();
                    const controls = [];
                    const missing = [];
                    const ambiguous = [];
                    const matchingCards = {};
                    for (const expectedStem of expectedStems) {
                        const matches = cards.filter(candidate => {
                            if (usedCards.has(candidate)) return false;
                            const cardText = (candidate.parentElement?.innerText || candidate.innerText || '')
                                .toLocaleLowerCase();
                            return cardText.includes(expectedStem);
                        });
                        matchingCards[expectedStem] = matches.map(candidate =>
                            (candidate.parentElement?.innerText || candidate.innerText || '')
                                .replace(/\s+/g, ' ').trim()
                        );
                        if (matches.length !== 1) {
                            missing.push(expectedStem);
                            if (matches.length > 1) ambiguous.push(expectedStem);
                            continue;
                        }
                        const card = matches[0];
                        const control = card.querySelector(
                            'input[type="checkbox"], input[type="radio"]'
                        );
                        if (!control) {
                            missing.push(expectedStem);
                            continue;
                        }
                        usedCards.add(card);
                        controls.push(control);
                    }
                    for (const control of controls) {
                        control.setAttribute('data-mpau-new-article-image', 'true');
                    }
                    return {
                        count: controls.length,
                        missing,
                        ambiguous,
                        matchingCards,
                    };
                }""",
                expected_image_stems,
            )
            if selected_images and selected_images.get("count") == expected_images:
                break
            await asyncio.sleep(0.5)
        if not selected_images or selected_images.get("count") != expected_images:
            actual = selected_images.get("count", 0) if selected_images else 0
            missing = ", ".join(selected_images.get("missing", []) if selected_images else [])
            ambiguous = ", ".join(selected_images.get("ambiguous", []) if selected_images else [])
            matching_cards = selected_images.get("matchingCards", {}) if selected_images else {}
            observed = " | ".join(
                f"{stem}: {' / '.join(card_texts) or '未出现在图库'}"
                for stem, card_texts in matching_cards.items()
            )
            raise RuntimeError(
                f"图片库未精确识别本次上传的 {expected_images} 张图文图片（识别到 {actual} 张），"
                f"未找到或重复的文件名：{missing or '未知'}"
                f"{f'；重复命中：{ambiguous}' if ambiguous else ''}；图库匹配详情："
                f"{observed or '未获得'}，已停止避免选择错误素材。"
            )

        selected_controls = picker_frame.locator('input[data-mpau-new-article-image="true"]')
        for index in range(expected_images):
            control = selected_controls.nth(index)
            if not await control.is_checked():
                # Next 组件隐藏原生 checkbox；只对本次唯一文件名锁定的控件触发事件。
                await control.evaluate("(control) => control.click()")
            if not await control.is_checked():
                raise RuntimeError("本次上传图文图片未进入选中状态，已停止避免选择错误素材。")

        await _click_visible_article_frame_button(
            (picker_frame,), ("确定",), description="图片库确认", timeout_seconds=30
        )

        for _ in range(90):
            body_text = await frame.locator("body").inner_text(timeout=3000)
            if "上传失败" in body_text or "上传出错" in body_text:
                raise RuntimeError("天猫图文图片上传失败，请检查页面提示")
            if _article_image_count_has_updated(body_text, expected_images):
                tmall_logger.success(_msg("🖼️", f"已上传并选中 {expected_images} 张图文图片"))
                return
            await asyncio.sleep(2)
        raise RuntimeError("天猫图文图片库确认后未回写图片数量")

    async def _crop_uploaded_images(self, frame, ratio: str) -> None:
        """将刚上传的全部图文图片逐张裁剪为指定比例并确认。

        发布器需先悬浮缩略图，图片上的“裁剪”操作才会显示。裁剪弹窗虽提示
        支持批量操作，但实测图片缩略图每次只保留单选状态，因此这里逐张选中
        并设置 3:4，避免只裁剪当前第一张图片。
        """
        expected_images = len(self.image_paths)
        crop_entries = frame.get_by_text("裁剪", exact=True)
        for _ in range(30):
            if await crop_entries.count() == expected_images:
                break
            await asyncio.sleep(0.5)
        else:
            actual_entries = await crop_entries.count()
            raise RuntimeError(
                f"图片上传后未找到全部裁剪入口（期望 {expected_images} 个，实际 {actual_entries} 个）"
            )

        first_crop_entry = crop_entries.first
        await first_crop_entry.hover()
        await first_crop_entry.click(force=True)

        crop_dialog = frame.get_by_role("dialog", name="裁剪图片")
        await crop_dialog.wait_for(state="visible", timeout=15000)
        thumbnails = crop_dialog.locator('div[class*="picture-upload--image--"]')
        for _ in range(30):
            if await thumbnails.count() == expected_images:
                break
            await asyncio.sleep(0.5)
        else:
            actual_thumbnails = await thumbnails.count()
            raise RuntimeError(
                f"裁剪弹窗未加载全部图片（期望 {expected_images} 张，实际 {actual_thumbnails} 张）"
            )

        # 比例同时有卡片外层和文字内层；平台在实际运行时只会稳定响应文字
        # 内层的点击。比例的激活样式位于其父卡片上。
        ratio_text = crop_dialog.locator(
            'div[class*="picture-upload--ratio_text--"]'
        ).filter(has_text=re.compile(rf"^{re.escape(ratio)}$"))
        for index in range(expected_images):
            thumbnail = thumbnails.nth(index)
            # 图片缩略图由内部 img 接收鼠标事件，比例和确认按钮也会被子 span 覆盖。
            await thumbnail.click(force=True)
            for _ in range(20):
                if await thumbnail.locator('div[class*="image_active_border"]').count():
                    break
                await asyncio.sleep(0.2)
            else:
                raise RuntimeError(f"第 {index + 1} 张图文图片未成功选中以进行裁剪")

            # 缩略图选中后，平台会异步重建左侧裁剪画布；只等激活边框出现还不够。
            # 留出画布切换时间后点击比例文字，让事件冒泡至比例卡片。
            await asyncio.sleep(0.5)
            await ratio_text.click(force=True)
            for _ in range(20):
                is_selected = await ratio_text.evaluate(
                    "(text) => text.parentElement.className.includes('ratio_active')"
                )
                if is_selected:
                    break
                await asyncio.sleep(0.2)
            else:
                raise RuntimeError(f"第 {index + 1} 张图文图片未成功设置为 {ratio} 裁剪比例")

        confirm_button = crop_dialog.get_by_role("button", name="确定", exact=True)
        await confirm_button.click(force=True)
        await crop_dialog.wait_for(state="hidden", timeout=15000)
        tmall_logger.success(_msg("✂️", f"已将 {expected_images} 张图文图片裁剪为 {ratio}"))

    async def _crop_images_if_requested(self, frame) -> None:
        """原始比例跳过裁剪，其他比例进入逐张裁剪流程。"""
        if self.cover_ratio == "original":
            return
        await self._crop_uploaded_images(frame, self.cover_ratio)

    async def _upload_in_context(self, context: BrowserContext) -> dict:
        """执行完整的天猫图文发布流程。"""
        tmall_logger.info(_msg("🧍", "小人先检查 cookie 和图文图片"))
        await self.validate_upload_args()
        success = False
        submitted = False
        page = None
        try:
            page = await context.new_page()
            await page.goto(TMALL_ARTICLE_PUBLISH_URL, wait_until="domcontentloaded")
            if _is_login_page_url(page.url) or _is_auth_page_url(page.url):
                raise TmallAuthenticationError("天猫 Cookie 已失效，请重新登录")
            frame = await self._find_publish_frame(page)
            await asyncio.sleep(3)
            await self._upload_images(frame, page)
            await self._crop_images_if_requested(frame)
            await self._fill_title_and_desc(frame, page)
            await self._add_activity_topic(frame, page)
            await self._add_music(frame)
            await self._add_goods(frame)
            await self._set_schedule(frame, page)
            await self._select_creator_declaration(frame)

            if self.dry_run:
                success = True
                tmall_logger.info(_msg("🧪", "图文 Dry run 模式：跳过正式发布"))
                return {"mode": "dry_run"}

            button_text = "定时发布" if self.schedule else "立即发布"
            publish_button = frame.locator("button.next-btn-primary").filter(has_text=button_text).first
            before_text = "\n".join((
                await page.locator("body").inner_text(timeout=3000),
                await frame.locator("body").inner_text(timeout=3000),
            ))
            initial_url = page.url
            await publish_button.click()
            submitted = True
            confirmation = await self._wait_for_publish_confirmation(
                page, frame, initial_url=initial_url, before_text=before_text
            )
            success = True
            return {"mode": "publish", "confirmation": confirmation, "final_url": page.url}
        except asyncio.CancelledError as exc:
            if submitted:
                raise PublishResultUncertainError(
                    "天猫图文发布按钮已点击，但任务在确认前被中断"
                ) from exc
            raise
        finally:
            if success:
                await context.storage_state(path=self.account_file)
            if page and not page.is_closed():
                tmall_logger.info(_msg("📌", "图文发布页面已保留供人工复核"))

    async def upload_in_session(self, session: TmallBrowserSession) -> dict:
        """通过天猫共享浏览器会话执行图文发布。"""
        context = await session.ensure_open()
        try:
            result = await self._upload_in_context(context)
        except TmallAuthenticationError:
            session.mark_authenticated(False)
            raise
        session.mark_authenticated(True)
        return result
