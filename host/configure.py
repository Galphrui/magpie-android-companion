#!/usr/bin/env python3
"""Desktop controls for the Magpie Bluetooth bridge."""

import asyncio
import errno
import http.client
from pathlib import Path
import queue
import sys
import threading
import time

from bridge import browser_config_candidates, decode_json, read_auto_key

CONNECTED = "蓝牙已连接"
BROWSER_HELP = "请先在电脑上打开 Magpie，在「设置 → 网络与共享 → 浏览器访问」选择「本机」，然后重试。"


class SetupError(Exception):
    """An error safe to display without printing configuration data."""


def find_browser_config(selected):
    candidates = (Path(selected).expanduser(),) if selected else browser_config_candidates()
    for path in candidates:
        try:
            key, port = read_auto_key(path)
        except ValueError:
            continue
        return path, key, port
    raise SetupError(BROWSER_HELP)


def get_gateway_port(folder):
    try:
        with (folder / "gateway.json").open("rb") as record:
            raw = record.read(4097)
        if len(raw) <= 4096:
            state = decode_json(raw)
            if isinstance(state, dict) and isinstance(state.get("addr"), str):
                host, separator, value = state["addr"].rpartition(":")
                if separator and host and value.isascii() and value.isdecimal():
                    port = int(value)
                    if 1 <= port <= 65535:
                        return port
    except (OSError, ValueError, RecursionError):
        pass
    return 3425


def check_browser_access(key, port):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request(
            "GET", "/api/settings", headers={"Cookie": f"magpie_web_{port}={key}"}
        )
        with connection.getresponse() as response:
            if response.status != 200 or response.headers.get_content_type() != "application/json":
                raise SetupError(BROWSER_HELP)
    except (OSError, http.client.HTTPException):
        raise SetupError(BROWSER_HELP) from None
    finally:
        connection.close()


async def run_session(session, stop, commands=None):
    task = asyncio.create_task(session.run())

    async def watch_commands():
        while not stop.is_set() and not task.done():
            if commands is None:
                await asyncio.sleep(0.15)
                continue
            try:
                command = commands.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.1)
                continue
            if command == "refresh":
                # rotate_pairing is called on the same event loop as run().
                session.rotate_pairing()

    watcher = asyncio.create_task(watch_commands())
    try:
        while not stop.is_set() and not task.done():
            await asyncio.wait({task}, timeout=0.15)
        if task.done():
            await task
    finally:
        watcher.cancel()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, watcher, return_exceptions=True)


def _put_event(events, generation, event, value):
    events.put((generation, event, value))


def run_worker(selected, stop, events, generation=0, commands=None):
    try:
        from bluetooth_transport import DesktopSession

        config, key, port = find_browser_config(selected)
        check_browser_access(key, port)
        if stop.is_set():
            return
        with DesktopSession(
            port, key, gateway_port=get_gateway_port(config.parent),
            status=lambda message: _put_event(events, generation, "status", message),
            pairing=lambda code: _put_event(events, generation, "qr", code),
        ) as session:
            asyncio.run(run_session(session, stop, commands))
    except SetupError as error:
        _put_event(events, generation, "error", str(error))
    except ImportError:
        _put_event(events, generation, "error", "蓝牙组件尚未安装。请使用项目中的桌面启动脚本重新启动。")
    except OSError as error:
        if error.errno == errno.EADDRINUSE or getattr(error, "winerror", None) == 10048:
            _put_event(events, generation, "error", "蓝牙桥接端口被占用。请关闭其他 Magpie 蓝牙配置窗口后重试。")
        else:
            _put_event(events, generation, "error", "无法启动蓝牙桥接。请检查电脑蓝牙开关及系统蓝牙权限。")
    except Exception:
        _put_event(events, generation, "error", "蓝牙连接失败。请确认电脑支持低功耗蓝牙、蓝牙已开启，并允许此程序使用蓝牙。")
    finally:
        _put_event(events, generation, "finished", None)


class ConfigWindow:
    def __init__(self, window, tk, ttk, qrcode):
        self.window = window
        self.events = queue.SimpleQueue()
        self.commands = queue.SimpleQueue()
        self.stop_event = None
        self.closing = False
        self.failed = False
        self.manual_stop = False
        self.qrcode = qrcode
        self.tk = tk
        self.qr_matrix = None
        self.connected = False
        self.worker_generation = 0

        window.title("Magpie 蓝牙配置")
        window.minsize(420, 620)
        frame = ttk.Frame(window, padding=24)
        frame.pack(expand=True, fill=tk.BOTH)
        ttk.Label(frame, text="Magpie", font=("TkDefaultFont", 23, "bold")).pack(anchor=tk.W)
        ttk.Label(frame, text="蓝牙配对", font=("TkDefaultFont", 15)).pack(anchor=tk.W)
        instructions = "打开 Android 伴侣端的扫码功能，扫描此二维码。"
        if sys.platform == "darwin":
            instructions += "首次使用时，请在系统提示中允许蓝牙访问。"
        ttk.Label(frame, text=instructions, wraplength=372).pack(anchor=tk.W, pady=(8, 14))

        self.canvas = tk.Canvas(frame, width=280, height=280, bg="white", borderwidth=0,
                                highlightthickness=1, highlightbackground="#cccccc")
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<Configure>", self.redraw)
        self.status = tk.StringVar(value="准备配对")
        ttk.Label(frame, textvariable=self.status, wraplength=372).pack(anchor=tk.W, pady=(12, 4))

        buttons = ttk.Frame(frame)
        buttons.pack(fill=tk.X, pady=(6, 0))
        self.refresh_button = ttk.Button(buttons, text="刷新二维码", command=self.refresh)
        self.refresh_button.pack(side=tk.LEFT, padx=(0, 6))
        self.disconnect_button = ttk.Button(buttons, text="断开", command=self.disconnect)
        self.disconnect_button.pack(side=tk.LEFT)

        self.progress = ttk.Progressbar(frame, mode="indeterminate")
        self.progress.pack(fill=tk.X, pady=(6, 6))
        log_frame = ttk.LabelFrame(frame, text="加载日志")
        log_frame.pack(fill=tk.X, pady=(0, 0))
        self.log_text = tk.Text(log_frame, height=6, wrap="word", state="disabled",
                                font=("TkDefaultFont", 10))
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        window.protocol("WM_DELETE_WINDOW", self.close)
        window.after(80, self.poll)
        window.after(300, self.redraw)
        window.after_idle(self.start)

    def start(self):
        if self.stop_event is not None or self.closing:
            return
        self.manual_stop = False
        self.worker_generation += 1
        generation = self.worker_generation
        self.stop_event = threading.Event()
        self.failed = False
        self.status.set("正在检查 Magpie 和准备蓝牙配对…")
        self._append_log(self.status.get())
        self.progress.start(12)
        self.disconnect_button.configure(text="断开")
        threading.Thread(target=run_worker,
                         args=("", self.stop_event, self.events, generation, self.commands),
                         name="magpie-bluetooth", daemon=True).start()

    def stop(self):
        if self.stop_event is not None:
            self.stop_event.set()
            self.status.set("正在停止蓝牙连接…")
            self._append_log(self.status.get())
            self.progress.stop()
            self.qr_matrix = None
            self.connected = False
            self.redraw()

    def refresh(self):
        if self.stop_event is None or self.stop_event.is_set():
            self.start()
            return
        self.commands.put("refresh")
        self.status.set("正在生成新二维码…")
        self._append_log(self.status.get())

    def disconnect(self):
        if self.stop_event is not None and not self.stop_event.is_set():
            self.manual_stop = True
            self.stop()
            self.status.set("蓝牙连接已断开")
            self._append_log(self.status.get())
            self.disconnect_button.configure(text="重连")
        else:
            self.start()

    def close(self):
        if self.stop_event is not None:
            self.closing = True
            self.stop()
        else:
            self.window.destroy()

    def redraw(self, _event=None):
        self.canvas.delete("all")
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        if width <= 1 or height <= 1:
            width, height = 280, 280
        if self.qr_matrix is None or self.connected:
            placeholder = "已配对" if self.connected else "正在生成二维码…" if self.stop_event is not None else "已断开"
            self.canvas.create_text(width // 2, height // 2, text=placeholder, fill="#767676",
                                    font=("TkDefaultFont", 16))
            return
        matrix = self.qr_matrix
        scale = max(1, min(width, height) // len(matrix))
        left, top = (width - len(matrix) * scale) // 2, (height - len(matrix) * scale) // 2
        for row, modules in enumerate(matrix):
            for column, dark in enumerate(modules):
                if dark:
                    x, y = left + column * scale, top + row * scale
                    self.canvas.create_rectangle(x, y, x + scale, y + scale, fill="black", width=0)

    def poll(self):
        while not self.events.empty():
            message = self.events.get_nowait()
            if len(message) == 3:
                generation, event, value = message
            else:
                generation, event, value = self.worker_generation, *message
            if generation != self.worker_generation:
                continue
            if event == "finished":
                self.stop_event = None
                self.qr_matrix = None
                self.connected = False
                self.progress.stop()
                if self.closing:
                    self.window.destroy()
                    return
                self.redraw()
                if self.manual_stop:
                    self.status.set("蓝牙连接已断开")
                    self.disconnect_button.configure(text="重连")
                elif not self.failed:
                    self.status.set("蓝牙监听已停止，正在重新启动…")
                    self._append_log(self.status.get())
                    self.window.after(3000, self.start)
                    continue
                self._append_log(self.status.get())
            elif self.stop_event is not None and not self.stop_event.is_set():
                if event == "qr":
                    if not value:
                        self.qr_matrix = None
                        self.redraw()
                        continue
                    qr = self.qrcode.QRCode(error_correction=self.qrcode.constants.ERROR_CORRECT_M, border=4)
                    qr.add_data(value)
                    self.qr_matrix = qr.get_matrix()
                    self.redraw()
                else:
                    if self.connected != (value == CONNECTED):
                        self.connected = value == CONNECTED
                        self.redraw()
                    self.failed = event == "error"
                    self.status.set(value)
                    self._append_log(value)
                    if self.connected or event == "error":
                        self.progress.stop()
                    else:
                        self.progress.start(12)
        self.window.after(80, self.poll)

    def _append_log(self, value):
        if not value:
            return
        self.log_text.configure(state="normal")
        self.log_text.insert(self.tk.END, time.strftime("%H:%M:%S") + "  " + value + "\n")
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > 240:
            self.log_text.delete("1.0", f"{lines - 200}.0")
        self.log_text.see(self.tk.END)
        self.log_text.configure(state="disabled")


def main():
    if sys.platform == "win32" and sys.getwindowsversion().build < 16299:
        print("Magpie 蓝牙配置需要 Windows 10 版本 1709（内部版本 16299）或更新版本。", file=sys.stderr)
        return 1
    try:
        import tkinter as tk
        from tkinter import ttk
        import qrcode
    except ImportError:
        print("请使用 Python 3.10 或更新版本并安装 Tk，然后通过桌面启动脚本运行。", file=sys.stderr)
        return 1
    window = tk.Tk()
    ConfigWindow(window, tk, ttk, qrcode)
    window.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
