"""运行配置：优先读环境变量，其次读项目根目录的 .env。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RUNTIME_DIR = DATA_DIR / "runtime"
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
