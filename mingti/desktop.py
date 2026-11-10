"""Tetra 桌面程序的入口：双击 Tetra.exe 就能用（打包见 packaging/）。

- 在本机起服务（和 tetra 命令起的是同一个服务），再用系统自带的 Edge（没有就用 Chrome）以"应用"模式打开一个窗口：
  没有地址栏和标签页，用自己单独的浏览器资料目录，不碰你平时用的浏览器。
- 网页版和桌面版用同一份数据（config.RUNTIME_DIR）。程序开着时，用浏览器打开 http://127.0.0.1:8765
  （端口被别的程序占了就往后顺延）也能用，那就是网页版：没有设置里的"桌面版"这一页。
- 同一时间只开一个桌面版：再双击一次，只是再开一个窗口。从源码用 tetra 命令起着的服务不借用（它没有桌面版的功能）：
  8765 被它占着时，桌面版换下一个空端口起自己的服务，数据还是同一份。
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
# 开窗口以后这么久里看不到窗口，也先别当它关了：浏览器刚更新过时，开窗口的进程会把窗口交给另一个进程、自己马上退出，
# 那个进程要过一会儿才占上窗口的资料目录（见 _window_open）
HANDOFF_SECONDS = 15
# 页面的标题（中文界面、英文界面，英文见 web/js/i18n-en.js）；应用模式的窗口标题就是它
WINDOW_TITLES = ("Tetra 命题智能体", "Tetra · Math Question Agent")
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
    """开机启动时把窗口最小化：等 Tetra 的窗口出来，再把它缩到任务栏。认窗口：属于我们开的浏览器进程，
    或者标题正好是 Tetra 的页面标题（浏览器把窗口交给了别的进程时；普通浏览器窗口的标题后面还带着浏览器名，不会认错）。"""
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def collect(hwnd, _param):
        if not user32.IsWindowVisible(hwnd) or user32.GetWindowTextLengthW(hwnd) == 0:
            return True
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        title = ctypes.create_unicode_buffer(200)
        user32.GetWindowTextW(hwnd, title, 200)
        if owner.value == pid or title.value in WINDOW_TITLES:
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


def _pick_port() -> int:
    """桌面版自己的服务用哪个端口：8765 被占了就往后找一个空的。
    占着的哪怕是 Tetra 也不借用：比如从源码用 tetra 命令起的服务，没有桌面版的功能（设置里的"桌面版"、
    导出存到文件夹……），数据也是项目里的另一份。同一个桌面版再打开一次，在前面的锁那里就已经接上了。"""
    for port in range(PORT, PORT + 30):
        if _port_free(port):
            return port
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
    """以"应用"模式开一个 Tetra 窗口，用单独的浏览器资料目录（home/window）：关窗口时浏览器就退出，
    据此判断窗口关了没有（_window_open）。"""
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


def _close_window(window: subprocess.Popen) -> None:
    """关掉我们开的窗口：先像点 × 那样请它关（浏览器正常退出，下次打开不会提示"意外关闭、要不要恢复"），
    5 秒还没关再强行结束。"""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def ask_close(hwnd, _param):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == window.pid and user32.IsWindowVisible(hwnd):
                user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
            return True

        user32.EnumWindows(ask_close, 0)
    else:
        window.terminate()  # Chrome 收到 SIGTERM 也是正常退出
    try:
        window.wait(timeout=5)
    except subprocess.TimeoutExpired:
        window.kill()


def _window_open(profile: Path) -> bool:
    """窗口用的浏览器还开着没有。Edge、Chrome 开着一份资料目录时一直占着里面的锁文件，退出时文件自己就没了
    （Windows 上是 lockfile，macOS、Linux 上是指向"主机名-进程号"的 SingletonLock）。
    不只看我们开的那个进程：浏览器刚更新过时，它会把窗口交给另一个进程、自己马上退出。"""
    if os.name == "nt":
        try:
            os.close(os.open(profile / "lockfile", os.O_WRONLY))
        except FileNotFoundError:
            return False
        except OSError:
            return True  # 打不开：浏览器占着
        return False  # 打得开：上次留下的空文件，浏览器没开着
    try:
        target = os.readlink(profile / "SingletonLock")
        os.kill(int(target.rsplit("-", 1)[1]), 0)
    except PermissionError:
        return True  # 进程在，只是不让碰
    except (OSError, ValueError, IndexError):
        return False
    return True


def _fallback_window(url: str) -> None:
    """没有 Edge、Chrome：用默认浏览器打开（地址带 ?window=desktop，页面照样当桌面版的窗口），
    再显示一个小窗口（打开 / 退出），关掉它就退出。"""
    page = f"{url}/?window=desktop"
    webbrowser.open(page)
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

    tk.Button(row, text="打开 Tetra", width=12, command=lambda: webbrowser.open(page)).pack(side="left", padx=6)
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


def _relaunch(reuse_window_port: int | None = None) -> None:
    """重启：起一个新的 Tetra（等这个退出、放开锁以后它再起服务、开窗口）。
    reuse_window_port：原来的窗口不归我们开的进程管（浏览器把它交给了别的进程），或者窗口已经关了、只剩浏览器里的页面时，
    新的 Tetra 就在这个端口起服务、不再开窗口，页面自己刷新、接上新的服务。"""
    command = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "mingti.desktop"]
    if reuse_window_port:
        command.append(f"--reuse-window={reuse_window_port}")
    flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(command, creationflags=flags, close_fds=True)


def _argument(name: str) -> str | None:
    for value in sys.argv[1:]:
        if value == name:
            return ""
        if value.startswith(f"{name}="):
            return value.split("=", 1)[1]
    return None


def main() -> None:
    os.environ["TETRA_DESKTOP"] = "1"  # 让服务知道是桌面程序：它的窗口里，设置里有"桌面版"这一页
    autostart = _argument("--autostart") is not None
    reuse = _argument("--reuse-window")  # 重启时原来的窗口还开着：在它的端口起服务，不再开新窗口
    reuse_port = int(reuse) if reuse and reuse.isdigit() else None
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
                webbrowser.open(f"{url}/?window=desktop")
            return
        port = None
        if reuse_port:
            # 原来的服务刚退出，端口可能要过一会儿才放开
            for _ in range(40):
                if _port_free(reuse_port):
                    port = reuse_port
                    break
                time.sleep(0.25)
        if port is None:
            reuse_port = None
            port = _pick_port()
        url = f"http://127.0.0.1:{port}"
        if port != PORT and not reuse_port:
            log.info("端口 %d 被别的程序（也可能是从源码起的 Tetra）占着，桌面版用 %d", PORT, port)
        # 设置里换过数据文件夹：服务打开数据库之前，先把数据搬过去
        from mingti import config, desktop_os

        moved = desktop_os.apply_pending_move()
        if moved is not None:
            config.RUNTIME_DIR = moved
        server, thread = _start_server(port)
        instance.publish(port)
        log.info("Tetra 已启动：%s（数据在 %s）", url, config.RUNTIME_DIR)
        window = None if reuse_port else open_window(url, home)
        if window is not None and autostart and desktop_os.load_prefs().get("autostart_minimized", True):
            threading.Thread(target=_minimize, args=(window.pid,), name="tetra-minimize", daemon=True).start()
        if not autostart and not reuse_port and not desktop_os.installed():
            threading.Thread(target=_offer_shortcut, args=(home,), name="tetra-shortcut", daemon=True).start()
        from mingti import server as srv

        if window is None and not reuse_port:
            _fallback_window(url)
        else:
            # 一、等窗口关掉：我们开的浏览器进程还在，或者浏览器还占着窗口的资料目录，窗口就开着（_window_open）
            profile = home / "window"
            opened = time.monotonic()
            keep_window = reuse_port is not None  # 重启时不关窗口，让页面自己接上新的服务
            handed_off = False
            while not srv.RESTART_REQUESTED:
                if window is not None and window.poll() is None:
                    pass
                elif _window_open(profile):
                    keep_window = True  # 窗口不归我们开的进程管：重启时关不掉它，只能沿用
                    if not handed_off:
                        handed_off = True
                        log.info("浏览器把窗口交给了别的进程，改看窗口的资料目录")
                elif time.monotonic() - opened >= HANDOFF_SECONDS:
                    break
                time.sleep(0.5)
            if not srv.RESTART_REQUESTED:
                for page in [key for key in srv.PAGES if key.startswith("desktop-window")]:
                    srv.PAGES.pop(page, None)  # 窗口关了：它的页面不算了
                keep_window = True  # 窗口已经关了：重启时也不再开窗口，浏览器里开着的页面自己接上
                # 二、还有题在出，或者浏览器里还开着 Tetra 的页面，就接着在后台跑；都没有了才退出
                closed = time.monotonic()
                empty_since = None
                while thread.is_alive() and not srv.RESTART_REQUESTED:
                    if _open_pages() or (_busy() and time.monotonic() - closed < BUSY_LIMIT):
                        empty_since = None
                    elif empty_since is None:
                        empty_since = time.monotonic()
                    elif time.monotonic() - empty_since > 5:  # 连着几秒都没有：页面刷新时会有一小会儿不在名单里
                        break
                    time.sleep(1)
            restart = srv.RESTART_REQUESTED
            if restart and not keep_window:
                _close_window(window)  # 新的 Tetra 再开一个
            reuse_port = port if restart and keep_window else None
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
        _relaunch(reuse_port)


if __name__ == "__main__":
    main()
