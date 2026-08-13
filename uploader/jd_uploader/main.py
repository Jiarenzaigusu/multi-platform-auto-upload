# -*- coding: utf-8 -*-
"""
uploader.jd_uploader.main 模块

京东京麦平台（dr.jd.com）视频发布器核心实现。

主要功能：
1. Cookie 校验：访问发布中心判断是否仍处于登录态
2. 手动登录：打开可见浏览器，等待用户完成扫码/密码/短信等验证后保存 storage_state
3. 视频发布：上传视频 → 填写标题 → 关联商品（可选）→ 选择创作者声明 →
            开启自主原创（可选）→ 设置定时（可选）→ 点击发布按钮 →
            处理验证码（人工介入）→ 等待确认

注意事项：
- 京麦页面对 document.body.innerText 做了限制，登录后 innerText 只返回 '👋'，
  所以通过 HTML 体量间接判断登录状态
- 京麦发布页是微前端，真实表单在 iframe.micro-iframe 中
- 验证码需人工完成，本模块会暂停并等待
- 发布结果可能"不确定"，会抛出 PublishResultUncertainError
"""
from __future__ import annotations

import asyncio
import inspect
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from patchright.async_api import BrowserContext, Frame

from uploader.errors import PublishResultUncertainError
from utils.config import DEBUG_MODE
from uploader.base_video import BaseVideoUploader
from uploader.jd_uploader.session import JdBrowserSession
from utils.log import jd_logger

# 京东京麦发布中心 URL，用于 Cookie 校验与登录入口
JD_POST_CENTER_URL = "https://dr.jd.com/jm/#/n/post-center.html"
# 京东京麦视频发布页 URL
JD_PUBLISH_VIDEO_URL = "https://dr.jd.com/jm/#/n/publish-video.html?platform=jm-pop"
# 登录成功后的目标 host，URL 命中此 host 表示已进入京麦后台
JD_LOGIN_SUCCESS_HOST = "dr.jd.com"
# passport.* / safe.* 都视作「用户正在登录中」的中间态，继续等待，不当成失败也不当成成功
JD_AUTH_HOSTS = {"passport.shop.jd.com", "passport.jd.com", "safe.jd.com"}

# 发布策略常量
JD_PUBLISH_STRATEGY_IMMEDIATE = "immediate"
JD_PUBLISH_STRATEGY_SCHEDULED = "scheduled"


class JdAuthenticationError(RuntimeError):
    """京东 Cookie 已失效异常。

    当发布或校验流程中发现页面被重定向到鉴权页时抛出，
    上层会捕获此异常并将会话标记为未认证。
    """
    pass


def _contains_exact_goods_id(value: str, goods_id: str) -> bool:
    """Avoid associating a product when its ID only partially matches the result."""
    return re.search(rf"(?<!\d){re.escape(goods_id)}(?!\d)", value) is not None


def _msg(emoji: str, text: str) -> str:
    """统一日志格式：emoji + 文本。"""
    return f"{emoji} {text}"


async def _emit_qrcode_callback(qrcode_callback, payload: dict):
    """触发二维码/登录回调，支持同步与异步回调函数。"""
    if not qrcode_callback:
        return
    callback_result = qrcode_callback(payload)
    if inspect.isawaitable(callback_result):
        await callback_result


def _build_login_result(success, status, message, account_file, current_url=""):
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


async def _is_logged_in(page) -> bool:
    """判断当前 page 是否已登录京麦后台。

    判定规则（实测可靠）：
    1. URL host 是 dr.jd.com 且 path 以 /jm 开头
    2. 浏览器标题严格为「京麦」（未登录被踢到 passport 时 title 是「京麦工作台-京东商家一站式工作台」）
    3. body outerHTML 体量 ≥ 50KB（登录页 ~9KB；登录后 ~600KB）

    ⚠️ 京麦页面对 document.body.innerText 做了限制，登录后 innerText 只返回 '👋'
       一个 emoji，不能用来判定。所以才用 HTML 体量这个间接信号。
    """
    if _url_host(page.url) in JD_AUTH_HOSTS:
        return False
    parsed = urlparse(page.url)
    if _url_host(page.url) != JD_LOGIN_SUCCESS_HOST or not parsed.path.startswith("/jm"):
        return False
    try:
        title = await page.title()
        body_html_len = await page.evaluate(
            "() => document.body ? document.body.outerHTML.length : 0"
        )
    except Exception:
        return False
    return title.strip() == "京麦" and body_html_len >= 50_000


async def _cookie_auth_in_context(context: BrowserContext) -> bool:
    """在指定 BrowserContext 中校验京东 Cookie 是否有效。

    访问发布中心，最多等待 8 轮（每轮 2 秒）观察是否进入登录态。
    """
    page = await context.new_page()
    try:
        await page.goto(JD_POST_CENTER_URL, wait_until="domcontentloaded")
        for _ in range(8):
            await asyncio.sleep(2)
            if _url_host(page.url) in JD_AUTH_HOSTS:
                return False
            if await _is_logged_in(page):
                return True
        return False
    finally:
        await page.close()


async def cookie_auth(
    account_file,
    *,
    session: JdBrowserSession,
    max_age_seconds: float = 0,
):
    """验证京东京麦 cookie 是否有效。

    :param account_file: 账号 Cookie 文件路径
    :param session: 浏览器会话
    :param max_age_seconds: 鉴权缓存有效期，<=0 不使用缓存
    :returns: True 有效，False 失效
    """
    # 优先复用鉴权缓存
    if session.auth_is_fresh(max_age_seconds):
        return True
    context = await session.ensure_open()
    try:
        authenticated = await _cookie_auth_in_context(context)
    except Exception as exc:
        jd_logger.warning(_msg("😵", f"cookie 校验出错，按失效处理: {exc}"))
        authenticated = False
    session.mark_authenticated(authenticated)
    return authenticated


async def jd_setup(
    account_file,
    handle=False,
    return_detail=False,
    qrcode_callback=None,
    *,
    session: JdBrowserSession,
    auth_cache_seconds: float = 0,
):
    """检查 cookie；失效且 handle=True 时打开浏览器让用户手动登录。

    :param account_file: 账号 Cookie 文件路径
    :param handle: True 时若 Cookie 失效则打开可见浏览器引导用户登录
    :param return_detail: True 返回完整结果 dict，False 返回布尔
    :param qrcode_callback: 登录回调
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
            result = _build_login_result(False, "cookie_invalid", "cookie文件不存在或已失效", account_file)
            return result if return_detail else False
        jd_logger.info(_msg("🥹", "cookie 失效了，准备打开浏览器让用户手动登录京东京麦"))
        result = await jd_cookie_gen(
            account_file,
            qrcode_callback=qrcode_callback,
            session=session,
        )
        return result if return_detail else result["success"]

    result = _build_login_result(True, "cookie_valid", "cookie有效", account_file)
    return result if return_detail else True


async def jd_cookie_gen(
    account_file,
    qrcode_callback=None,
    poll_interval: int = 3,
    max_checks: int = 200,
    *,
    session: JdBrowserSession,
):
    """打开京麦发布中心，等待用户在浏览器内完成登录（密码 / 短信 / 扫码），成功后保存 storage_state。

    :param account_file: 账号 Cookie 文件路径
    :param qrcode_callback: 登录回调
    :param poll_interval: 轮询间隔秒数
    :param max_checks: 最大轮询次数（默认 200 次）
    :param session: 浏览器会话
    :returns: 登录结果 dict

    注意：京东可能在新的 tab 中完成认证，因此需要遍历 context.pages 检测登录态。
    """
    context = await session.ensure_open()
    # 记录本次登录前已有的 page，登录流程结束后只清理新建的 page
    existing_page_ids = {id(open_page) for open_page in context.pages}
    result = _build_login_result(False, "failed", "京东京麦登录失败", account_file)
    page = None

    try:
        page = await context.new_page()
        await page.goto(JD_POST_CENTER_URL, wait_until="domcontentloaded")
        jd_logger.info(_msg("🧍", "已打开京东京麦发布中心入口，请在浏览器中完成登录"))
        await _emit_qrcode_callback(qrcode_callback, {
            "type": "manual_login",
            "login_url": page.url,
            "target_url": JD_POST_CENTER_URL,
            "account_file": str(account_file),
        })

        # 等待 5 秒让入口 URL 的 JS 跳转稳定，避免误判
        await asyncio.sleep(5)

        # 轮询等待用户完成登录
        for tick in range(max_checks):
            # 京东可能在新 tab 中完成认证，遍历所有 page
            hit = None
            for candidate in context.pages:
                if await _is_logged_in(candidate):
                    hit = candidate
                    break
            if hit is not None:
                page = hit
                jd_logger.info(_msg("🥳", f"检测到已进入京东京麦: {page.url}"))
                break

            if tick % 10 == 0:
                jd_logger.info(_msg("⏳", f"等待用户完成登录: {[p.url for p in context.pages]}"))
            await asyncio.sleep(poll_interval)
        else:
            # for...else：循环正常结束（未 break）表示超时
            return _build_login_result(False, "timeout", "等待京东京麦登录超时", account_file, page.url)

        await asyncio.sleep(3)
        await context.storage_state(path=account_file)
        jd_logger.info(_msg("💾", f"cookie 已保存: {account_file}"))

        jd_logger.success(_msg("🥳", "京东京麦登录成功，cookie 验证通过"))
        result = _build_login_result(True, "success", "京东京麦登录成功", account_file, page.url)
        session.mark_authenticated(True)
    except Exception as exc:
        result = _build_login_result(False, "failed", str(exc), account_file,
                                     current_url=page.url if page else "")
    finally:
        if not result["success"]:
            jd_logger.error(_msg("😢", f"登录失败: {result['message']}"))
        # 只清理本次登录流程新建的页面，保留之前累积的发布页供人工复核
        for open_page in [
            candidate for candidate in list(context.pages)
            if id(candidate) not in existing_page_ids
        ]:
            try:
                await open_page.close()
            except Exception:
                pass

    return result


class JDBaseUploader(BaseVideoUploader):
    """京东京麦上传器基类。

    提供账号文件存在性校验，被 JDVideo 继承。
    """

    def __init__(self, account_file, debug: bool = DEBUG_MODE):
        """初始化基类。

        :param account_file: 账号 Cookie 文件路径
        :param debug: 是否调试模式
        """
        self.account_file = account_file
        self.debug = debug

    async def validate_base_args(self):
        """校验账号 Cookie 文件存在。"""
        if not os.path.exists(self.account_file):
            raise RuntimeError(f"cookie文件不存在，请先完成京东京麦登录: {self.account_file}")


async def _find_publish_iframe(page, timeout_seconds: int = 30) -> Frame:
    """定位京麦视频发布 iframe。

    京麦发布页是微前端，真实表单在 iframe.micro-iframe（src=/n/publish-video.html）里。
    最多等待 30 秒。
    """
    for _ in range(timeout_seconds):
        for f in page.frames:
            if f != page.main_frame and "publish-video.html" in f.url:
                return f
        await asyncio.sleep(1)
    raise RuntimeError("未找到京麦视频发布 iframe")


class JDVideo(JDBaseUploader):
    """京东京麦视频发布器。

    完整发布流程：
    1. validate_upload_args: 校验所有参数
    2. 打开发布页 → 定位发布 iframe → 上传视频文件
    3. 等待视频上传完成（发布按钮 disabled 消失）
    4. 填写标题 → 关联商品（可选）→ 选择创作者声明 → 开启自主原创（可选）→ 设置定时（可选）
    5. dry_run 跳过发布；否则点击发布按钮 → 处理验证码（人工）→ 等待确认
    6. 成功后保存 storage_state；页面保留供人工复核
    """

    def __init__(
        self,
        file_path: str,
        title: str,
        account_file,
        goods_id: str | None = None,
        schedule: datetime | None = None,
        original: bool = False,
        creator_declaration: str = "",
        *,
        debug: bool = DEBUG_MODE,
        dry_run: bool = False,
    ):
        """初始化发布参数。

        :param file_path: 视频文件路径
        :param title: 视频标题（5-27 字）
        :param account_file: 账号 Cookie 文件路径
        :param goods_id: 商品 ID（可选，最多 1 个）
        :param schedule: 定时发布时间（None 立即发布）
        :param original: 是否开启"自主原创"开关
        :param creator_declaration: 创作者声明（必填）
        :param debug: 调试模式
        :param dry_run: True 只走流程不点发布按钮
        """
        super().__init__(account_file=account_file, debug=debug)
        self.file_path = file_path
        self.title = title
        self.goods_id = (goods_id or "").strip()
        self.schedule = schedule
        self.original = original
        self.creator_declaration = creator_declaration.strip()
        self.dry_run = dry_run

    async def validate_upload_args(self):
        """校验所有发布参数。"""
        await self.validate_base_args()
        # 校验视频文件
        self.file_path = str(self.validate_video_file(self.file_path))
        # 标题长度校验（5-27 字）
        title = (self.title or "").strip()
        if not title:
            raise ValueError("京东视频标题不能为空")
        if not (5 <= len(title) <= 27):
            raise ValueError(f"京东视频标题长度必须 5-27 字（当前 {len(title)} 字）")
        self.title = title
        # 商品 ID 必须为纯数字
        if self.goods_id and not self.goods_id.isdigit():
            raise ValueError(f"京东商品 ID 必须为纯数字: {self.goods_id}")
        # 定时发布时间校验
        if self.schedule is not None:
            self.validate_publish_date(self.schedule)
        # 创作者声明必填
        if not self.creator_declaration:
            raise ValueError("京东创作声明不能为空")

    async def _wait_for_video_uploaded(self, frame: Frame, timeout_seconds: int = 600):
        """等视频上传完成。判定信号：发布按钮 disabled 属性消失。

        :param frame: 发布 iframe
        :param timeout_seconds: 超时秒数（默认 600 秒 = 10 分钟）
        """
        for i in range(timeout_seconds // 2):
            disabled = await frame.evaluate(
                """() => {
                    const b = document.querySelector('button[class*="publishBtn"]');
                    return b ? b.disabled : true;
                }"""
            )
            if disabled is False:
                jd_logger.success(_msg("🥳", f"视频上传完成（{i*2}s 后发布按钮可点）"))
                return
            if i % 5 == 0:
                jd_logger.info(_msg("🏃", f"小人正在等待视频上传完成 ({i*2}s)"))
            await asyncio.sleep(2)
        raise RuntimeError(f"等待视频上传完成超时（{timeout_seconds}s）")

    async def _add_goods(self, page, frame: Frame):
        """通过商品 ID 在「站内搜索」tab 关联商品。

        流程：点 + → 切站内搜索 tab → 输入 ID → 点查询 →
              等结果出现（含 ¥）→ 勾选第一个商品卡 → 点确定 → 等 drawer 关闭。

        四种终态判定：
        1. 出现 ¥ 价格 → 找到商品，可勾选
        2. 平台返回明确无结果文案 → ID 不存在
        3. 平台返回「失效原因」卡片 → ID 格式正确但商品不可用
        4. 20s 内既无 ¥ 也无文案 → 接口超时
        """
        if not self.goods_id:
            return

        jd_logger.info(_msg("🛒", f"小人准备添加商品: {self.goods_id}"))

        # 点关联挂件区域的 + 按钮
        plus_btn = frame.locator('div[class*="addgoods-upload"]').first
        await plus_btn.scroll_into_view_if_needed()
        await plus_btn.click()

        # 等 drawer 打开。京东 tab id（如 rc-tabs-0-tab-2）是动态生成的，不能作为稳定选择器
        drawer = frame.locator('.jd-drawer-open, .jd-drawer-wrapper-body').first
        await drawer.wait_for(state="visible", timeout=10000)
        await asyncio.sleep(1)

        # 切到「站内搜索」tab：优先按 role+文本定位，避免依赖动态 id
        site_tab = frame.locator('.jd-drawer-wrapper-body [role="tab"]').filter(has_text="站内搜索").first
        if not await site_tab.count():
            site_tab = frame.locator('.jd-drawer-wrapper-body .jd-tabs-tab-btn').filter(has_text="站内搜索").first
        await site_tab.wait_for(state="visible", timeout=10000)
        await site_tab.click()
        await asyncio.sleep(1.5)

        # 找当前激活的 tab panel。不同会话的 rc-tabs id 可能变化，
        # 优先取 aria-hidden=false 的 active panel
        active_panel = frame.locator('.jd-drawer-wrapper-body [role="tabpanel"][aria-hidden="false"]').first
        if not await active_panel.count():
            active_panel = frame.locator('.jd-drawer-wrapper-body .jd-tabs-tabpane-active').first
        await active_panel.wait_for(state="visible", timeout=5000)

        # 输入商品 ID
        site_input = active_panel.locator('input').first
        await site_input.wait_for(state="visible", timeout=5000)
        await site_input.click()
        await site_input.fill(self.goods_id)
        await asyncio.sleep(0.5)

        # 点查询
        query_btn = active_panel.locator('button').filter(has_text="查询").first
        if not await query_btn.count():
            query_btn = frame.locator('.jd-drawer-wrapper-body button.jd-btn-primary').filter(has_text="查询").first
        await query_btn.wait_for(state="visible", timeout=5000)
        await query_btn.click()
        jd_logger.info(_msg("🔎", f"已点查询，搜索商品 ID: {self.goods_id}"))

        # 等查询结果，四种终态判定
        INVALID_HINTS = ("暂无数据", "没有找到", "无结果", "未搜索到")
        result_text = ""
        for _ in range(20):
            await asyncio.sleep(1)
            result_text = await active_panel.evaluate("el => el.innerText || ''")
            # 终态 1：出现价格 → 找到商品
            if "¥" in result_text or "￥" in result_text:
                break
            # 终态 2：明确无结果
            if any(k in result_text for k in INVALID_HINTS):
                raise ValueError(
                    f"商品 ID {self.goods_id} 在京东站内搜索无结果。请核实 ID 是否正确，或使用「本店商品」tab。"
                )
            # 终态 3：失效卡（实测格式：「<id>\n失效原因: <reason>」）
            if "失效原因" in result_text:
                reason = "未知"
                if "失效原因:" in result_text:
                    reason = result_text.split("失效原因:", 1)[1].split("\n", 1)[0].strip() or reason
                raise ValueError(
                    f"商品 ID {self.goods_id} 在京东站内搜索不可用（失效原因: {reason}）。"
                    "请核实 ID 是否正确、商品是否上架。"
                )
        else:
            # 终态 4：超时
            raise RuntimeError(
                f"商品 ID {self.goods_id} 搜索 20s 内未返回有效商品（结果区无 ¥/￥ 价格）。"
                "建议用 --headed 观察。"
            )

        # 勾选第一个商品卡。表头还有一个 goods-card-header-check 是「全选」，要排除
        goods_check = active_panel.locator('label.jd-checkbox-wrapper.goods-card-check').first
        if not await goods_check.count():
            # 兜底：找 active panel 里第一个不是 header-check 的 checkbox-wrapper
            goods_check = active_panel.locator('label.jd-checkbox-wrapper:not(.goods-card-header-check)').first
        await goods_check.wait_for(state="visible", timeout=5000)
        await goods_check.click()
        jd_logger.info(_msg("✅", "已勾选商品"))
        await asyncio.sleep(1)

        # 点 drawer 底部的「确定」
        confirm_btn = frame.locator('.jd-drawer-wrapper-body button.jd-btn-primary').filter(has_text="确定").first
        await confirm_btn.click()
        # 等 drawer 关闭
        drawer = frame.locator('.jd-drawer-wrapper-body')
        try:
            await drawer.wait_for(state="hidden", timeout=10000)
        except Exception:
            # 兜底：drawer 关闭可能是内部状态切换而不是 DOM 移除
            await asyncio.sleep(2)
        jd_logger.success(_msg("🛒", f"商品 {self.goods_id} 已关联"))

    async def _select_creator_declaration(self, frame: Frame):
        """选择运营人员指定的创作者声明下拉项。

        DOM：.content-declaration-wrapper .jd-select-selector
        点开后 options 渲染到 portal，selector 为 div.jd-select-item-option。

        实测可选值（label 属性）：
        - 含AI生成内容 / 含虚构演绎内容 / 内容为转载 / 个人观点，仅供参考 /
          内容含营销广告 / 内容无需标注
        """
        declaration = frame.locator('.content-declaration-wrapper .jd-select-selector').first
        await declaration.scroll_into_view_if_needed()
        await declaration.click()
        await asyncio.sleep(1)

        # 按 label 属性精确匹配
        target_option = frame.locator(
            f'div.jd-select-item-option[label="{self.creator_declaration}"]'
        ).first
        if not await target_option.count():
            raise RuntimeError(
                f"未找到创作声明“{self.creator_declaration}”，页面选项可能已变化"
            )

        # 二次校验：防止 label 属性与 innerText 不一致
        text = (await target_option.evaluate("el => el.getAttribute('label') || el.innerText") or "").strip()
        if text != self.creator_declaration:
            raise RuntimeError(
                f"创作声明校验失败：期望“{self.creator_declaration}”，实际“{text}”"
            )
        await target_option.click()
        await asyncio.sleep(0.5)
        jd_logger.success(_msg("📋", f"创作声明已选择: {text}"))

    async def _set_original(self, frame: Frame):
        """开启「自主原创」switch。

        DOM 结构：
        - label[title="自主原创"] 旁边有 button[role="switch"]
        - 可用时：class="jd-switch"，aria-checked="false"/"true"
        - 禁用时：class="jd-switch jd-switch-disabled"，disabled=""，style="pointer-events: none"
          （可能是账号资质未达标或类目不支持）

        只有用户传了 --original 时才进入此方法。
        switch 禁用时直接报错，不绕过。
        """
        if not self.original:
            return

        # 通过 JS 查找「自主原创」label 并向上找对应 switch 按钮
        switch_state = await frame.evaluate(r"""
            () => {
                const lbl = [...document.querySelectorAll('label')]
                    .find(l => l.title === '自主原创' || (l.innerText || '').trim() === '自主原创');
                if (!lbl) return { found: false };
                let n = lbl;
                for (let i = 0; i < 6 && n.parentElement; i++) {
                    n = n.parentElement;
                    const sw = n.querySelector('button[role="switch"]');
                    if (sw) return {
                        found: true,
                        disabled: sw.disabled,
                        aria_checked: sw.getAttribute('aria-checked'),
                    };
                }
                return { found: true, disabled: null, aria_checked: null, error: 'switch_not_found' };
            }
        """)

        if not switch_state.get("found"):
            raise RuntimeError("页面未找到「自主原创」选项，可能页面结构已变化。")

        if switch_state.get("error") == "switch_not_found":
            raise RuntimeError("找到「自主原创」label 但未找到对应 switch 按钮。")

        # switch 禁用时报错（不绕过）
        if switch_state.get("disabled"):
            raise ValueError(
                "该账号的「自主原创」switch 当前不可用（可能是账号资质未达标或该类目不支持）。"
                "请在京东商家后台确认账号是否已开通原创功能，或去掉 --original 参数后重试。"
            )

        # 已经开启则跳过
        if switch_state.get("aria_checked") == "true":
            jd_logger.info(_msg("✅", "自主原创已经是开启状态，跳过"))
            return

        # 用 Playwright locator 点击（确保触发 React 事件）
        sw_locator = frame.locator('label[title="自主原创"]').locator(
            "xpath=ancestor::*[position()<=5]//button[@role='switch']"
        ).first
        if not await sw_locator.count():
            # 兜底：页面中唯一一个非 disabled switch
            sw_locator = frame.locator('button[role="switch"]:not([disabled])').first
        await sw_locator.click()
        await asyncio.sleep(0.5)

        # 校验 aria-checked 变成 true
        checked = await frame.evaluate(r"""
            () => {
                const lbl = [...document.querySelectorAll('label')]
                    .find(l => l.title === '自主原创' || (l.innerText || '').trim() === '自主原创');
                if (!lbl) return null;
                let n = lbl;
                for (let i = 0; i < 6 && n.parentElement; i++) {
                    n = n.parentElement;
                    const sw = n.querySelector('button[role="switch"]');
                    if (sw) return sw.getAttribute('aria-checked');
                }
                return null;
            }
        """)
        if checked != "true":
            raise RuntimeError(
                f"点击「自主原创」switch 后 aria-checked={checked!r}，未成功开启。建议用 --headed 观察。"
            )
        jd_logger.success(_msg("✅", "自主原创已开启"))

    async def _set_schedule(self, frame: Frame):
        """切到「定时发布」并选择具体的日期 + 时间。

        DOM 结构（实测）：
        - radio：label.jd-radio-wrapper:has-text("定时发布")
        - 输入框：input[placeholder="请选择日期"]（readonly），父链 .jd-picker-input → .jd-picker
        - 面板：.jd-picker-datetime-panel（点 input 后显示）
        - 翻月：button.jd-picker-header-prev-btn / .jd-picker-header-next-btn
        - 日期 cell：td.jd-picker-cell[title="YYYY-MM-DD"]，禁用时含 jd-picker-cell-disabled
        - 时分滚轮：.jd-picker-time-panel（实测有两列 ul，分别是小时和分钟，li[title="N"]）
        - 确定按钮：.jd-picker-ok button（panel 右下角）

        平台限制：京东只允许选最近 30 天内的日期；超出范围对应 cell 会 disabled。
        """
        if self.schedule is None:
            return

        target_date = self.schedule.strftime("%Y-%m-%d")
        target_hour, target_minute = self.schedule.hour, self.schedule.minute
        expected_value = self.schedule.strftime("%Y-%m-%d %H:%M")

        # 滚动到「定时发布」radio 并点击
        await frame.evaluate(
            "() => { const el = [...document.querySelectorAll('label')].find(l => l.title === '定时发布'); if(el) el.scrollIntoView({block:'center'}); }"
        )
        await asyncio.sleep(0.3)
        await frame.locator('label.jd-radio-wrapper').filter(has_text="定时发布").first.click()
        await asyncio.sleep(1)

        # 打开日历面板
        date_input = frame.locator('input[placeholder="请选择日期"]').first
        await date_input.wait_for(state="visible", timeout=5000)
        await date_input.click()
        await asyncio.sleep(1.2)

        # 翻月找目标日期 cell（最多 14 次，足够 1 年内任意月份）
        async def panel_state():
            """读取当前日历面板状态：目标日期是否找到、是否禁用、可见范围。"""
            return await frame.evaluate(
                r"""(targetTitle) => {
                    const cells = [...document.querySelectorAll('td.jd-picker-cell[title]')];
                    const enabled = cells.filter(c => !c.className.includes('disabled'));
                    const inView = cells.filter(c => c.className.includes('in-view'));
                    const enabledTitles = enabled.map(c => c.title).sort();
                    const inViewTitles = inView.map(c => c.title).sort();
                    const target = cells.find(c => c.title === targetTitle);
                    return {
                        target_found: !!target,
                        target_disabled: target ? target.className.includes('disabled') : null,
                        first_enabled: enabledTitles[0] || '',
                        last_enabled: enabledTitles[enabledTitles.length - 1] || '',
                        in_view_first: inViewTitles[0] || '',
                        in_view_last: inViewTitles[inViewTitles.length - 1] || '',
                    };
                }""",
                target_date,
            )

        for _ in range(14):
            state = await panel_state()
            if state["target_found"]:
                break
            iv_first, iv_last = state["in_view_first"], state["in_view_last"]
            # 当前面板覆盖月份用 in_view 范围判断（in_view 是当月 1 号到月底）
            if iv_first and target_date < iv_first:
                await frame.locator('button.jd-picker-header-prev-btn').first.click()
            elif iv_last and target_date > iv_last:
                await frame.locator('button.jd-picker-header-next-btn').first.click()
            else:
                # in_view 没解析出来，跳出避免死循环
                break
            await asyncio.sleep(0.4)

        state = await panel_state()
        if not state["target_found"]:
            raise RuntimeError(
                f"未在日历面板找到目标日期 {target_date}。"
                f"当前面板可点击范围约 {state['first_enabled']} 到 {state['last_enabled']}。"
            )
        if state["target_disabled"]:
            raise ValueError(
                f"京东京麦当前不允许选择定时日期 {target_date}。"
                f"当前面板可点击范围约为 {state['first_enabled']} 到 {state['last_enabled']}。"
                "请改用日历中可点击的日期后重试。"
            )

        # 点击目标日期 cell
        await frame.locator(f'td.jd-picker-cell[title="{target_date}"]').first.click()
        await asyncio.sleep(0.5)
        jd_logger.info(_msg("📅", f"已选择日期: {target_date}"))

        # 设小时和分钟。两列 ul 分别是小时（24 个 li）和分钟（60 个 li）
        time_panel = frame.locator('.jd-picker-time-panel-column')
        hour_col, minute_col = time_panel.nth(0), time_panel.nth(1)

        hour_li = hour_col.locator(f'li.jd-picker-time-panel-cell:has-text("{target_hour:02d}")').first
        await hour_li.scroll_into_view_if_needed()
        await hour_li.click()
        jd_logger.info(_msg("🕐", f"已选择小时: {target_hour:02d}"))
        await asyncio.sleep(0.3)

        minute_li = minute_col.locator(f'li.jd-picker-time-panel-cell:has-text("{target_minute:02d}")').first
        await minute_li.scroll_into_view_if_needed()
        await minute_li.click()
        jd_logger.info(_msg("🕐", f"已选择分钟: {target_minute:02d}"))
        await asyncio.sleep(0.3)

        # 点击日期面板右下角的「确定」按钮
        confirm_btn = frame.locator('.jd-picker-datetime-panel').locator('button').filter(has_text="确定").first
        if not await confirm_btn.count():
            confirm_btn = frame.locator('.jd-picker-ok button').first
        await confirm_btn.click()
        await asyncio.sleep(0.8)

        # 校验 input.value 与期望值一致
        actual = (await date_input.input_value()).strip()
        if actual != expected_value:
            raise RuntimeError(
                f"定时发布时间设置后校验失败：期望 {expected_value}，页面实际 {actual!r}。已停止发布以避免错误时间。"
            )
        jd_logger.success(_msg("📅", f"定时发布时间已设置: {actual}"))

    async def _handle_captcha(self, frame) -> None:
        """检测验证码弹窗，若出现则暂停并等待用户手动完成。

        检测逻辑：
        - 弹窗出现：class 含 captcha_modal_popup，且 display != none
        - 弹窗消失（用户完成验证）：节点不存在或 display == none

        用户操作（两种模式）：
        - 交互终端（tty）：打印提示，等用户按回车，再确认弹窗消失
        - 后台进程（无 tty）：打印提示，轮询等弹窗自行消失（最长 10 分钟）
        """
        # 先等最多 8 秒检测验证码是否出现
        captcha_appeared = False
        for _ in range(8):
            await asyncio.sleep(1)
            appeared = await frame.evaluate("""
                () => {
                    const el = document.querySelector('.captcha_modal_popup, .captcha_modal_pc');
                    if (!el) return false;
                    const s = window.getComputedStyle(el);
                    return s.display !== 'none' && s.visibility !== 'hidden';
                }
            """)
            if appeared:
                captcha_appeared = True
                break

        if not captcha_appeared:
            # 没有验证码，正常流程
            return

        jd_logger.warning(_msg("🔐", "检测到安全验证码，上传已暂停"))

        is_tty = bool(sys.stdin and sys.stdin.isatty())

        if is_tty:
            # 交互终端模式：等用户按回车再确认
            while True:
                print("\n" + "=" * 50, flush=True)
                print("⚠️  触发验证码，请在浏览器中完成验证（旋转图片到正确角度）", flush=True)
                print("   完成后按回车继续...", flush=True)
                print("=" * 50, flush=True)

                # 在 executor 中阻塞读取 stdin，避免阻塞事件循环
                await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)

                still_there = await frame.evaluate("""
                    () => {
                        const el = document.querySelector('.captcha_modal_popup, .captcha_modal_pc');
                        if (!el) return false;
                        const s = window.getComputedStyle(el);
                        return s.display !== 'none' && s.visibility !== 'hidden';
                    }
                """)
                if not still_there:
                    jd_logger.success(_msg("✅", "验证码已完成"))
                    return
                else:
                    jd_logger.warning(_msg("⚠️", "验证码仍未消失，请重新完成验证后再按回车"))
        else:
            # 后台进程模式：打印提示，轮询等弹窗消失（最长 10 分钟）
            print("\n" + "=" * 50, flush=True)
            print("⚠️  触发验证码，请在浏览器中完成验证（旋转图片到正确角度）", flush=True)
            print("   程序将等待验证完成，最长等待 10 分钟...", flush=True)
            print("=" * 50, flush=True)
            jd_logger.warning(_msg("⏳", "后台模式：等待验证码完成（最长 10 分钟）"))

            for i in range(600):  # 最多 600 秒 = 10 分钟
                await asyncio.sleep(1)
                still_there = await frame.evaluate("""
                    () => {
                        const el = document.querySelector('.captcha_modal_popup, .captcha_modal_pc');
                        if (!el) return false;
                        const s = window.getComputedStyle(el);
                        return s.display !== 'none' && s.visibility !== 'hidden';
                    }
                """)
                if not still_there:
                    jd_logger.success(_msg("✅", "验证码已完成"))
                    return
                if i > 0 and i % 30 == 0:
                    jd_logger.warning(_msg("⏳", f"仍在等待验证码完成... ({i}s)"))

            raise RuntimeError("等待验证码超时（10 分钟），请检查浏览器并手动处理后重试")

    async def _upload_in_context(self, context: BrowserContext) -> dict:
        """在指定 BrowserContext 中执行完整的发布流程。

        流程：
        1. 校验参数
        2. 打开发布页 → 定位 iframe
        3. 上传视频 → 等待上传完成 → 填写标题 → 关联商品 →
           选择创作者声明 → 开启自主原创 → 设置定时
        4. dry_run 跳过发布；否则点击发布按钮 → 处理验证码 → 等待确认
        5. 成功后保存 storage_state；页面保留供人工复核

        :returns: 发布结果 dict（含 mode/confirmation/final_url）
        :raises JdAuthenticationError: Cookie 失效
        :raises PublishResultUncertainError: 发布结果不确定
        """
        jd_logger.info(_msg("🧍", "小人先检查 cookie 和视频文件"))
        await self.validate_upload_args()
        jd_logger.info(_msg("🥳", "上传前检查通过"))

        page = None
        success = False
        submitted = False

        try:
            page = await context.new_page()
            await page.goto(JD_PUBLISH_VIDEO_URL, wait_until="domcontentloaded")
            if _url_host(page.url) in JD_AUTH_HOSTS:
                raise JdAuthenticationError("京东 Cookie 已失效，请重新登录")
            jd_logger.info(_msg("🧭", "小人正在赶往京东京麦发视频页面"))
            try:
                frame = await _find_publish_iframe(page)
            except RuntimeError as exc:
                if _url_host(page.url) in JD_AUTH_HOSTS:
                    raise JdAuthenticationError("京东 Cookie 已失效，请重新登录") from exc
                raise
            await asyncio.sleep(3)

            # 上传视频文件
            jd_logger.info(_msg("🏃", f"小人开始上传视频: {Path(self.file_path).name}"))
            file_input = frame.locator('input[type="file"][accept*=".mp4"]').first
            await file_input.set_input_files(self.file_path)
            await self._wait_for_video_uploaded(frame)

            # 填写标题
            jd_logger.info(_msg("✍️", f"填写正文标题: {self.title}"))
            await frame.locator("#title").fill(self.title)
            await asyncio.sleep(0.5)

            # 各步骤依次执行
            await self._add_goods(page, frame)
            await self._select_creator_declaration(frame)
            await self._set_original(frame)
            await self._set_schedule(frame)

            if self.dry_run:
                jd_logger.info(_msg("🧪", "Dry run 模式：跳过发布，所有基础设置已完成"))
                success = True
                return {"mode": "dry_run"}

            # 真实发布
            publish_btn = frame.locator('button[class*="publishBtn"]').filter(has_text="发布").first
            jd_logger.info(_msg("🚀", "点击发布按钮"))
            before_submit_text = await frame.locator("body").inner_text(timeout=3000)
            initial_url = page.url
            await publish_btn.click()
            submitted = True

            # 检测并处理验证码（人工介入）
            await self._handle_captcha(frame)

            # 确认明确的成功消息或真实的页面跳转
            published = False
            confirmation = ""
            success_hints = ("发布成功", "提交成功", "已提交审核", "审核中", "发布完成")
            failure_hints = ("发布失败", "提交失败", "发布出错", "请修改后重试")
            for _ in range(30):
                await asyncio.sleep(1)
                try:
                    current_text = await frame.locator("body").inner_text(timeout=3000)
                    # 先检测失败
                    for hint in failure_hints:
                        if hint in current_text and hint not in before_submit_text:
                            raise RuntimeError(f"平台返回发布失败提示：{hint}")
                    # 检测成功提示
                    matched_success = next(
                        (
                            hint
                            for hint in success_hints
                            if hint in current_text and hint not in before_submit_text
                        ),
                        None,
                    )
                    if matched_success:
                        published = True
                        confirmation = f"检测到平台成功提示：{matched_success}"
                        break
                    # 检测页面跳转
                    if page.url != initial_url and _url_host(page.url) not in JD_AUTH_HOSTS:
                        published = True
                        confirmation = f"页面已跳转：{page.url}"
                        break
                except Exception as e:
                    # frame 可能因页面跳转而 detach
                    if "detached" in str(e).lower():
                        if page.url != initial_url and _url_host(page.url) not in JD_AUTH_HOSTS:
                            published = True
                            confirmation = f"发布表单已关闭并跳转：{page.url}"
                            break
                        raise PublishResultUncertainError(
                            "京东发布表单已关闭，但页面没有给出可确认的发布结果"
                        ) from e
                    raise

            if not published:
                raise PublishResultUncertainError(
                    "已点击京东发布按钮，但 30 秒内没有检测到明确成功或失败信号"
                )

            jd_logger.success(_msg("🥳", f"视频发布已确认（{confirmation}）"))
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
                    "京东发布按钮已经点击，但任务在取得平台确认前被中断"
                ) from exc
            raise
        except Exception as exc:
            jd_logger.error(_msg("❌", f"UPLOAD_FAILED: {exc}"))
            raise
        finally:
            # 成功后保存 storage_state（更新 Cookie）
            if success:
                await context.storage_state(path=self.account_file)
                jd_logger.success(_msg("🥳", "cookie 更新完毕"))
            # 页面保留供人工复核
            if page:
                try:
                    if not page.is_closed():
                        jd_logger.info(
                            _msg(
                                "📌",
                                f"发布页面已保留供人工复核；当前账号共保留 {len(context.pages)} 个页面",
                            )
                        )
                except Exception:
                    pass

    async def upload_in_session(self, session: JdBrowserSession) -> dict:
        """通过浏览器会话执行发布流程。

        会话校验 Cookie 后调用 _upload_in_context。
        若抛出 JdAuthenticationError 则标记会话未认证。
        """
        context = await session.ensure_open()
        try:
            result = await self._upload_in_context(context)
        except JdAuthenticationError:
            session.mark_authenticated(False)
            raise
        session.mark_authenticated(True)
        return result
