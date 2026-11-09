"""Tetra 桌面程序的入口：双击 Tetra.exe 就能用（打包见 packaging/）。

- 在本机起服务（和 tetra 命令起的是同一个服务），再用系统自带的 Edge（没有就用 Chrome）以"应用"模式打开一个窗口：
  没有地址栏和标签页，用自己单独的浏览器资料目录，不碰你平时用的浏览器。
- 网页端照常能用：程序开着时，用任何浏览器打开 http://127.0.0.1:8765 （端口被别的程序占了就往后顺延）就是同一个应用；
  左下角菜单里也有"在浏览器中打开"。
- 同一时间只开一个服务：再双击一次，或者已经用 tetra 命令起着服务，就只是再开一个窗口。
- 关掉窗口后，还有题在出，或者还有浏览器页面开着，服务就接着在后台跑，都结束了才退出（题不会出到一半丢掉）。
- 电脑上没有 Edge、Chrome 时，用默认浏览器打开，另外显示一个小窗口，关掉它就退出。

会变的数据（题库、设置、模型服务）、日志、窗口用的浏览器资料都在 config.APP_HOME 下（Windows：%LOCALAPPDATA%\\Tetra）。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

PORT = int(os.environ.get("TETRA_PORT") or 8765)  # 想换端口可以设环境变量 TETRA_PORT
# 窗口关了以后，这么多秒没有页面来问过服务，又没有题在出，就退出（页面每分钟会来问一次）
IDLE_EXIT = float(os.environ.get("TETRA_IDLE_EXIT") or 150)
BUSY_LIMIT = 30 * 60  # 窗口关了以后，最多再等这么久让还在出的题出完
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 本机地址不走系统代理

log = logging.getLogger("mingti.desktop")


# ---------- 准备：数据目录、日志、只开一个 ----------


def _home() -> Path:
    from mingti.desktop_os import home

    return home()


def _hold_mutex() -> None:
    """Windows 上开一个有名字的互斥量：安装、卸载程序用它看 Tetra 是不是开着（tetra.iss 的 AppMutex）。"""
    if os.name != "nt":
        return
    try:
        import ctypes

        _hold_mutex.handle = ctypes.windll.kernel32.CreateMutexW(None, False, "TetraDesktopApp")
    except Exception:  # noqa: BLE001 - 没有也不影响使用
        pass


def _minimize(pid: int, timeout: float = 20.0) -> None:
    """开机启动时把窗口最小化：等这个浏览器进程的窗口出来，再把它缩到任务栏。"""
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def collect(hwnd, _param):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
            found.append(hwnd)
        return True

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found.clear()
        user32.EnumWindows(collect, 0)
        if found:
            for hwnd in found:
                user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
            return
        time.sleep(0.2)


def _prepare_logging(home: Path) -> None:
    """打包成没有控制台的程序时 sys.stdout / sys.stderr 是 None：输出改写进日志文件，不然一写日志就出错。"""
    logs = home / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / "tetra.log"
    try:
        if path.exists() and path.stat().st_size > 5_000_000:
            path.replace(logs / "tetra.old.log")
    except OSError:
        pass
    if sys.stdout is None or sys.stderr is None or getattr(sys, "frozen", False):
        stream = open(path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 - 进程活多久就开多久
        sys.stdout = sys.stderr = stream
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class _SingleInstance:
    """用一个加了锁的文件保证同一时间只有一个服务；拿到锁的进程把端口写进 port 文件，后开的照着去开窗口。"""

    def __init__(self, home: Path) -> None:
        self.lock_path = home / "tetra.lock"
        self.port_path = home / "tetra.port"
        self.handle = None

    def acquire(self) -> bool:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.lock_path, "a+b")  # noqa: SIM115 - 锁要一直拿着
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        return True

    def publish(self, port: int) -> None:
        self.port_path.write_text(str(port), encoding="utf-8")

    def running_port(self, wait: float = 25.0) -> int | None:
        """另一个 Tetra 正在启动或已经在跑：等它把端口写出来、服务能连上。"""
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            try:
                port = int(self.port_path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                port = None
            if port and _is_tetra(port):
                return port
            time.sleep(0.3)
        return None


# ---------- 服务 ----------


def _is_tetra(port: int) -> bool:
    try:
        with _OPENER.open(f"http://127.0.0.1:{port}/api/health", timeout=1.5) as response:
            return "ui_version" in json.loads(response.read())
    except Exception:  # noqa: BLE001 - 连不上、不是 Tetra 都算没有
        return False


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _pick_port() -> tuple[int, bool]:
    """(端口, 那里是不是已经有 Tetra 在跑)：8765 被别的程序占了就往后找一个空的。"""
    for port in range(PORT, PORT + 30):
        if _is_tetra(port):
            return port, True
        if _port_free(port):
            return port, False
    raise RuntimeError(f"端口 {PORT}～{PORT + 29} 都被占用了，没法启动服务")


def _start_server(port: int):
    """在后台线程里起服务；返回 (uvicorn.Server, 线程)。"""
    import uvicorn

    from mingti.server import app

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False, lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="tetra-server", daemon=True)
    thread.start()
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and thread.is_alive():
        if _is_tetra(port):
            return server, thread
        time.sleep(0.1)
    raise RuntimeError("服务没能启动，详情写在日志里")


def _busy() -> bool:
    """还有题在出（单道、一批、组卷、存题）。"""
    from mingti import server as srv

    with srv.manager._lock:
        tasks = [*srv.manager._jobs.values(), *srv.manager._batches.values()]
    with srv.assemblies._lock:
        tasks += list(srv.assemblies._assemblies.values())
    with srv.importer._lock:
        tasks += list(srv.importer._jobs.values())
    return any(task.finished_at is None for task in tasks)


def _open_pages() -> int:
    """还开着的 Tetra 页面有几个（浏览器里的标签页；IDLE_EXIT 秒内来问过服务、又没说自己关了的）。"""
    from mingti import server as srv

    now = time.monotonic()
    return sum(1 for seen in list(srv.PAGES.values()) if now - seen < IDLE_EXIT)


# ---------- 窗口 ----------


def find_browser() -> str | None:
    """Edge 或 Chrome（以"应用"模式开窗口用）。"""
    names = ["msedge", "chrome", "google-chrome", "chromium", "chromium-browser"]
    candidates = [os.environ.get("TETRA_BROWSER")]
    if os.name == "nt":
        for base in filter(None, (os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES"), os.environ.get("LOCALAPPDATA"))):
            candidates += [
                Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe",
            ]
    elif sys.platform == "darwin":
        candidates += [
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ]
    candidates += [shutil.which(name) for name in names]
    return next((str(path) for path in candidates if path and Path(path).exists()), None)


def open_window(url: str, home: Path) -> subprocess.Popen | None:
    """以"应用"模式开一个 Tetra 窗口；用单独的浏览器资料目录，所以关窗口时这个进程会结束（用来判断窗口关了没有）。"""
    browser = find_browser()
    if not browser:
        return None
    profile = home / "window"
    profile.mkdir(parents=True, exist_ok=True)
    command = [
        browser,
        f"--app={url}/?window=desktop",  # 页面看到这个就知道自己是桌面版的窗口（关窗口时由这里替它说"关了"）
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--disable-features=Translate,msEdgeSidebarV2,msUndersideButton,msHubApps,EdgeCollections",
        "--window-size=1440,920",
    ]
    try:
        return subprocess.Popen(command)
    except OSError:
        log.exception("没能打开 %s", browser)
        return None


def _fallback_window(url: str) -> None:
    """没有 Edge、Chrome：用默认浏览器打开，再显示一个小窗口（打开 / 退出），关掉它就退出。"""
    webbrowser.open(url)
    try:
        import tkinter as tk
        from tkinter import messagebox
    except ImportError:
        while True:  # 连 tkinter 都没有：一直开着，直到进程被关掉
            time.sleep(3600)
    root = tk.Tk()
    root.title("Tetra")
    root.geometry("360x150")
    root.resizable(False, False)
    tk.Label(root, text="Tetra 正在运行", font=("Microsoft YaHei UI", 13, "bold")).pack(pady=(18, 4))
    tk.Label(root, text=url, fg="#555").pack()
    row = tk.Frame(root)
    row.pack(pady=14)

    def quit_app() -> None:
        if _busy() and not messagebox.askokcancel("Tetra", "还有题在出，退出会中断。确定退出？"):
            return
        root.destroy()

    tk.Button(row, text="在浏览器中打开", width=14, command=lambda: webbrowser.open(url)).pack(side="left", padx=6)
    tk.Button(row, text="退出 Tetra", width=10, command=quit_app).pack(side="left", padx=6)
    root.protocol("WM_DELETE_WINDOW", quit_app)
    root.mainloop()


def _offer_shortcut(home: Path) -> None:
    """桌面程序第一次打开时问一次：要不要在桌面（和开始菜单）放一个快捷方式。只问一次，不管答什么。"""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    marker = home / "shortcut-asked"
    if marker.exists():
        return
    marker.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
    try:
        import ctypes

        answer = ctypes.windll.user32.MessageBoxW(
            None, "要在桌面上放一个 Tetra 的快捷方式吗？\n以后双击它就能打开，开始菜单里也会有。", "Tetra", 0x4 | 0x20 | 0x40000,
        )  # 是 / 否、问号图标、放在最前面
        if answer != 6:  # 6 = 是
            return
        exe = sys.executable.replace("'", "''")
        folder = str(Path(sys.executable).parent).replace("'", "''")
        script = (
            "$s = New-Object -ComObject WScript.Shell; "
            "$places = @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs')); "
            "foreach ($p in $places) { $l = $s.CreateShortcut((Join-Path $p 'Tetra.lnk')); "
            f"$l.TargetPath = '{exe}'; $l.WorkingDirectory = '{folder}'; $l.IconLocation = '{exe},0'; "
            "$l.Description = 'Tetra 命题智能体'; $l.Save() }"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            creationflags=0x08000000, timeout=30, check=False,  # 不弹出黑窗口
        )
        log.info("建好了桌面和开始菜单的快捷方式")
    except Exception:  # noqa: BLE001 - 建不成快捷方式不影响使用
        log.exception("没能建快捷方式")


def _alert(text: str) -> None:
    log.error(text)
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, text, "Tetra", 0x10)
            return
        except Exception:  # noqa: BLE001 - 弹不出对话框就只写日志
            pass
    print(text, file=sys.stderr)


# ---------- 主流程 ----------


def _relaunch() -> None:
    """重启：起一个新的 Tetra（等这个退出、放开锁以后它再起服务、开窗口）。"""
    command = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "mingti.desktop"]
    flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(command, creationflags=flags, close_fds=True)


def main() -> None:
    os.environ["TETRA_DESKTOP"] = "1"  # 让服务知道是桌面程序：菜单里显示"在浏览器中打开"，设置里有"桌面版"
    autostart = "--autostart" in sys.argv[1:]
    home = _home()
    home.mkdir(parents=True, exist_ok=True)
    _prepare_logging(home)
    _hold_mutex()
    instance = _SingleInstance(home)
    restart = False
    try:
        if not instance.acquire():
            # 已经有一个 Tetra 在跑（或正在启动、正在重启）：再开一个窗口给它就行
            port = instance.running_port()
            if port is None:
                _alert("Tetra 好像已经在运行，但连不上。可以在任务管理器里结束 Tetra 再打开。")
                return
            url = f"http://127.0.0.1:{port}"
            if open_window(url, home) is None:
                webbrowser.open(url)
            return
        port, existing = _pick_port()
        url = f"http://127.0.0.1:{port}"
        if existing:
            # 用 tetra 命令起着的服务：只开窗口，服务由它自己管
            log.info("用已经在运行的服务 %s", url)
            if open_window(url, home) is None:
                webbrowser.open(url)
            return
        # 设置里换过数据文件夹：服务打开数据库之前，先把数据搬过去
        from mingti import config, desktop_os

        moved = desktop_os.apply_pending_move()
        if moved is not None:
            config.RUNTIME_DIR = moved
        server, thread = _start_server(port)
        instance.publish(port)
        log.info("Tetra 已启动：%s（数据在 %s）", url, config.RUNTIME_DIR)
        window = open_window(url, home)
        if window is not None and autostart and desktop_os.load_prefs().get("autostart_minimized", True):
            threading.Thread(target=_minimize, args=(window.pid,), name="tetra-minimize", daemon=True).start()
        if not autostart and not desktop_os.installed():
            threading.Thread(target=_offer_shortcut, args=(home,), name="tetra-shortcut", daemon=True).start()
        from mingti import server as srv

        if window is None:
            _fallback_window(url)
        else:
            while window.poll() is None:
                if srv.RESTART_REQUESTED:
                    restart = True
                    window.terminate()
                    break
                time.sleep(0.5)
            # 窗口关了：桌面版窗口里的页面不算了；还有题在出、或者浏览器里还开着 Tetra 的页面，就接着跑
            srv.PAGES.pop("desktop-window", None)
            closed = time.monotonic()
            while thread.is_alive() and not restart:
                busy = _busy()
                if busy and time.monotonic() - closed < BUSY_LIMIT:
                    time.sleep(2)
                    continue
                if not busy and _open_pages():
                    time.sleep(2)
                    continue
                break
        server.should_exit = True
        thread.join(timeout=10)
        log.info("Tetra 已退出" + ("，马上重新打开" if restart else ""))
    except Exception as exc:  # noqa: BLE001 - 出错要让用户看到
        log.exception("Tetra 启动失败")
        _alert(f"Tetra 没能启动：{exc}\n\n日志在 {home / 'logs' / 'tetra.log'}")
    finally:
        if instance.handle is not None:
            instance.handle.close()  # 放开锁，重启时新的 Tetra 才能起服务
    if restart:
        _relaunch()


if __name__ == "__main__":
    main()
