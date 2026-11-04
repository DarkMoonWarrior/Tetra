"""启动命题智能体：python run.py，服务就绪后浏览器会自动打开 http://127.0.0.1:8765 。
平时用 tetra 命令启动更方便（见 mingti/launcher.py）。"""

from __future__ import annotations

import argparse
import socket
import threading
import time
import webbrowser

import uvicorn


def _when_ready(port: int, url: str, *, open_browser: bool, banner: bool) -> None:
    """等服务真的能连上了再打印地址、打开浏览器（第一次启动加载知识树要一两秒）。"""
    for _ in range(300):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    else:
        return
    if banner:
        print(
            "\n  △  Tetra 命题智能体\n"
            f"     已启动：{url}\n"
            f"     {'浏览器已经打开；' if open_browser else ''}关掉这个窗口或按 Ctrl+C 停止。\n",
            flush=True,
        )
    if open_browser:
        webbrowser.open(url)


def main() -> None:
    parser = argparse.ArgumentParser(description="命题智能体")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--quiet", action="store_true", help="不逐条打印请求日志，只显示启动信息和错误（tetra 命令用）")
    args = parser.parse_args()

    url = f"http://127.0.0.1:{args.port}"
    threading.Thread(
        target=_when_ready, args=(args.port, url), kwargs={"open_browser": not args.no_browser, "banner": args.quiet},
        daemon=True,
    ).start()
    uvicorn.run(
        "mingti.server:app",
        host="127.0.0.1",
        port=args.port,
        log_level="warning" if args.quiet else "info",
        access_log=not args.quiet,
    )


if __name__ == "__main__":
    main()
