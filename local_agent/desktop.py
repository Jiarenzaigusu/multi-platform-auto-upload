from __future__ import annotations

import argparse
import os
from pathlib import Path
import queue
import socket
import sys
import threading
import time
import traceback
from collections.abc import Callable

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
_WAKE_HOST = "127.0.0.1"
_WAKE_PORT = 48766
_WAKE_TOKEN = "show"

UPDATE_CHECK_INTERVAL_SECONDS = 6 * 60 * 60
UPDATE_CHECK_DELAY_SECONDS = 90
CONNECTION_FAILURES_BEFORE_REPAIR = 3


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
        return


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


def _log_background_thread_exception(args) -> None:
    if issubclass(args.exc_type, KeyboardInterrupt):
        return
    logger.error(
        "桌面助手后台线程 {} 发生异常\n{}",
        args.thread.name if args.thread else "unknown",
        "".join(
            traceback.format_exception(
                args.exc_type, args.exc_value, args.exc_traceback
            )
        ),
    )


def _acquire_single_instance() -> bool:
    global _WINDOWS_MUTEX
    if os.name != "nt":
        return True
    import ctypes

    _WINDOWS_MUTEX = ctypes.windll.kernel32.CreateMutexW(
        None, False, "Local\\MPAU-Agent-Desktop"
    )
    already_running = bool(ctypes.windll.kernel32.GetLastError() == 183)
    if already_running:
        _notify_existing_instance()
        return False
    return bool(_WINDOWS_MUTEX)


def _notify_existing_instance() -> None:
    """Ask the already-running helper to show its status window."""
    if os.name != "nt":
        return
    try:
        with socket.create_connection((_WAKE_HOST, _WAKE_PORT), timeout=0.4) as conn:
            conn.sendall(_WAKE_TOKEN.encode("ascii"))
    except OSError:
        pass


def _start_wake_listener(
    application: LocalAgentApplication, on_wake
) -> Callable[[], None]:
    """Listen on localhost so a second double-click can reveal the UI."""
    if os.name != "nt":
        return lambda: None

    stop_event = threading.Event()

    def worker() -> None:
        try:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((_WAKE_HOST, _WAKE_PORT))
            server.listen(5)
            server.settimeout(0.5)
        except OSError as exc:
            logger.warning("无法启动助手窗口唤醒服务：{}", exc)
            return
        with server:
            while not application.stopping and not stop_event.is_set():
                try:
                    conn, _address = server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                with conn:
                    try:
                        message = conn.recv(32).decode("ascii", errors="ignore")
                    except OSError:
                        continue
                if message == _WAKE_TOKEN:
                    try:
                        on_wake()
                    except Exception:
                        logger.exception("显示助手状态窗口失败")

    listener = threading.Thread(
        target=worker,
        name="mpau-agent-window-wake",
        daemon=True,
    )
    listener.start()

    def stop() -> None:
        stop_event.set()
        try:
            with socket.create_connection((_WAKE_HOST, _WAKE_PORT), timeout=0.2):
                pass
        except OSError:
            pass
        listener.join(timeout=1)

    return stop


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
    server_entry.insert(0, initial_server or "http://")
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
            server = _server_url(raw_server)
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
        self.progress: tuple[int, int | None] = (0, None)

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

    def download(self, progress=None) -> Path:
        """Download and verify the installer for the known newer release."""
        if self.release is None or self.busy:
            raise RuntimeError("没有可用的更新")
        if not getattr(sys, "frozen", False):
            raise RuntimeError("自动更新仅支持已安装的 Windows 助手")
        if self.has_running_jobs():
            raise RuntimeError("有发布任务正在执行，请等待任务完成后再更新")
        self.busy = True
        self.progress = (0, self.release.get("size") or None)
        try:
            installer = updater.download_release(
                self.client,
                self.release,
                self.data_root,
                progress=progress,
            )
            updater.cleanup_stale_installers(self.data_root, keep=installer)
            return installer
        finally:
            self.busy = False

    def prepare_install(self, progress=None) -> tuple[bool, str]:
        """Download the update and mark it ready for the next shutdown."""
        if self.pending_installer is not None and self.pending_installer.is_file():
            return True, "更新已就绪，助手即将重启并完成安装"
        try:
            installer = self.download(progress=progress)
        except (AgentApiError, OSError, RuntimeError) as exc:
            return False, f"更新下载失败：{exc}"
        self.pending_installer = installer
        return True, "更新已就绪，助手即将重启并完成安装"


def _format_bytes(value: int | None) -> str:
    if value is None:
        return ""
    size = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _start_background_update_checks(
    updater_state: AgentUpdater, notify=None
) -> None:
    """Poll the server for newer installers while the desktop helper runs."""

    def worker() -> None:
        try:
            found, message = updater_state.check()
        except Exception:
            found = False
            message = ""
        else:
            if found and notify is not None:
                notify(message)
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


def _revoke_pairing(application: LocalAgentApplication, store: AgentConnectionStore) -> None:
    """撤销本机与服务器的配对并清除本地连接状态。"""
    try:
        application.client.revoke_device(application.agent_id)
    except AgentApiError:
        pass
    application.mark_disconnected()
    store.clear()
    set_autostart(False)


def _run_tray(
    application: LocalAgentApplication,
    connection: StoredConnection,
    store: AgentConnectionStore,
    data_root: Path,
    *,
    show_status_on_start: bool = False,
    on_ready: Callable[[], None] | None = None,
) -> str:
    try:
        previous_backend = os.environ.get("PYSTRAY_BACKEND")
        os.environ["PYSTRAY_BACKEND"] = "win32"
        import pystray
        if pystray.Icon.__module__ != "pystray._win32":
            raise RuntimeError(
                f"加载了错误的托盘后端：{pystray.Icon.__module__ or 'unknown'}"
            )
    except Exception as exc:
        message = "Windows 托盘组件加载失败，助手不会继续后台运行。请重新安装完整版本。"
        logger.exception("{}：{}", message, exc)
        _show_fatal_error(message)
        application.stop()
        application.disconnect()
        return "tray-failed"
    finally:
        if previous_backend is None:
            os.environ.pop("PYSTRAY_BACKEND", None)
        else:
            os.environ["PYSTRAY_BACKEND"] = previous_backend

    updater_state = AgentUpdater(application, data_root)
    status_window_lock = threading.Lock()
    status_window_open = False
    tray_outcome = "quit"
    tray_failure: list[str] = []

    def open_console(_icon=None, _item=None) -> None:
        open_url(connection.server_url)

    def notify(message: str) -> None:
        try:
            icon.update_menu()
        except Exception:
            pass
        try:
            icon.notify(message, "MPAU 本地执行助手")
        except Exception:
            pass

    def quit_agent(icon, _item=None) -> None:
        application.stop()
        application.disconnect()
        icon.stop()

    def disconnect(icon, _item=None) -> None:
        nonlocal tray_outcome
        _revoke_pairing(application, store)
        tray_outcome = "re-pair"
        application.stop()
        icon.stop()

    def open_status_window(
        _icon=None, _item=None, *, auto_install: bool = False
    ) -> None:
        nonlocal status_window_open
        with status_window_lock:
            if status_window_open:
                notify("助手窗口已经打开")
                return
            status_window_open = True

        def worker() -> None:
            nonlocal status_window_open, tray_outcome
            try:
                window_outcome = _run_status_window(
                    application,
                    connection,
                    data_root,
                    store,
                    updater_state=updater_state,
                    start_update_checks=False,
                    auto_install_on_open=auto_install,
                )
                if window_outcome == "re-pair":
                    tray_outcome = "re-pair"
                if application.stopping:
                    icon.stop()
            finally:
                with status_window_lock:
                    status_window_open = False

        threading.Thread(
            target=worker,
            name="mpau-status-window",
            daemon=True,
        ).start()

    def check_update(icon, _item=None) -> None:
        def worker() -> None:
            try:
                found, message = updater_state.check()
            except Exception as exc:
                notify(f"检查更新失败：{exc}")
                return
            try:
                icon.update_menu()
            except Exception:
                pass
            notify(message if found else f"检查完成：{message}")

        threading.Thread(target=worker, name="mpau-update-check-once", daemon=True).start()

    def install_update(icon, _item=None) -> None:
        if updater_state.busy:
            notify("正在下载更新，请打开助手窗口查看进度")
            open_status_window(icon)
            return
        if updater_state.has_running_jobs():
            notify("有发布任务正在执行，请等待任务完成后再更新")
            return
        release = updater_state.release
        if release is None:
            notify("正在检查更新，请稍候")
            open_status_window(icon, auto_install=True)
            return
        notify(f"发现新版本 v{release['version']}，正在打开更新窗口")
        open_status_window(icon, auto_install=True)

    user_label = connection.user.get("display_name") or connection.user.get("username")
    icon = None
    stop_wake_listener = lambda: None

    def watch_application() -> None:
        while not application.stopping:
            time.sleep(0.5)
        if application.authorization_failed and icon is not None:
            icon.stop()

    def setup(_icon) -> None:
        # pystray only makes the icon visible automatically when no custom
        # setup callback is supplied.
        try:
            _icon.visible = True
            if not _icon.visible:
                raise RuntimeError("托盘后端没有确认图标可见")
            logger.info("Windows 托盘图标已创建并显示")
            if on_ready is not None:
                on_ready()
            _start_background_update_checks(updater_state, notify=notify)
        except Exception as exc:
            message = f"Windows 托盘图标显示失败，助手无法继续运行：{exc}"
            tray_failure.append(message)
            logger.exception("{}", message)
            application.stop()
            _icon.stop()
            return
        if show_status_on_start:
            open_status_window()

    try:
        menu = pystray.Menu(
            pystray.MenuItem("打开助手窗口", open_status_window, default=True),
            pystray.MenuItem("打开商家发布台", open_console),
            pystray.MenuItem(f"已连接：{user_label}", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("检查更新", check_update),
            pystray.MenuItem(
                lambda item: (
                    f"安装新版本 v{updater_state.release['version']}"
                    if updater_state.release
                    else "检查并安装新版本"
                ),
                install_update,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("解除配对", disconnect),
            pystray.MenuItem("退出助手", quit_agent),
        )
        icon = pystray.Icon(
            "MPAU-Agent",
            _tray_image(),
            f"MPAU 本地执行助手：已连接（v{__version__}）",
            menu,
        )
        threading.Thread(
            target=watch_application,
            name="mpau-agent-tray-monitor",
            daemon=True,
        ).start()
        stop_wake_listener = _start_wake_listener(application, open_status_window)
        icon.run(setup=setup)
        if tray_failure:
            raise RuntimeError(tray_failure[0])
        if not application.stopping:
            raise RuntimeError("Windows 托盘循环意外结束")
    except Exception as exc:
        message = f"Windows 托盘初始化失败，助手无法继续运行：{exc}"
        logger.exception("{}", message)
        _show_fatal_error(message)
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass
        application.stop()
        application.disconnect()
        return "tray-failed"
    finally:
        stop_wake_listener()
    return tray_outcome


def _run_status_window(
    application: LocalAgentApplication,
    connection: StoredConnection,
    data_root: Path,
    store: AgentConnectionStore,
    *,
    updater_state: AgentUpdater | None = None,
    start_update_checks: bool = True,
    auto_install_on_open: bool = False,
) -> str:
    import tkinter as tk
    from tkinter import messagebox, ttk

    updater_state = updater_state or AgentUpdater(application, data_root)
    ui_queue: queue.Queue[tuple[str, object]] = queue.Queue()
    closing = False
    outcome = "closed"
    checking = False
    progress_indeterminate = False

    root = tk.Tk()
    root.title("MPAU 本地执行助手")
    root.resizable(False, False)
    root.configure(bg=theme.CREAM)
    theme.apply_tk_scaling(root)
    theme.center_window(root, 520, 760)

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
    update_banner_visible = False
    update_status = tk.StringVar(value="有新版本时会在这里提示，也可手动检查")
    progress_label = tk.StringVar(value="")

    progress_bar = ttk.Progressbar(
        update_inner,
        orient="horizontal",
        mode="determinate",
        maximum=100,
    )
    progress_text = tk.Label(
        update_inner,
        textvariable=progress_label,
        bg=theme.CARD,
        fg=theme.TEXT_600,
        font=theme.font(9),
        anchor="w",
    )

    def show_banner(text: str) -> None:
        nonlocal update_banner_visible
        update_hint.set(text)
        if not update_banner_visible:
            update_banner.pack(fill="x", pady=(10, 0), before=update_status_label)
            update_banner_visible = True

    def hide_banner() -> None:
        nonlocal update_banner_visible
        if update_banner_visible:
            update_banner.pack_forget()
            update_banner_visible = False

    def set_progress_visible(visible: bool) -> None:
        if visible and not progress_bar.winfo_manager():
            progress_bar.pack(fill="x", pady=(12, 0), before=update_actions)
            progress_text.pack(fill="x", pady=(6, 0), before=update_actions)
        elif not visible and progress_bar.winfo_manager():
            progress_bar.pack_forget()
            progress_text.pack_forget()

    def set_buttons_enabled(enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        check_button.configure(state=state)
        install_button.configure(state=state)

    def apply_progress(downloaded: int, total: int | None) -> None:
        nonlocal progress_indeterminate
        set_progress_visible(True)
        if total and total > 0:
            if progress_indeterminate:
                progress_bar.stop()
                progress_bar.configure(mode="determinate")
                progress_indeterminate = False
            percent = min(100, int(downloaded * 100 / total))
            progress_bar.configure(value=percent)
            progress_label.set(
                f"下载进度 {percent}% · {_format_bytes(downloaded)} / {_format_bytes(total)}"
            )
            update_status.set(f"正在下载新版本安装包：{percent}%")
        else:
            if not progress_indeterminate:
                progress_bar.configure(mode="indeterminate")
                progress_bar.start(12)
                progress_indeterminate = True
            progress_label.set(f"已下载 {_format_bytes(downloaded)}")
            update_status.set("正在下载新版本安装包…")

    def enqueue(kind: str, payload: object = None) -> None:
        ui_queue.put((kind, payload))

    def drain_queue() -> None:
        nonlocal checking, closing, progress_indeterminate
        while True:
            try:
                kind, payload = ui_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "check-result":
                checking = False
                set_buttons_enabled(True)
                found, message, auto_install = payload  # type: ignore[misc]
                if found:
                    show_banner(f"{message}，点击“安装新版本”自动更新")
                    update_status.set("新版本可用，点击“安装新版本”开始下载")
                    if auto_install:
                        root.after(100, install_updates)
                else:
                    hide_banner()
                    update_status.set(str(message))
            elif kind == "progress":
                downloaded, total = payload  # type: ignore[misc]
                apply_progress(int(downloaded), total if isinstance(total, int) else None)
            elif kind == "install-result":
                set_buttons_enabled(True)
                ready, message = payload  # type: ignore[misc]
                if not ready:
                    if progress_indeterminate:
                        progress_bar.stop()
                        progress_bar.configure(mode="determinate")
                        progress_indeterminate = False
                    update_status.set(str(message))
                    continue
                progress_bar.configure(mode="determinate", value=100)
                progress_label.set("下载进度 100% · 安装包已校验")
                update_status.set("更新已下载完成，准备打开安装窗口")
                confirm_and_install()
            elif kind == "show-window":
                try:
                    root.deiconify()
                    root.lift()
                    root.focus_force()
                    root.attributes("-topmost", True)
                    root.after(800, lambda: root.attributes("-topmost", False))
                except Exception:
                    pass
            elif kind == "application-stopped":
                closing = True
                root.destroy()
                return
            elif kind == "re-pair":
                closing = True
                root.destroy()
                return
        if not closing:
            root.after(100, drain_queue)

    def check_updates(*, auto_install: bool = False) -> None:
        nonlocal checking
        if checking or updater_state.busy:
            return
        checking = True
        set_buttons_enabled(False)
        update_status.set("正在检查更新…")

        def worker() -> None:
            try:
                found, message = updater_state.check()
            except Exception as exc:
                found, message = False, f"检查更新失败：{exc}"
            enqueue("check-result", (found, message, auto_install))

        threading.Thread(target=worker, name="mpau-update-check", daemon=True).start()

    def confirm_and_install() -> None:
        installer = updater_state.pending_installer
        if installer is None:
            update_status.set("安装包状态异常，请重新检查更新")
            return
        if not messagebox.askyesno(
            "更新已就绪",
            "新版本已下载完成。是否立即打开安装向导并退出旧助手？",
        ):
            update_status.set("已下载，可稍后点击“安装新版本”打开安装窗口")
            return
        update_status.set("正在打开安装窗口…")
        try:
            updater.launch_update(data_root, installer)
        except Exception as exc:
            message = f"启动安装失败：{exc}"
            update_status.set(message)
            messagebox.showerror("无法打开更新安装程序", message)
            return
        updater_state.pending_installer = None
        close(for_install=True)

    def install_updates() -> None:
        if updater_state.busy:
            update_status.set("正在下载更新，请稍候…")
            return
        if updater_state.has_running_jobs():
            update_status.set("有发布任务正在执行，请等待任务完成后再更新")
            return
        if updater_state.pending_installer is not None:
            confirm_and_install()
            return
        if updater_state.release is None:
            check_updates(auto_install=True)
            return
        set_buttons_enabled(False)
        show_banner(f"发现新版本 v{updater_state.release['version']}，正在下载")
        set_progress_visible(True)
        progress_bar.configure(mode="determinate", value=0)
        progress_label.set("准备下载…")
        update_status.set("正在下载新版本安装包：0%")

        def progress(downloaded: int, total: int | None = None) -> None:
            updater_state.progress = (downloaded, total)
            enqueue("progress", (downloaded, total))

        def worker() -> None:
            ready, message = updater_state.prepare_install(progress=progress)
            enqueue("install-result", (ready, message))

        threading.Thread(target=worker, name="mpau-update-install", daemon=True).start()

    update_actions = tk.Frame(update_inner, bg=theme.CARD)
    update_actions.pack(fill="x", pady=(12, 0))
    check_button = theme.secondary_button(update_actions, "检查更新", lambda: check_updates())
    check_button.pack(side="left")
    install_button = theme.primary_button(update_actions, "安装新版本", install_updates)
    install_button.pack(side="left", padx=(10, 0))

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

    def disconnect_and_reconfigure() -> None:
        nonlocal outcome
        if not messagebox.askyesno(
            "解除配对",
            "确定要解除本机与服务器的配对并重新配置吗？\n助手将返回最开始的配置界面，后台进程不会退出。",
        ):
            return

        def worker() -> None:
            nonlocal outcome
            _revoke_pairing(application, store)
            outcome = "re-pair"
            application.stop()
            enqueue("re-pair")

        threading.Thread(target=worker, name="mpau-reconfigure", daemon=True).start()

    def quit_from_window() -> None:
        if not messagebox.askyesno(
            "退出助手",
            "确定要退出本地执行助手吗？\n退出后任务栏托盘也会同步关闭。",
        ):
            return
        application.stop()
        application.disconnect()
        enqueue("application-stopped")

    account_actions = tk.Frame(body, bg=theme.CREAM)
    account_actions.pack(fill="x", pady=(16, 0))
    theme.danger_button(
        account_actions,
        "解除配对",
        disconnect_and_reconfigure,
        side="left",
    )
    theme.secondary_button(
        account_actions,
        "退出助手",
        quit_from_window,
    ).pack(side="left", padx=(10, 0))

    tk.Label(
        body,
        text="关闭窗口不会退出助手；需要退出请点击“退出助手”或右键托盘图标",
        bg=theme.CREAM,
        fg=theme.TEXT_400,
        font=theme.font(9),
    ).pack(pady=(14, 20))

    def close(*, for_install: bool = False) -> None:
        nonlocal closing
        closing = True
        if for_install:
            application.stop()
            application.disconnect()
        root.destroy()

    def watch_application() -> None:
        if application.stopping:
            enqueue("application-stopped")
            return
        root.after(500, watch_application)

    root.protocol("WM_DELETE_WINDOW", close)
    root.after(100, drain_queue)
    root.after(500, watch_application)
    if start_update_checks:
        _start_background_update_checks(updater_state)
    if auto_install_on_open:
        root.after(250, install_updates)
    else:
        check_updates()
    try:
        root.lift()
        root.attributes("-topmost", True)
        root.after(1000, lambda: root.attributes("-topmost", False))
    except Exception:
        pass
    root.mainloop()
    return outcome

def _connect_with_status_window(
    application: LocalAgentApplication, *, start_hidden: bool = False
) -> str:
    """Connect with visible retry feedback, remaining quiet for autostart."""
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.title("MPAU 本地执行助手")
    root.resizable(False, False)
    root.configure(bg=theme.CREAM)
    theme.apply_tk_scaling(root)
    theme.center_window(root, 540, 420)

    theme.header_band(
        root,
        "MPAU 本地执行助手",
        "正在连接商家发布台",
    ).pack(fill="x")
    body = tk.Frame(root, bg=theme.CREAM)
    body.pack(fill="both", expand=True, padx=32, pady=(28, 24))
    status = tk.StringVar(value=f"正在连接：{application.client.server_url}")
    detail = tk.Label(
        body,
        textvariable=status,
        bg=theme.CREAM,
        fg=theme.TEXT_600,
        font=theme.font(10),
        justify="left",
        anchor="w",
        wraplength=460,
    )
    detail.pack(fill="x")
    ui_queue: queue.Queue[tuple[str, str]] = queue.Queue()
    running = True
    connecting = False
    outcome = "cancelled"
    retry_timer = None
    consecutive_failures = 0

    def close() -> None:
        nonlocal running
        running = False
        root.destroy()

    def attempt() -> None:
        nonlocal connecting, retry_timer
        if not running or connecting:
            return
        if retry_timer is not None:
            root.after_cancel(retry_timer)
            retry_timer = None
        connecting = True
        retry_button.configure(state="disabled", text="正在连接…")
        status.set(f"正在连接：{application.client.server_url}")

        def worker() -> None:
            try:
                application.connect()
            except AgentApiError as exc:
                if exc.status == 401:
                    ui_queue.put(("unauthorized", str(exc)))
                    return
                ui_queue.put(("retry", str(exc)))
                return
            except Exception as exc:
                ui_queue.put(("fatal", str(exc)))
                return
            if not running:
                application.stop()
                application.disconnect()
                return
            ui_queue.put(("connected", ""))

        threading.Thread(target=worker, name="mpau-connect", daemon=True).start()

    def scheduled_attempt() -> None:
        nonlocal retry_timer
        retry_timer = None
        attempt()

    def show_window() -> None:
        root.deiconify()
        root.lift()
        root.focus_force()
        root.attributes("-topmost", True)
        root.after(800, lambda: root.attributes("-topmost", False))

    def drain_queue() -> None:
        nonlocal connecting, outcome, retry_timer, running, consecutive_failures
        while True:
            try:
                kind, message = ui_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "show-window":
                show_window()
            elif kind == "connected":
                consecutive_failures = 0
                outcome = "connected"
                running = False
                root.destroy()
                return
            elif kind == "unauthorized":
                outcome = "unauthorized"
                running = False
                root.destroy()
                return
            elif kind == "retry":
                connecting = False
                consecutive_failures += 1
                retry_button.configure(state="normal", text="立即重试")
                status.set(
                    f"连接失败：{message}\n\n将于 5 秒后自动重试，也可以点击“立即重试”。"
                )
                if consecutive_failures >= CONNECTION_FAILURES_BEFORE_REPAIR:
                    show_window()
                    reconnect = messagebox.askyesno(
                        "多次连接失败",
                        "连续 3 次无法连接发布台。是否断开当前配对并重新配对？\n\n"
                        "如果发布台地址或 IP 已变更，请选择“是”。",
                        parent=root,
                    )
                    consecutive_failures = 0
                    if reconnect:
                        outcome = "re-pair"
                        running = False
                        root.destroy()
                        return
                retry_timer = root.after(5000, scheduled_attempt)
            elif kind == "fatal":
                connecting = False
                retry_button.configure(state="normal", text="立即重试")
                status.set(f"启动本机服务失败：{message}")
                show_window()
        if running:
            root.after(100, drain_queue)

    retry_button = theme.primary_button(body, "立即重试", attempt)
    retry_button.pack(fill="x", pady=(28, 0), ipady=6)
    cancel_button = theme.secondary_button(body, "退出助手", close)
    cancel_button.pack(fill="x", pady=(12, 0), ipady=5)
    root.protocol("WM_DELETE_WINDOW", close)
    if start_hidden:
        root.withdraw()
    stop_wake_listener = _start_wake_listener(
        application, lambda: ui_queue.put(("show-window", ""))
    )
    root.after(50, attempt)
    root.after(100, drain_queue)
    try:
        root.mainloop()
    finally:
        stop_wake_listener()
    return outcome


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MPAU 本地执行助手桌面程序")
    parser.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--data-dir", type=Path, default=default_data_root())
    return parser


def run() -> None:
    if os.name != "nt":
        raise SystemExit("MPAU 本地执行助手仅支持 Windows")
    sys.excepthook = _log_and_show_unhandled_exception
    threading.excepthook = _log_background_thread_exception
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
        connect_outcome = _connect_with_status_window(
            application, start_hidden=args.background
        )
        if connect_outcome == "cancelled":
            return
        if connect_outcome in {"unauthorized", "re-pair"}:
            previous_server = connection.server_url
            store.clear()
            connection = _pairing_dialog(
                store,
                args.data_dir,
                initial_server="" if connect_outcome == "re-pair" else previous_server,
            )
            continue

        worker = threading.Thread(
            target=application.run,
            kwargs={"already_connected": True},
            name="mpau-agent-worker",
            daemon=True,
        )
        worker_started = threading.Event()

        def start_worker_after_tray_ready() -> None:
            worker.start()
            worker_started.set()

        tray_outcome = _run_tray(
            application,
            connection,
            store,
            args.data_dir,
            show_status_on_start=not args.background,
            on_ready=start_worker_after_tray_ready,
        )
        application.stop()
        application.disconnect()
        if worker_started.is_set():
            worker.join(timeout=15)
        if tray_outcome == "re-pair":
            connection = _pairing_dialog(store, args.data_dir)
            continue
        if tray_outcome == "tray-failed":
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
