from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import threading
import time
import traceback

from local_agent import __version__
from local_agent.autostart import open_url, set_autostart
from local_agent.client import AgentApiClient, AgentApiError
from local_agent.credentials import AgentConnectionStore, StoredConnection
from local_agent.main import LocalAgentApplication, _server_url
from local_agent.paths import default_data_root
from local_agent import theme
from local_agent import updater
from utils.log import logger


_WINDOWS_MUTEX = None

UPDATE_CHECK_INTERVAL_SECONDS = 6 * 60 * 60
UPDATE_CHECK_DELAY_SECONDS = 90


def _show_fatal_error(message: str) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox
    except Exception:
        print(message, file=sys.stderr)
        return

    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("MPAU 本地执行助手启动失败", message)
        root.destroy()
    except Exception:
        print(message, file=sys.stderr)


def _log_and_show_unhandled_exception(exc_type, exc, tb) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        return sys.__excepthook__(exc_type, exc, tb)
    logger.error(
        "桌面助手发生未处理异常\n{}",
        "".join(traceback.format_exception(exc_type, exc, tb)),
    )
    _show_fatal_error(
        f"MPAU 本地执行助手启动或运行时发生异常：{exc}\n"
        f"详细日志请查看本机日志文件。"
    )




def _acquire_single_instance() -> bool:
    global _WINDOWS_MUTEX
    if os.name != "nt":
        return True
    import ctypes

    _WINDOWS_MUTEX = ctypes.windll.kernel32.CreateMutexW(
        None, False, "Local\\MPAU-Agent-Desktop"
    )
    return bool(_WINDOWS_MUTEX and ctypes.windll.kernel32.GetLastError() != 183)


def _pairing_dialog(
    store: AgentConnectionStore,
    data_root: Path,
    *,
    initial_server: str = "",
) -> StoredConnection | None:
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.title("MPAU 本地执行助手")
    root.resizable(False, False)
    root.configure(bg=theme.CREAM)
    theme.apply_tk_scaling(root)
    theme.center_window(root, 520, 540)
    result: dict[str, StoredConnection] = {}

    theme.header_band(
        root,
        "MPAU 本地执行助手",
        "连接商家发布台，替你在本机自动完成商品发布",
    ).pack(fill="x")

    body = tk.Frame(root, bg=theme.CREAM)
    body.pack(fill="both", expand=True, padx=28)

    form = theme.card(body)
    form.pack(fill="x", pady=(22, 0))
    form_inner = tk.Frame(form, bg=theme.CARD)
    form_inner.pack(fill="x", padx=22, pady=(20, 16))

    theme.field_label(form_inner, "发布台地址", anchor="w", fill="x")
    server_entry = theme.styled_entry(
        form_inner,
        entry_font=theme.mono_font(10),
        fill="x",
        ipady=8,
        pady=(6, 16),
    )
    server_entry.insert(0, initial_server or "https://")
    theme.field_label(form_inner, "一次性配对码", anchor="w", fill="x")
    code_entry = theme.styled_entry(
        form_inner,
        entry_font=(theme.FONT_FAMILY, 15, "bold"),
        justify="center",
        fill="x",
        ipady=9,
        pady=(6, 0),
    )
    code_entry.focus_set()

    status = tk.StringVar(value="")
    tk.Label(
        form_inner,
        textvariable=status,
        bg=theme.CARD,
        fg=theme.RED_600,
        font=theme.font(9),
        wraplength=400,
        justify="left",
    ).pack(anchor="w", pady=(12, 0))

    def finish_pairing() -> None:
        raw_server = server_entry.get().strip()
        code = code_entry.get().strip()
        try:
            server = _server_url(raw_server, allow_http=False)
        except ValueError as exc:
            status.set(str(exc))
            return
        if not code:
            status.set("请输入网页生成的配对码")
            return
        button.configure(state="disabled", text="正在配对...")
        status.set("")

        def worker() -> None:
            client = AgentApiClient(server)
            application = LocalAgentApplication(client, data_root=data_root, poll_seconds=2)
            try:
                paired = client.pair(application.hello, code)
                store.save(
                    server_url=server,
                    agent_token=paired["agent_token"],
                    user=paired["user"],
                    expires_at=paired["expires_at"],
                )
                result["connection"] = store.load()
            except (AgentApiError, OSError, ValueError) as exc:
                message = str(exc)
                root.after(0, lambda value=message: pairing_failed(value))
                return
            root.after(0, pairing_succeeded)

        threading.Thread(target=worker, name="mpau-pairing", daemon=True).start()

    def pairing_failed(message: str) -> None:
        status.set(message)
        button.configure(state="normal", text="完成配对")

    def pairing_succeeded() -> None:
        set_autostart(True)
        messagebox.showinfo("配对成功", "本地执行助手已连接，以后只需打开发布台网页。")
        root.destroy()

    button = theme.primary_button(body, "完成配对", finish_pairing)
    button.pack(fill="x", ipady=5, pady=(18, 0))
    root.bind("<Return>", lambda _e: finish_pairing())
    tk.Label(
        body,
        text="配对一次后会随 Windows 登录自动连接，无需再次输入",
        bg=theme.CREAM,
        fg=theme.TEXT_400,
        font=theme.font(9),
    ).pack(pady=(14, 20))
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()
    return result.get("connection")


class AgentUpdater:
    """Shared self-update state for the tray menu and the status window."""

    def __init__(
        self, application: LocalAgentApplication, data_root: Path
    ) -> None:
        self.application = application
        self.client = application.client
        self.data_root = data_root
        self.release: dict | None = None
        self.busy = False
        self.pending_installer: Path | None = None

    def has_running_jobs(self) -> bool:
        runner = self.application.runner
        return runner is not None and bool(runner._running_tasks)

    def check(self) -> tuple[bool, str]:
        """Query the server once; returns (found, human message)."""
        release = updater.fetch_latest_release(self.client, __version__)
        self.release = release
        if release is None:
            return False, "当前已是最新版本"
        return True, f"发现新版本 v{release['version']}"

    def download(self) -> Path:
        """Download and verify the installer for the known newer release."""
        if self.release is None or self.busy:
            raise RuntimeError("没有可用的更新")
        if not getattr(sys, "frozen", False):
            raise RuntimeError("自动更新仅支持已安装的 Windows 助手")
        if self.has_running_jobs():
            raise RuntimeError("有发布任务正在执行，请等待任务完成后再更新")
        self.busy = True
        try:
            installer = updater.download_release(
                self.client, self.release, self.data_root
            )
            updater.cleanup_stale_installers(self.data_root, keep=installer)
            return installer
        finally:
            self.busy = False

    def prepare_install(self) -> tuple[bool, str]:
        """Download the update and mark it ready for the next shutdown."""
        try:
            installer = self.download()
        except (AgentApiError, OSError, RuntimeError) as exc:
            return False, f"更新下载失败：{exc}"
        self.pending_installer = installer
        return True, "更新已就绪，助手即将重启并完成安装"


def _start_background_update_checks(
    updater_state: AgentUpdater, notify=None
) -> None:
    """Poll the server for newer installers while the desktop helper runs."""

    def worker() -> None:
        time.sleep(UPDATE_CHECK_DELAY_SECONDS)
        while not updater_state.application.stopping:
            try:
                found, message = updater_state.check()
                if found and notify is not None:
                    notify(message)
            except Exception:
                pass
            for _ in range(int(UPDATE_CHECK_INTERVAL_SECONDS)):
                if updater_state.application.stopping:
                    return
                time.sleep(1)

    threading.Thread(target=worker, name="mpau-agent-update-check", daemon=True).start()


def _tray_image():
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((2, 2, 62, 62), radius=18, fill=theme.GREEN_800)
    draw.rounded_rectangle((13, 13, 51, 51), radius=11, fill=theme.ORANGE_500)
    draw.text(
        (32, 31),
        "M",
        fill="white",
        anchor="mm",
        stroke_width=2,
        stroke_fill="white",
    )
    return image


def _run_tray(
    application: LocalAgentApplication,
    connection: StoredConnection,
    store: AgentConnectionStore,
    data_root: Path,
) -> Path | None:
    try:
        import pystray
    except ImportError:
        return _run_status_window(application, connection, data_root)

    updater_state = AgentUpdater(application, data_root)

    def open_console(_icon=None, _item=None) -> None:
        open_url(connection.server_url)

    def quit_agent(icon, _item=None) -> None:
        application.stop()
        icon.stop()

    def disconnect(icon, _item=None) -> None:
        try:
            application.client.revoke_device(application.agent_id)
        except AgentApiError:
            pass
        store.clear()
        set_autostart(False)
        quit_agent(icon)

    def notify(message: str) -> None:
        try:
            icon.notify(message, "MPAU 本地执行助手")
        except Exception:
            pass

    def check_update(icon, _item=None) -> None:
        def worker() -> None:
            try:
                found, message = updater_state.check()
            except Exception as exc:
                notify(f"检查更新失败：{exc}")
                return
            notify(message if found else f"检查完成：{message}")

        threading.Thread(target=worker, name="mpau-update-check-once", daemon=True).start()

    def install_update(icon, _item=None) -> None:
        if updater_state.busy:
            notify("正在下载更新，请稍候")
            return
        if updater_state.has_running_jobs():
            notify("有发布任务正在执行，请等待任务完成后再更新")
            return
        release = updater_state.release
        if release is None:
            check_update(icon)
            return
        notify(f"正在下载新版本 v{release['version']}，下载完成后会自动重启安装")

        def worker() -> None:
            ready, message = updater_state.prepare_install()
            if not ready:
                notify(message)
                return
            notify("更新已下载完成，助手即将退出并安装新版本")
            time.sleep(2)
            application.stop()
            icon.stop()

        threading.Thread(target=worker, name="mpau-update-install", daemon=True).start()

    user_label = connection.user.get("display_name") or connection.user.get("username")
    menu = pystray.Menu(
        pystray.MenuItem("打开商家发布台", open_console, default=True),
        pystray.MenuItem(f"已连接：{user_label}", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("检查更新", check_update),
        pystray.MenuItem(
            lambda item: (
                f"安装新版本 v{updater_state.release['version']}"
                if updater_state.release
                else "安装新版本"
            ),
            install_update,
            visible=lambda item: updater_state.release is not None,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("解除配对并退出", disconnect),
        pystray.MenuItem("退出助手", quit_agent),
    )
    icon = pystray.Icon(
        "MPAU-Agent",
        _tray_image(),
        f"MPAU 本地执行助手：已连接（v{__version__}）",
        menu,
    )

    def watch_application() -> None:
        while not application.stopping:
            time.sleep(0.5)
        if application.authorization_failed:
            icon.stop()

    threading.Thread(
        target=watch_application,
        name="mpau-agent-tray-monitor",
        daemon=True,
    ).start()
    updater.cleanup_stale_installers(data_root)
    _start_background_update_checks(updater_state, notify=notify)
    icon.run()
    return updater_state.pending_installer


def _run_status_window(
    application: LocalAgentApplication,
    connection: StoredConnection,
    data_root: Path,
) -> Path | None:
    import tkinter as tk
    from tkinter import messagebox

    updater_state = AgentUpdater(application, data_root)
    pending: list[Path | None] = []

    root = tk.Tk()
    root.title("MPAU 本地执行助手")
    root.resizable(False, False)
    root.configure(bg=theme.CREAM)
    theme.apply_tk_scaling(root)
    theme.center_window(root, 500, 640)

    user_label = (
        connection.user.get("display_name") or connection.user.get("username")
    )

    theme.header_band(
        root,
        "MPAU 本地执行助手",
        "本地任务执行组件正在后台运行",
    ).pack(fill="x")

    body = tk.Frame(root, bg=theme.CREAM)
    body.pack(fill="both", expand=True, padx=24)

    # -- 连接状态卡片 --------------------------------------------------
    conn_card = theme.card(body)
    conn_card.pack(fill="x", pady=(20, 0))
    conn_inner = tk.Frame(conn_card, bg=theme.CARD)
    conn_inner.pack(fill="x", padx=20, pady=(16, 18))

    conn_row = tk.Frame(conn_inner, bg=theme.CARD)
    conn_row.pack(fill="x")
    theme.status_dot(conn_row, theme.STATUS_ONLINE).pack(side="left", padx=(0, 8))
    tk.Label(
        conn_row,
        text="已连接 · 正在后台运行",
        bg=theme.CARD,
        fg=theme.TEXT_900,
        font=theme.font(12, "bold"),
    ).pack(side="left")
    tk.Label(
        conn_row,
        text=f"v{__version__}",
        bg=theme.CARD,
        fg=theme.TEXT_400,
        font=theme.font(9),
    ).pack(side="right")

    tk.Label(
        conn_inner,
        text=connection.server_url,
        bg=theme.CARD,
        fg=theme.TEXT_600,
        font=theme.mono_font(9),
        anchor="w",
    ).pack(fill="x", pady=(6, 14))

    user_row = tk.Frame(conn_inner, bg=theme.CARD)
    user_row.pack(fill="x", pady=(0, 14))
    theme.avatar_canvas(user_row, user_label or "?", 36, theme.CARD).pack(
        side="left", padx=(0, 10)
    )
    user_wrap = tk.Frame(user_row, bg=theme.CARD)
    user_wrap.pack(side="left")
    tk.Label(
        user_wrap,
        text=user_label or "",
        bg=theme.CARD,
        fg=theme.TEXT_900,
        font=theme.font(10, "bold"),
        anchor="w",
    ).pack(fill="x")
    tk.Label(
        user_wrap,
        text="已配对账号",
        bg=theme.CARD,
        fg=theme.TEXT_400,
        font=theme.font(9),
        anchor="w",
    ).pack(fill="x")

    theme.primary_button(
        conn_inner,
        "打开商家发布台",
        lambda: open_url(connection.server_url),
    ).pack(fill="x", ipady=5)

    # -- 软件更新卡片 --------------------------------------------------
    update_card = theme.card(body)
    update_card.pack(fill="x", pady=(14, 0))
    update_inner = tk.Frame(update_card, bg=theme.CARD)
    update_inner.pack(fill="x", padx=20, pady=(16, 18))

    tk.Label(
        update_inner,
        text="软件更新",
        bg=theme.CARD,
        fg=theme.TEXT_900,
        font=theme.font(11, "bold"),
        anchor="w",
    ).pack(fill="x")

    update_hint = tk.StringVar(value="")
    update_banner = theme.update_banner(update_inner, update_hint)
    update_status = tk.StringVar(value="有新版本时会在这里提示，也可手动检查")

    def show_banner(text: str) -> None:
        update_hint.set(text)
        update_banner.pack(fill="x", pady=(10, 0), before=update_status_label)

    def check_updates() -> None:
        def worker() -> None:
            try:
                found, message = updater_state.check()
            except Exception as exc:
                found, message = False, f"检查更新失败：{exc}"
            root.after(0, lambda: apply_check_result(found, message))

        threading.Thread(target=worker, name="mpau-update-check", daemon=True).start()

    def apply_check_result(found: bool, message: str) -> None:
        if found:
            show_banner(f"{message}，点击“安装新版本”自动更新")
        else:
            update_status.set(message)

    def install_updates() -> None:
        if updater_state.busy:
            return
        if updater_state.release is None:
            found, message = updater_state.check()
            if not found:
                update_status.set(message)
                return
            show_banner(f"{message}，即将开始下载")

        def confirm_and_restart() -> None:
            if not messagebox.askyesno(
                "更新已就绪", "新版本已下载完成。是否立即重启助手并安装？"
            ):
                update_status.set("已下载，可稍后点击“安装新版本”完成安装")
                return
            pending.append(updater_state.pending_installer)
            close()

        def worker() -> None:
            ready, message = updater_state.prepare_install()
            if not ready:
                root.after(0, lambda: update_status.set(message))
                return
            root.after(0, confirm_and_restart)

        update_status.set("正在下载新版本…")
        threading.Thread(target=worker, name="mpau-update-install", daemon=True).start()

    update_actions = tk.Frame(update_inner, bg=theme.CARD)
    update_actions.pack(fill="x", pady=(12, 0))
    theme.secondary_button(update_actions, "检查更新", check_updates).pack(
        side="left"
    )
    theme.primary_button(update_actions, "安装新版本", install_updates).pack(
        side="left", padx=(10, 0)
    )

    update_status_label = tk.Label(
        update_inner,
        textvariable=update_status,
        bg=theme.CARD,
        fg=theme.TEXT_600,
        font=theme.font(9),
        wraplength=380,
        justify="left",
        anchor="w",
    )
    update_status_label.pack(fill="x", pady=(12, 0))

    tk.Label(
        body,
        text="关闭窗口将退出本地执行助手",
        bg=theme.CREAM,
        fg=theme.TEXT_400,
        font=theme.font(9),
    ).pack(pady=(14, 20))

    def close() -> None:
        application.stop()
        root.destroy()

    def watch_application() -> None:
        if application.stopping:
            root.destroy()
            return
        root.after(500, watch_application)

    root.protocol("WM_DELETE_WINDOW", close)
    root.after(500, watch_application)
    updater.cleanup_stale_installers(data_root)
    _start_background_update_checks(updater_state)
    root.mainloop()
    return pending[0] if pending else None


def _connect_when_available(application: LocalAgentApplication) -> bool:
    """Keep an autostarted helper alive while the local network is unavailable."""
    while True:
        try:
            application.connect()
            return True
        except AgentApiError as exc:
            if exc.status == 401:
                return False
            time.sleep(5)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MPAU 本地执行助手桌面程序")
    parser.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--data-dir", type=Path, default=default_data_root())
    return parser


def run() -> None:
    if os.name != "nt":
        raise SystemExit("MPAU 本地执行助手仅支持 Windows")
    sys.excepthook = _log_and_show_unhandled_exception
    threading.excepthook = lambda args: _log_and_show_unhandled_exception(
        args.exc_type, args.exc_value, args.exc_traceback
    )
    theme.enable_dpi_awareness()
    args = build_parser().parse_args()
    if not _acquire_single_instance():
        return
    store = AgentConnectionStore(args.data_dir)
    try:
        connection = store.load()
    except ValueError:
        store.clear()
        connection = None
    if connection is None:
        connection = _pairing_dialog(store, args.data_dir)
    if connection is None:
        return

    while connection is not None:
        client = AgentApiClient(connection.server_url, connection.agent_token)
        application = LocalAgentApplication(client, data_root=args.data_dir, poll_seconds=2)
        if not _connect_when_available(application):
            store.clear()
            connection = _pairing_dialog(
                store, args.data_dir, initial_server=connection.server_url
            )
            continue

        worker = threading.Thread(
            target=application.run,
            kwargs={"already_connected": True},
            name="mpau-agent-worker",
            daemon=True,
        )
        worker.start()
        pending_installer = _run_tray(application, connection, store, args.data_dir)
        application.stop()
        worker.join(timeout=15)
        if pending_installer is not None:
            updater.launch_update(args.data_dir, pending_installer)
            return
        if not application.authorization_failed:
            return

        previous_server = connection.server_url
        store.clear()
        connection = _pairing_dialog(
            store, args.data_dir, initial_server=previous_server
        )


if __name__ == "__main__":
    run()
