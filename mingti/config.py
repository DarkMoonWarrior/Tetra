"""运行配置：优先读环境变量，其次读项目根目录的 .env。

程序自带的文件（界面、知识树）：从源码运行（tetra 命令、run.py）时在项目目录里；桌面程序（PyInstaller 打包，见 packaging/）
在程序解包出来的目录里（sys._MEIPASS）。

会变的数据（题库、试卷、对话、设置、模型服务）两种方式共用一份，放在用户目录：Windows 是 %LOCALAPPDATA%\\Tetra\\data，
macOS 是 ~/Library/Application Support/Tetra/data。在桌面版设置里换过数据文件夹，两种方式都跟着换。
以前从源码运行时数据在项目的 data/runtime 里，第一次用新版本启动时会并进来（见 datafolder.py）。
设了环境变量 TETRA_HOME 时，两种方式都把会变的数据放到它下面（测试、或者想把数据放在别处时用）。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))  # 是不是打包好的桌面程序
ROOT = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) if FROZEN else Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"


def _app_home() -> Path:
    """Tetra 在用户目录下的文件夹：数据、桌面版的偏好和日志都在这里。从源码运行和桌面程序是同一个。"""
    custom = os.environ.get("TETRA_HOME")
    if custom:
        return Path(custom).expanduser().resolve()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "Tetra"


def _runtime_dir(home: Path) -> Path:
    """数据文件夹：默认是 <APP_HOME>/data；在桌面版设置里换过位置就用换过的
    （记在 <APP_HOME>/desktop.json 的 data_dir，见 desktop_os.py）。"""
    try:
        import json

        custom = json.loads((home / "desktop.json").read_text(encoding="utf-8")).get("data_dir")
    except (OSError, ValueError, AttributeError):
        custom = None
    return Path(custom) if custom else home / "data"


APP_HOME = _app_home()
CUSTOM_HOME = bool(os.environ.get("TETRA_HOME"))
RUNTIME_DIR = _runtime_dir(APP_HOME)
# 以前从源码运行时的数据文件夹（项目里的 data/runtime）：只用来把旧数据并进 RUNTIME_DIR
LEGACY_RUNTIME_DIR = DATA_DIR / "runtime"
CURRICULUM_CSV = DATA_DIR / "curriculum" / "pep-a-full-knowledge-tree-v1.csv"
# 教材版本：编号 → (界面上的名字, 知识点目录)。B 版目录由 tools/build_pep_b_tree.py 从 A 版知识点重新编排生成
CURRICULA = {
    "pep_a": ("人教 A 版（2019 年版）", CURRICULUM_CSV),
    "pep_b": ("人教 B 版（2019 年版）", DATA_DIR / "curriculum" / "pep-b-full-knowledge-tree-v1.csv"),
}
BLUEPRINT_JSON = DATA_DIR / "blueprint_specs.json"
WEB_DIR = ROOT / "web"


def _read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


@dataclass(frozen=True)
class Settings:
    deepseek_api_key: str
    deepseek_model: str
    deepseek_base_url: str
    deepseek_timeout_seconds: int
    max_parallel_jobs: int
    # 选配：一个兼容 OpenAI 接口的其他厂商（通义千问、Kimi、智谱等）
    compat_name: str
    compat_base_url: str
    compat_api_key: str
    compat_models: tuple[str, ...]
    # 整个服务同时在出的题最多几道（几个对话各自出题、批量出题、组卷现场出题加在一起）；
    # 再多的排队等。max_parallel_jobs 管的是一批题、一份试卷里同时出几道
    max_running_jobs: int = 8


def load_settings() -> Settings:
    file_values = _read_dotenv(ROOT / ".env")

    def get(name: str, default: str = "") -> str:
        return os.environ.get(name) or file_values.get(name) or default

    return Settings(
        deepseek_api_key=get("DEEPSEEK_API_KEY"),
        deepseek_model=get("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        deepseek_base_url=get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        deepseek_timeout_seconds=int(get("DEEPSEEK_TIMEOUT_SECONDS", "90")),
        max_parallel_jobs=int(get("MAX_PARALLEL_JOBS", "2")),
        compat_name=get("COMPAT_NAME", "其他模型"),
        compat_base_url=get("COMPAT_BASE_URL"),
        compat_api_key=get("COMPAT_API_KEY"),
        compat_models=tuple(m.strip() for m in get("COMPAT_MODELS").split(",") if m.strip()),
        max_running_jobs=max(1, int(get("MAX_RUNNING_JOBS", "8"))),
    )
