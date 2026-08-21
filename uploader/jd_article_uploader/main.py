# -*- coding: utf-8 -*-
"""京东京麦图文发布器（独立于京东视频流程）。

本模块只负责图片上传和图文表单操作；账号登录态由 Web 平台适配层统一调用
京东视频上传器的 ``jd_setup`` 校验，避免视频与图文出现两套不一致的 Cookie 规则。
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from patchright.async_api import BrowserContext, Frame

from uploader.errors import PublishResultUncertainError
from uploader.jd_session import JdBrowserSession
from utils.log import jd_logger
from utils.clipboard import write_clipboard

JD_GRAPHIC_URL = "https://dr.jd.com/jm/#/n/publish-graphic.html?platform=jm-pop"
JD_AUTH_HOSTS = {"passport.shop.jd.com", "passport.jd.com", "safe.jd.com"}
JD_MAX_IMAGES = 20
JD_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
JD_MAX_IMAGE_BYTES = 5 * 1024 * 1024
JD_MAX_GOODS_IDS = 10
JD_DECLARATIONS = (
    "内容无需标注", "内容含营销广告", "含AI生成内容", "含虚构演绎内容", "内容为转载", "个人观点，仅供参考",
)


class JdArticleAuthenticationError(RuntimeError):
    """图文发布页跳转到京东鉴权页时使用的明确错误类型。"""


def _host(url: str) -> str:
    try:
        return urlparse(url).hostname or ""
    except Exception:
        return ""


async def _find_frame(page, timeout_seconds: int = 30) -> Frame:
    for _ in range(timeout_seconds):
        for frame in page.frames:
            if frame != page.main_frame and "publish-graphic.html" in frame.url:
                return frame
        await asyncio.sleep(1)
    raise RuntimeError("未找到京东图文发布 iframe")


def _normal_ids(raw: str) -> tuple[str, ...]:
    values = (part.strip("'\"‘’“”") for part in re.split(r"[,，\s]+", raw.strip()))
    return tuple(dict.fromkeys(value for value in values if value))


class JDArticle:
    """京东图文发布流程，独立维护图片、正文和图文设置。"""

    def __init__(self, image_paths, title: str, description: str, account_file: str,
                 goods_id: str = "", topic: str = "", schedule: datetime | None = None,
                 original: bool = False, creator_declaration: str = "", debug: bool = True,
                 dry_run: bool = False) -> None:
        self.image_paths = tuple(str(path) for path in image_paths)
        self.title, self.description, self.account_file = title.strip(), description.strip(), account_file
        self.goods_id, self.topic, self.schedule = goods_id.strip(), topic.strip(), schedule
        self.original, self.creator_declaration = original, creator_declaration.strip()
        self.debug, self.dry_run = debug, dry_run

    async def validate_upload_args(self) -> None:
        if not os.path.exists(self.account_file):
            raise RuntimeError(f"cookie文件不存在，请先完成京东京麦登录: {self.account_file}")
        if not 1 <= len(self.image_paths) <= JD_MAX_IMAGES:
            raise ValueError("京东图文必须上传 1-20 张图片")
        normalized = []
        for raw_path in self.image_paths:
            path = Path(raw_path).resolve()
            if not path.is_file():
                raise ValueError(f"京东图文图片不存在: {path}")
            if path.suffix.lower() not in JD_IMAGE_EXTENSIONS:
                raise ValueError("京东图文图片仅支持 JPG 或 PNG 格式")
            if path.stat().st_size == 0 or path.stat().st_size > JD_MAX_IMAGE_BYTES:
                raise ValueError("京东图文单张图片大小必须大于 0 且不超过 5 MiB")
            normalized.append(str(path))
        self.image_paths = tuple(normalized)
        if not 5 <= len(self.title) <= 20:
            raise ValueError(f"京东图文标题长度必须为 5-20 个字符（当前 {len(self.title)}）")
        if len(self.description) > 1001:
            raise ValueError("京东图文正文最多 1001 个字符")
        ids = _normal_ids(self.goods_id)
        if any(not item.isdigit() for item in ids):
            raise ValueError("京东商品 ID 必须为纯数字")
        if len(ids) > JD_MAX_GOODS_IDS:
            raise ValueError(f"京东一次最多关联 {JD_MAX_GOODS_IDS} 个商品 ID")
        self.goods_id = ",".join(ids)
        if self.creator_declaration not in JD_DECLARATIONS:
            raise ValueError("请选择有效的京东创作声明")

    async def _upload_images(self, page, frame: Frame) -> None:
        # 直接对隐藏的 file input 设置文件，复用京东视频上传的 set_input_files 写法。
        # 不再走 page.expect_file_chooser + chooser.set_files：后者会弹出 Windows 原生
        # 文件选择框，对话框关闭后浏览器会丢失前台焦点，进而导致后续 Ctrl+V 粘贴时
        # 被 Windows 前台锁定机制发到后台。
        file_input = frame.locator('input[type="file"][accept*="image"][multiple]').first
        if not await file_input.count():
            file_input = frame.locator('input[type="file"][accept*="image"]').first
        if not await file_input.count():
            file_input = frame.locator('input[type="file"]').first
        if not await file_input.count():
            raise RuntimeError("未找到京东图文图片上传的 file input 控件")
        await file_input.wait_for(state="attached", timeout=10000)
        await file_input.set_input_files(list(self.image_paths))
        for _ in range(300):
            text = await frame.locator("body").inner_text(timeout=3000)
            if "上传失败" in text or "处理失败" in text:
                raise RuntimeError(f"京东图文图片上传失败: {text[-300:]}")
            # 实页初始状态已有一张平台占位图，不能以 img 数量判断。图片完成后
            # “编辑封面”由禁用变可用，同时计数从 0/20 更新为实际上传数量。
            edit_cover = frame.get_by_role("button", name="编辑封面", exact=True)
            expected_count = re.search(
                rf"(?<!\d){len(self.image_paths)}\s*/\s*20(?!\d)", text
            ) is not None
            if expected_count and await edit_cover.is_enabled():
                jd_logger.success(f"🖼️ 京东图文已上传 {len(self.image_paths)} 张图片")
                return
            await asyncio.sleep(1)
        raise RuntimeError("等待京东图文图片上传完成超时")

    async def _add_goods(self, page, frame: Frame) -> None:
        """通过视频发布同款“链接导入”流程一次关联并确认全部商品。"""
        if not self.goods_id:
            return
        ids = tuple(self.goods_id.split(","))
        jd_logger.info(f"🛒 准备通过链接导入添加图文商品: {', '.join(ids)}")

        plus_button = frame.locator('div[class*="addgoods-upload"]').first
        await plus_button.scroll_into_view_if_needed()
        await plus_button.click()
        drawer = frame.locator('.jd-drawer-open, .jd-drawer-wrapper-body').first
        await drawer.wait_for(state="visible", timeout=10000)
        await asyncio.sleep(1)

        link_tab = drawer.locator('[role="tab"]').filter(has_text="链接导入").first
        if not await link_tab.count():
            link_tab = drawer.locator('.jd-tabs-tab-btn').filter(has_text="链接导入").first
        await link_tab.wait_for(state="visible", timeout=10000)
        await link_tab.click()
        await asyncio.sleep(1.5)

        panel = drawer.locator('[role="tabpanel"][aria-hidden="false"]').first
        if not await panel.count():
            panel = drawer.locator('.jd-tabs-tabpane-active').first
        await panel.wait_for(state="visible", timeout=5000)
        target = panel.locator('.paste-search-input-content').first
        await target.wait_for(state="visible", timeout=5000)
        links = "\n".join(f"https://item.jd.com/{item}.html" for item in ids)
        paste_shortcut = write_clipboard(links)
        await target.click()
        await page.keyboard.press(paste_shortcut)
        tags = panel.locator('.paste-search-input-content-tag')
        for _ in range(10):
            if await tags.count() == len(ids):
                break
            await asyncio.sleep(.2)
        else:
            raise RuntimeError(f"京东链接导入未生成全部商品标签：期望 {len(ids)} 个，实际 {await tags.count()} 个。")
        query_button = panel.locator('.paste-search-input button').filter(has_text="查询").first
        await query_button.wait_for(state="visible", timeout=5000)
        await query_button.click()
        jd_logger.info(f"🔎 已一次查询 {len(ids)} 个图文商品 ID")

        cards = panel.locator('.goods-card')
        invalid_hints = ("暂无数据", "没有找到", "无结果", "未搜索到", "失效原因")
        for _ in range(30):
            await asyncio.sleep(1)
            result_text = await panel.inner_text()
            if any(hint in result_text for hint in invalid_hints) and await cards.count() < len(ids):
                raise ValueError(f"京东链接导入商品不可用：{result_text.strip()[-300:]}")
            if await cards.count() >= len(ids):
                break
        else:
            raise RuntimeError(f"京东链接导入查询超时：期望 {len(ids)} 个商品，实际 {await cards.count()} 个。")
        checks = panel.locator('label.jd-checkbox-wrapper.goods-card-check')
        if await checks.count() != len(ids):
            raise RuntimeError(f"京东链接导入结果数量不匹配：期望 {len(ids)} 个，实际 {await checks.count()} 个。")

        async def is_selected(checkbox) -> bool:
            return bool(await checkbox.evaluate("""
                element => {
                    const input = element.querySelector('input[type="checkbox"]');
                    if (input) return input.checked;
                    return element.classList.contains('jd-checkbox-checked')
                        || element.getAttribute('aria-checked') === 'true'
                        || element.querySelector('.jd-checkbox-checked, [aria-checked="true"]') !== null;
                }
            """))

        for index, goods_id in enumerate(ids, start=1):
            for _ in range(3):
                check = checks.nth(index - 1)
                await check.wait_for(state="visible", timeout=5000)
                if await is_selected(check):
                    break
                await check.click()
                await asyncio.sleep(.5)
                if await is_selected(checks.nth(index - 1)):
                    break
            else:
                raise RuntimeError(f"京东链接导入商品 {goods_id} 连续 3 次点击后仍未选中，已停止避免漏挂商品。")
            jd_logger.info(f"✅ 已确认勾选第 {index}/{len(ids)} 个图文商品: {goods_id}")

        selected_count = sum([await is_selected(checks.nth(index)) for index in range(len(ids))])
        if selected_count != len(ids):
            raise RuntimeError(f"京东链接导入勾选校验失败：期望已选 {len(ids)} 个，实际 {selected_count} 个。")
        await drawer.locator('button.jd-btn-primary').filter(has_text="确定").first.click()
        try:
            await drawer.wait_for(state="hidden", timeout=10000)
        except Exception:
            await asyncio.sleep(2)
        jd_logger.success(f"🛒 图文商品已关联（共 {len(ids)} 个）")

    async def _add_graphic_topic(self, frame: Frame) -> None:
        """按京东视频发布器的方式搜索并选择图文参与话题。

        京东候选卡片的标题节点还包含“详情”等嵌套元素，不能直接使用
        ``inner_text`` 做精确匹配；只读取标题节点的直接文本，并兼容平台
        自动展示的 ``#`` 前缀。
        """
        if not self.topic:
            return

        expected_topic = re.sub(r"[\s#]+", "", self.topic)
        jd_logger.info(f"📣 准备添加京东图文话题: {self.topic}")
        trigger = frame.get_by_text("点击添加话题", exact=True).first
        await trigger.wait_for(state="visible", timeout=10000)
        await trigger.click()

        drawer_title = frame.get_by_text("参与话题", exact=True).last
        await drawer_title.wait_for(state="visible", timeout=15000)
        drawer = drawer_title.locator('xpath=ancestor::*[@role="dialog"][1]')
        await drawer.wait_for(state="visible", timeout=5000)
        search = drawer.locator('input[placeholder="输入关键词搜索"]').first
        await search.wait_for(state="visible", timeout=5000)
        await search.fill(self.topic)
        await drawer.locator("button.jd-input-search-button").first.click()

        cards = drawer.locator(".select-item")
        available_topics = []
        matched_card = None
        for _ in range(30):
            available_topics = []
            for index in range(await cards.count()):
                card = cards.nth(index)
                title = card.locator(".title").first
                name = await title.evaluate("""
                    element => [...element.childNodes]
                        .filter(node => node.nodeType === Node.TEXT_NODE)
                        .map(node => node.textContent || "").join("").trim()
                """)
                if name:
                    available_topics.append(name)
                if re.sub(r"[\s#]+", "", name) == expected_topic:
                    matched_card = card
                    break
            if matched_card is not None:
                break
            await asyncio.sleep(1)

        if matched_card is None:
            try:
                await drawer.locator("button.jd-drawer-close").click()
            except Exception:
                pass
            raise ValueError(
                f"京东图文话题“{self.topic}”没有完全匹配的搜索结果。"
                f"当前结果：{', '.join(available_topics[:8]) or '无'}"
            )

        await matched_card.click()
        for _ in range(20):
            if not await drawer.is_visible():
                break
            await asyncio.sleep(0.5)
        else:
            raise RuntimeError(f"京东图文话题“{self.topic}”点击后未被平台接受")
        jd_logger.success(f"📣 京东图文话题已添加: {self.topic}")

    async def _select_declaration(self, frame: Frame) -> None:
        declaration = frame.locator('.content-declaration-wrapper .jd-select-selector').first
        await declaration.scroll_into_view_if_needed()
        await declaration.click()
        await asyncio.sleep(1)
        option = frame.locator(f'div.jd-select-item-option[label="{self.creator_declaration}"]').first
        if not await option.count():
            raise RuntimeError(f"未找到创作声明“{self.creator_declaration}”，页面选项可能已变化")
        text = (await option.evaluate("el => el.getAttribute('label') || el.innerText") or "").strip()
        if text != self.creator_declaration:
            raise RuntimeError(f"创作声明校验失败：期望“{self.creator_declaration}”，实际“{text}”")
        await option.click()
        await asyncio.sleep(.5)
        jd_logger.success(f"📋 图文创作声明已选择: {text}")

    async def _set_original(self, frame: Frame) -> None:
        """按京东视频相同的规则设置图文页“自主原创”开关并回读校验。"""
        if not self.original:
            return

        switch_state = await frame.evaluate(r"""
            () => {
                const label = [...document.querySelectorAll('label')]
                    .find(item => item.title === '自主原创' || (item.innerText || '').trim() === '自主原创');
                if (!label) return { found: false };
                let node = label;
                for (let index = 0; index < 6 && node.parentElement; index += 1) {
                    node = node.parentElement;
                    const toggle = node.querySelector('button[role="switch"]');
                    if (toggle) return { found: true, disabled: toggle.disabled,
                        aria_checked: toggle.getAttribute('aria-checked') };
                }
                return { found: true, error: 'switch_not_found' };
            }
        """)
        if not switch_state.get("found"):
            raise RuntimeError("图文页面未找到「自主原创」选项，可能页面结构已变化。")
        if switch_state.get("error") == "switch_not_found":
            raise RuntimeError("找到图文「自主原创」标签但未找到对应 switch 按钮。")
        if switch_state.get("disabled"):
            raise ValueError(
                "该账号的图文「自主原创」switch 当前不可用（可能是账号资质未达标或该类目不支持）。"
                "请在京东商家后台确认账号是否已开通原创功能，或取消自主原创后重试。"
            )
        if switch_state.get("aria_checked") == "true":
            jd_logger.info("✅ 图文自主原创已经开启，跳过")
            return

        switch = frame.locator('label[title="自主原创"]').locator(
            "xpath=ancestor::*[position()<=5]//button[@role='switch']"
        ).first
        if not await switch.count():
            switch = frame.locator('button[role="switch"]:not([disabled])').first
        await switch.click()
        await asyncio.sleep(.5)
        checked = await frame.evaluate(r"""
            () => {
                const label = [...document.querySelectorAll('label')]
                    .find(item => item.title === '自主原创' || (item.innerText || '').trim() === '自主原创');
                if (!label) return null;
                let node = label;
                for (let index = 0; index < 6 && node.parentElement; index += 1) {
                    node = node.parentElement;
                    const toggle = node.querySelector('button[role="switch"]');
                    if (toggle) return toggle.getAttribute('aria-checked');
                }
                return null;
            }
        """)
        if checked != "true":
            raise RuntimeError(f"点击图文「自主原创」switch 后 aria-checked={checked!r}，未成功开启。建议用 --headed 观察。")
        jd_logger.success("✅ 京东图文自主原创已开启")

    async def _set_schedule(self, frame: Frame) -> None:
        """复用视频发布的日期、时分选择与最终值回读校验。"""
        if self.schedule is None:
            return
        target_date = self.schedule.strftime("%Y-%m-%d")
        target_hour, target_minute = self.schedule.hour, self.schedule.minute
        expected_value = self.schedule.strftime("%Y-%m-%d %H:%M")
        await frame.evaluate("() => { const el = [...document.querySelectorAll('label')].find(l => l.title === '定时发布'); if (el) el.scrollIntoView({block: 'center'}); }")
        await asyncio.sleep(.3)
        await frame.locator('label.jd-radio-wrapper').filter(has_text="定时发布").first.click()
        await asyncio.sleep(1)
        date_input = frame.locator('input[placeholder="请选择日期"]').first
        await date_input.wait_for(state="visible", timeout=5000)
        await date_input.click()

        async def panel_state():
            return await frame.evaluate(r"""(targetTitle) => {
                const cells = [...document.querySelectorAll('td.jd-picker-cell[title]')];
                const enabled = cells.filter(c => !c.className.includes('disabled')).map(c => c.title).sort();
                const inView = cells.filter(c => c.className.includes('in-view')).map(c => c.title).sort();
                const target = cells.find(c => c.title === targetTitle);
                return { target_found: !!target, target_disabled: target ? target.className.includes('disabled') : null,
                    first_enabled: enabled[0] || '', last_enabled: enabled[enabled.length - 1] || '',
                    in_view_first: inView[0] || '', in_view_last: inView[inView.length - 1] || '' };
            }""", target_date)

        for _ in range(14):
            state = await panel_state()
            if state["target_found"]:
                break
            if state["in_view_first"] and target_date < state["in_view_first"]:
                await frame.locator("button.jd-picker-header-prev-btn").first.click()
            elif state["in_view_last"] and target_date > state["in_view_last"]:
                await frame.locator("button.jd-picker-header-next-btn").first.click()
            else:
                break
            await asyncio.sleep(.4)
        state = await panel_state()
        if not state["target_found"]:
            raise RuntimeError(f"未在图文日历面板找到目标日期 {target_date}。当前可点击范围约 {state['first_enabled']} 到 {state['last_enabled']}。")
        if state["target_disabled"]:
            raise ValueError(f"京东京麦当前不允许选择定时日期 {target_date}。当前可点击范围约为 {state['first_enabled']} 到 {state['last_enabled']}。")
        await frame.locator(f'td.jd-picker-cell[title="{target_date}"]').first.click()
        await asyncio.sleep(.5)
        columns = frame.locator('.jd-picker-time-panel-column')
        hour = columns.nth(0).locator(f'li.jd-picker-time-panel-cell:has-text("{target_hour:02d}")').first
        minute = columns.nth(1).locator(f'li.jd-picker-time-panel-cell:has-text("{target_minute:02d}")').first
        await hour.scroll_into_view_if_needed()
        await hour.click()
        await minute.scroll_into_view_if_needed()
        await minute.click()
        confirm = frame.locator('.jd-picker-datetime-panel').locator('button').filter(has_text="确定").first
        if not await confirm.count():
            confirm = frame.locator('.jd-picker-ok button').first
        await confirm.click()
        await asyncio.sleep(.8)
        actual = (await date_input.input_value()).strip()
        if actual != expected_value:
            raise RuntimeError(f"图文定时发布时间设置后校验失败：期望 {expected_value}，页面实际 {actual!r}。")
        jd_logger.success(f"📅 图文定时发布时间已设置: {actual}")

    async def _handle_captcha(self, frame: Frame) -> None:
        """与视频发布一致：检测到京东验证码后等待人工在可见浏览器完成。"""
        for _ in range(8):
            await asyncio.sleep(1)
            appeared = await frame.evaluate("""
                () => {
                    const el = document.querySelector('.captcha_modal_popup, .captcha_modal_pc');
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    return style.display !== 'none' && style.visibility !== 'hidden';
                }
            """)
            if appeared:
                break
        else:
            return

        jd_logger.warning("🔐 检测到京东图文安全验证码，发布已暂停")
        if sys.stdin.isatty():
            while True:
                print("\n请在京东发布页完成验证码，完成后按回车继续...", flush=True)
                await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)
                still_there = await frame.evaluate("""
                    () => { const el = document.querySelector('.captcha_modal_popup, .captcha_modal_pc');
                        if (!el) return false; const style = window.getComputedStyle(el);
                        return style.display !== 'none' && style.visibility !== 'hidden'; }
                """)
                if not still_there:
                    jd_logger.success("✅ 京东图文验证码已完成")
                    return
                jd_logger.warning("⚠️ 京东图文验证码仍未消失，请重新完成后按回车")

        for elapsed in range(600):
            await asyncio.sleep(1)
            still_there = await frame.evaluate("""
                () => { const el = document.querySelector('.captcha_modal_popup, .captcha_modal_pc');
                    if (!el) return false; const style = window.getComputedStyle(el);
                    return style.display !== 'none' && style.visibility !== 'hidden'; }
            """)
            if not still_there:
                jd_logger.success("✅ 京东图文验证码已完成")
                return
            if elapsed and elapsed % 30 == 0:
                jd_logger.warning(f"⏳ 仍在等待京东图文验证码完成... ({elapsed}s)")
        raise RuntimeError("等待京东图文验证码超时（10 分钟），请检查浏览器后重试")

    async def _upload_in_context(self, context: BrowserContext) -> dict:
        await self.validate_upload_args()
        page = None
        success = False
        submitted = False
        try:
            page = await context.new_page()
            await page.goto(JD_GRAPHIC_URL, wait_until="domcontentloaded")
            if _host(page.url) in JD_AUTH_HOSTS:
                raise JdArticleAuthenticationError("京东 Cookie 已失效，请重新登录")
            try:
                frame = await _find_frame(page)
            except RuntimeError as exc:
                if _host(page.url) in JD_AUTH_HOSTS:
                    raise JdArticleAuthenticationError("京东 Cookie 已失效，请重新登录") from exc
                raise
            await asyncio.sleep(3)
            await self._upload_images(page, frame)
            jd_logger.info(f"✍️ 填写京东图文标题: {self.title}")
            await frame.locator("#title").fill(self.title)
            await frame.locator("#description").fill(self.description)
            await asyncio.sleep(.5)
            await self._add_goods(page, frame)
            await self._add_graphic_topic(frame)
            await self._select_declaration(frame)
            await self._set_original(frame)
            await self._set_schedule(frame)
            if self.dry_run:
                jd_logger.info("🧪 图文流程验证完成，跳过正式发布")
                success = True
                return {"mode": "dry_run"}

            publish_button = frame.locator('button[class*="publishBtn"]').filter(has_text="发布").first
            if not await publish_button.count():
                publish_button = frame.get_by_role("button", name="发布", exact=True)
            before = await frame.locator("body").inner_text(timeout=3000)
            initial_url = page.url
            jd_logger.info("🚀 点击京东图文发布按钮")
            await publish_button.click()
            submitted = True
            await self._handle_captcha(frame)

            success_hints = ("发布成功", "提交成功", "已提交审核", "审核中", "发布完成")
            failure_hints = ("发布失败", "提交失败", "发布出错", "请修改后重试")
            for _ in range(30):
                await asyncio.sleep(1)
                try:
                    text = await frame.locator("body").inner_text(timeout=3000)
                    failure = next((hint for hint in failure_hints if hint in text and hint not in before), None)
                    if failure:
                        raise RuntimeError(f"平台返回图文发布失败提示：{failure}")
                    confirmation = next((hint for hint in success_hints if hint in text and hint not in before), None)
                    if confirmation:
                        success = True
                        return {"mode": "publish", "confirmation": f"检测到平台成功提示：{confirmation}", "final_url": page.url}
                    if page.url != initial_url and _host(page.url) not in JD_AUTH_HOSTS:
                        success = True
                        return {"mode": "publish", "confirmation": f"页面已跳转：{page.url}", "final_url": page.url}
                except Exception as exc:
                    if "detached" in str(exc).lower() and page.url != initial_url and _host(page.url) not in JD_AUTH_HOSTS:
                        success = True
                        return {"mode": "publish", "confirmation": f"发布表单已关闭并跳转：{page.url}", "final_url": page.url}
                    raise
            raise PublishResultUncertainError("已点击京东图文发布按钮，但 30 秒内没有检测到明确成功或失败信号")
        except asyncio.CancelledError as exc:
            if submitted:
                raise PublishResultUncertainError("京东图文发布按钮已经点击，但任务在取得平台确认前被中断") from exc
            raise
        except Exception as exc:
            jd_logger.error(f"❌ JD_ARTICLE_UPLOAD_FAILED: {exc}")
            raise
        finally:
            if success:
                await context.storage_state(path=self.account_file)
                jd_logger.success("🥳 京东图文 cookie 已更新")
            if page:
                try:
                    if not page.is_closed():
                        jd_logger.info(f"📌 京东图文发布页面已保留供人工复核；当前账号共保留 {len(context.pages)} 个页面")
                except Exception:
                    pass

    async def upload_in_session(self, session: JdBrowserSession) -> dict:
        try:
            result = await self._upload_in_context(await session.ensure_open())
        except JdArticleAuthenticationError:
            session.mark_authenticated(False)
            raise
        session.mark_authenticated(True)
        return result
