"""tetra 命令：在任意终端输入 tetra，就启动命题智能体并打开浏览器。

安装一次（在项目目录里运行，装进系统的 Python，Scripts 目录在 PATH 里就能直接用）：
    python -m pip install --no-build-isolation -e .
以后：
    tetra                 启动并打开浏览器（已经在运行就只打开浏览器）
    tetra --port 9000     换一个端口
    tetra --no-browser    不自动打开浏览器

模型（接口地址、API Key、要用的型号）都在页面的「个人设置 → 模型服务」里接入和更换，不用改后台文件。

这个文件只用标准库：它跑在系统的 Python 里，负责第一次运行时在项目里建好 .venv、装好依赖，
然后用 .venv 里的 Python 启动 run.py。应用本身的依赖都装在 .venv 里，不动系统的 Python。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
VENV_PYTHON = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
REQUIREMENTS = ROOT / "requirements.txt"
STAMP = VENV / "tetra-requirements.sha1"  # 装依赖时 requirements.txt 的指纹，变了就重新装

# 默认镜像装不上时（镜像 403、系统代理挡住 pip 等），改用官方 PyPI 并绕开代理再试一次
_PYPI = ["-i", "https://pypi.org/simple"]
_NO_PROXY_ENV = {
    "HTTPS_PROXY": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9",
    "NO_PROXY": "pypi.org,pythonhosted.org,files.pythonhosted.org",
}


# 输出统一用 UTF-8：Windows 控制台本来就能显示中文，Git Bash 这类走管道的终端按 UTF-8 解码，
# 不统一的话那里的中文会变成乱码。子进程（pip、run.py）也一样
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")
CHILD_ENV = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def say(text: str = "") -> None:
    print(text, flush=True)


# ---------- 第一次运行：.venv 和依赖 ----------


def _requirements_hash() -> str:
    return hashlib.sha1(REQUIREMENTS.read_bytes()).hexdigest()


def ensure_venv() -> None:
    if not VENV_PYTHON.exists():
        say("第一次运行：在项目里新建 .venv …")
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True, env=CHILD_ENV)
    if STAMP.exists() and STAMP.read_text(encoding="utf-8").strip() == _requirements_hash():
        return
    say("安装依赖（只在第一次和依赖有变化时进行，需要一两分钟）…")
    command = [str(VENV_PYTHON), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(REQUIREMENTS)]
    if subprocess.run(command, env=CHILD_ENV).returncode != 0:
        say("默认的下载源没装上，改用官方 PyPI 再试一次 …")
        if subprocess.run(command + _PYPI, env={**CHILD_ENV, **_NO_PROXY_ENV}).returncode != 0:
            raise SystemExit("依赖没有装上。请检查网络后重新运行 tetra。")
    STAMP.write_text(_requirements_hash(), encoding="utf-8")


# ---------- 模型 ----------


def has_model() -> bool:
    """有没有接入过模型：界面里接入的（data/runtime/models.json），或者以前写在 .env 里、启动时会自动搬进来的。"""
    config = ROOT / "data" / "runtime" / "models.json"
    try:
        if any(s.get("api_key") and s.get("models") for s in json.loads(config.read_text(encoding="utf-8")).get("services", [])):
            return True
    except (OSError, ValueError, AttributeError):
        pass
    env = ROOT / ".env"
    if os.environ.get("DEEPSEEK_API_KEY", "").strip():
        return True
    return env.exists() and any(
        line.partition("=")[0].strip() == "DEEPSEEK_API_KEY" and line.partition("=")[2].strip()
        for line in env.read_text(encoding="utf-8").splitlines()
    )


# ---------- 端口与已在运行的服务 ----------


def tetra_running(port: int) -> bool:
    """这个端口上是不是已经有一个命题智能体在运行（不走系统代理）。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=1.5) as response:
            return "prompt_revision" in json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return False


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


# ---------- 入口 ----------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tetra", description="Tetra 命题智能体：启动服务并打开浏览器。")
    parser.add_argument("--port", type=int, default=8765, help="端口，默认 8765")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args(argv)

    if tetra_running(args.port):
        url = f"http://127.0.0.1:{args.port}"
        say(f"命题智能体已经在运行：{url}")
        if not args.no_browser:
            webbrowser.open(url)
        return 0

    ensure_venv()
    if not has_model():
        say("第一次使用：页面打开后，在「个人设置 → 模型服务」里接入一个模型服务，就能开始出题。")

    port = args.port
    while not port_free(port):
        if port >= args.port + 10:
            raise SystemExit(f"端口 {args.port}～{port} 都被占用了，用 tetra --port 换一个端口。")
        port += 1
    if port != args.port:
        say(f"端口 {args.port} 被别的程序占用，改用 {port}。")

    command = [str(VENV_PYTHON), "-X", "utf8", str(ROOT / "run.py"), "--port", str(port), "--quiet"]
    if args.no_browser:
        command.append("--no-browser")
    child = subprocess.Popen(command, cwd=ROOT, env=CHILD_ENV)
    while True:
        try:
            return child.wait()
        except KeyboardInterrupt:
            # Ctrl+C 也会发给服务进程，等它自己收尾退出
            continue


if __name__ == "__main__":
    sys.exit(main())
