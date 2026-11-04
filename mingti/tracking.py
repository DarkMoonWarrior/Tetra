"""记录每一次模型调用：写入 SQLite，同时实时汇报给当前命题任务。

命题流程里的每个阶段（构思、审题、写解析……）都通过 recorder.track() 调用模型，
这里包一层，就能在界面上逐条显示"哪个命题组的哪个角色在做什么、用的什么模型、
用了多久、花了多少 token"。另外在每次调用前检查是否该停下（别的组已交卷或用户点了停止）。
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Callable, Iterator

from mingti.model_operations import ModelRunRecorder

# listener(event, call, updates)：event 为 "start" 或 "end"；
# "end" 时由监听者在自己的锁里把 updates 合并进 call，避免界面读取时数据被并发修改。
CallListener = Callable[[str, dict, dict], None]


class StopRequested(Exception):
    """别的命题组已经交卷，或用户点了停止：在下一次调用模型前结束。"""


class _SpanProxy:
    def __init__(self, span, usage: dict) -> None:
        self._span = span
        self._usage = usage

    def capture_response(self, payload: dict) -> None:
        self._span.capture_response(payload)
        usage = payload.get("usage") or {}
        self._usage["input_tokens"] = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        self._usage["output_tokens"] = int(
            usage.get("completion_tokens") or usage.get("output_tokens") or 0
        )


class JobRecorder:
    """满足 ModelRunRecorder 协议：转发给持久化记录器，并回调任务监听者。"""

    def __init__(
        self,
        base: ModelRunRecorder,
        listener: CallListener,
        *,
        lane: str = "A",
        role_of: Callable[[str], str] = lambda stage: "composer",
        should_stop: Callable[[], bool] = lambda: False,
    ) -> None:
        self._base = base
        self._listener = listener
        self._lane = lane
        self._role_of = role_of
        self._should_stop = should_stop

    @contextmanager
    def track(
        self,
        *,
        feature: str,
        provider: str,
        model: str,
        prompt_version: str,
        actor_id: str | None = None,
        validation_owner_id: str | None = None,
    ) -> Iterator[_SpanProxy]:
        if self._should_stop():
            raise StopRequested()
        stage = prompt_version.partition(":")[2] or "single"
        call = {
            "stage": stage,
            "lane": self._lane,
            "role": self._role_of(stage),
            "model": model,
            "started": time.time(),
        }
        self._listener("start", call, {})
        usage: dict = {}
        ok = False
        try:
            with self._base.track(
                feature=feature,
                provider=provider,
                model=model,
                prompt_version=prompt_version,
                actor_id=actor_id,
                validation_owner_id=validation_owner_id,
            ) as span:
                yield _SpanProxy(span, usage)
            ok = True
        finally:
            self._listener(
                "end",
                call,
                {
                    "seconds": round(time.time() - call["started"], 2),
                    "ok": ok,
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                },
            )
