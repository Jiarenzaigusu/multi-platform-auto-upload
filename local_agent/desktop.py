from __future__ import annotations

import argparse
from pathlib import Path
import sys
import threading
import time

from local_agent.autostart import open_url, set_windows_autostart
from local_agent.client import AgentApiClient, AgentApiError
from local_agent.credentials import AgentConnectionStore, StoredConnection
from local_agent.main import LocalAgentApplication, _server_url
from local_agent.paths import default_data_root


_WINDOWS_MUTEX = None


def _acquire_single_instance() -> bool:
    global _WINDOWS_MUTEX
    if sys.platform != "win32":
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
    root.geometry("520x360")
    root.resizable(False, False)
    root.configure(bg="#f3f0e8")
    result: dict[str, StoredConnection] = {}

    tk.Label(
        root,
        text="连接商家发布台",
        font=("Microsoft YaHei UI", 20, "bold"),
        bg="#f3f0e8",
        fg="#173f37",
    ).pack(anchor="w", padx=36, pady=(34, 8))
    tk.Label(
        root,
        text="在网页的“本地执行助手”面板生成配对码，然后填到这里。\n配对一次后会随 Windows 登录自动连接，不再需要输入密码。",
        justify="left",
        font=("Microsoft YaHei UI", 10),
        bg="#f3f0e8",
        fg="#53645f",
    ).pack(anchor="w", padx=36, pady=(0, 20))

    form = tk.Frame(root, bg="#f3f0e8")
    form.pack(fill="x", padx=36)
    tk.Label(form, text="发布台地址", bg="#f3f0e8", fg="#173f37").pack(anchor="w")
    server_entry = tk.Entry(form, font=("Consolas", 11), relief="solid", bd=1)
    server_entry.pack(fill="x", ipady=7, pady=(5, 14))
    server_entry.insert(0, initial_server or "https://")
    tk.Label(form, text="一次性配对码", bg="#f3f0e8", fg="#173f37").pack(anchor="w")
    code_entry = tk.Entry(form, font=("Consolas", 18, "bold"), relief="solid", bd=1)
    code_entry.pack(fill="x", ipady=7, pady=(5, 16))

    status = tk.StringVar(value="")
    tk.Label(
        form,
        textvariable=status,
        bg="#f3f0e8",
        fg="#b54835",
        wraplength=440,
        justify="left",
    ).pack(anchor="w")

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
        set_windows_autostart(True)
        messagebox.showinfo("配对成功", "本地执行助手已连接，以后只需打开发布台网页。")
        root.destroy()

    button = tk.Button(
        form,
        text="完成配对",
        command=finish_pairing,
        bg="#e56d3e",
        fg="white",
        activebackground="#c9522c",
        activeforeground="white",
        relief="flat",
        font=("Microsoft YaHei UI", 11, "bold"),
        cursor="hand2",
    )
    button.pack(fill="x", ipady=8, pady=(10, 0))
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()
    return result.get("connection")


def _tray_image():
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (64, 64), "#173f37")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((5, 5, 59, 59), radius=15, fill="#e56d3e")
    draw.text((18, 13), "M", fill="white", stroke_width=1, stroke_fill="white")
    return image


def _run_tray(
    application: LocalAgentApplication,
    connection: StoredConnection,
    store: AgentConnectionStore,
) -> None:
    try:
        import pystray
    except ImportError:
        _run_status_window(application, connection)
        return

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
        set_windows_autostart(False)
        quit_agent(icon)

    user_label = connection.user.get("display_name") or connection.user.get("username")
    menu = pystray.Menu(
        pystray.MenuItem("打开商家发布台", open_console, default=True),
        pystray.MenuItem(f"已连接：{user_label}", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("解除配对并退出", disconnect),
        pystray.MenuItem("退出助手", quit_agent),
    )
    icon = pystray.Icon(
        "MPAU-Agent",
        _tray_image(),
        "MPAU 本地执行助手：已连接",
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
    icon.run()


def _run_status_window(
    application: LocalAgentApplication, connection: StoredConnection
) -> None:
    import tkinter as tk

    root = tk.Tk()
    root.title("MPAU 本地执行助手")
    root.geometry("420x210")
    tk.Label(root, text="本地执行助手已连接", font=("Microsoft YaHei UI", 18, "bold")).pack(
        pady=(35, 12)
    )
    tk.Label(root, text=connection.server_url, font=("Consolas", 10)).pack()
    tk.Button(root, text="打开商家发布台", command=lambda: open_url(connection.server_url)).pack(
        pady=18
    )

    def close() -> None:
        application.stop()
        root.destroy()

    def watch_application() -> None:
        if application.authorization_failed:
            root.destroy()
            return
        root.after(500, watch_application)

    root.protocol("WM_DELETE_WINDOW", close)
    root.after(500, watch_application)
    root.mainloop()


def _connect_when_available(application: LocalAgentApplication) -> bool:
    """Keep an autostarted helper alive while Windows is waiting for the network."""
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
        _run_tray(application, connection, store)
        application.stop()
        worker.join(timeout=15)
        if not application.authorization_failed:
            return

        previous_server = connection.server_url
        store.clear()
        connection = _pairing_dialog(
            store, args.data_dir, initial_server=previous_server
        )


if __name__ == "__main__":
    run()
