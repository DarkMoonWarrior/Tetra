"""eval 下几个脚本共用的东西：结果放在哪、jsonl 怎么读写、按 DeepSeek 价目表算钱。"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "eval" / "results"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # 让 eval/ 下的脚本能 import mingti

# DeepSeek 价目表，美元 / 百万 Token：（缓存命中的输入，缓存未命中的输入，输出）。
# 2026-11-04 抄自 api-docs.deepseek.com/quick_start/pricing，这是闲时价；高峰时段翻倍。
PRICES = {
    "deepseek-flash": (0.003, 0.15, 0.60),
    "deepseek-v4-flash": (0.003, 0.15, 0.60),
    "deepseek-v4-pro": (0.022, 0.66, 1.98),
}


def is_peak(moment: float) -> bool:
    """高峰时段：周一到周五 UTC 1–4 点、6–10 点，即北京时间 9–12 点、14–18 点（节假日按闲时算，这里不管）。"""
    utc = datetime.fromtimestamp(moment, timezone.utc)
    return utc.weekday() < 5 and (1 <= utc.hour < 4 or 6 <= utc.hour < 10)


def call_cost(call: dict, *, peak: bool | None = None) -> float:
    """一次调用花了多少美元。peak 不给时按调用开始的时刻判断是不是高峰。"""
    hit_rate, miss_rate, out_rate = PRICES.get(call["model"], PRICES["deepseek-flash"])
    hit = call.get("cache_hit", 0)
    miss = max(0, call.get("input_tokens", 0) - hit)
    usd = (hit * hit_rate + miss * miss_rate + call.get("output_tokens", 0) * out_rate) / 1e6
    if peak is None:
        peak = is_peak(call["started"])
    return usd * (2 if peak else 1)


def usage_of(payload: dict) -> dict:
    """从 DeepSeek 的返回里取 Token 用量：输入（其中缓存命中多少）、输出（其中思考多少）。"""
    usage = payload.get("usage") or {}
    return {
        "input_tokens": int(usage.get("prompt_tokens") or 0),
        "cache_hit": int(usage.get("prompt_cache_hit_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or 0),
        "reasoning_tokens": int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0),
    }


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


class JsonlWriter:
    """多个线程往同一个文件追加，一条记录一行；写完马上落盘，中途停了也不丢。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False)
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def latest(rows: list[dict]) -> dict[str, dict]:
    """同一个编号跑过几次时只留最后一次（出错重跑的情况）。"""
    return {row["id"]: row for row in rows}


# 账户余额用完（HTTP 402）以后脚本就停下，不再一个个发注定失败的请求
OUT_OF_BALANCE = threading.Event()
# 被限流（HTTP 429）等了几次、共几秒：等的时间算在那几道题的用时里，结束时报出来，看用时可不可信
RATE_LIMITED = {"times": 0, "seconds": 0.0}
_RATE_LOCK = threading.Lock()


def patient_client(*, api_key: str, model: str, base_url: str, timeout_seconds: int):
    """实验用的 DeepSeek 客户端：遇到 429（并发超限）或 5xx 等一会儿再试，最多再试 3 次；
    遇到 402（余额不足）记下来让脚本停下。只改网络层，出题流程本身不变。"""
    from mingti.deepseek import DeepSeekClientError, DeepSeekJsonClient

    class PatientClient(DeepSeekJsonClient):
        def _post_json(self, *, payload: dict, action: str, timeout: float | None = None) -> dict:
            for attempt in range(4):
                try:
                    return super()._post_json(payload=payload, action=action, timeout=timeout)
                except DeepSeekClientError as exc:
                    if "HTTP 402" in str(exc):
                        OUT_OF_BALANCE.set()
                    if attempt == 3 or not re.search(r"HTTP (?:429|5\d\d)", str(exc)):
                        raise
                    wait = 15 * (attempt + 1)
                    if "HTTP 429" in str(exc):
                        with _RATE_LOCK:
                            RATE_LIMITED["times"] += 1
                            RATE_LIMITED["seconds"] += wait
                    time.sleep(wait)
            raise AssertionError("unreachable")

    return PatientClient(api_key=api_key, model=model, base_url=base_url, timeout_seconds=timeout_seconds)
