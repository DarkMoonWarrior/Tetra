"""模型服务的配置：用户在界面里接入、修改、删除，保存在数据文件夹（config.RUNTIME_DIR，网页版和桌面版共用）的 models.json。

一个"服务"是一家大模型服务：接口地址、API Key、请求格式（兼容 OpenAI 的接口，或 Claude 的官方接口）、
思考模式怎么开关，以及要用的几个模型（型号 + 界面上显示的名称）。可以接好几家，出题方式里的主模型、
把关模型从这里选。常用的几家有现成的模板（presets.py），选了只要填 API Key。

API Key 只存在这台电脑上；返回给页面时只给头尾几位，页面上看不到完整的 Key，修改时 Key 留空表示不改。
以前的做法是把 Key 写在 .env 里：第一次启动时自动搬进来，以后都在界面里改。
"""

from __future__ import annotations

import http.client
import json
import re
import threading
import time
import urllib.error
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mingti import presets
from mingti.deepseek import DeepSeekClientError, DeepSeekJsonClient, fetch

# 思考强度怎么告诉模型："effort" 只用于 Claude 的官方接口，其余几种是兼容 OpenAI 接口的各家写法
ThinkingStyle = Literal["thinking", "enable_thinking", "reasoning_effort", "effort", "none"]
# 请求格式：兼容 OpenAI 的 Chat Completions，或者 Claude 的官方 Messages API
ApiStyle = Literal["openai", "anthropic"]

# 以前 .env 里默认接的那家：已知型号在界面上的名字（不写厂商，按用途起名）
KNOWN_MODELS = {
    "deepseek-flash": ("快速模型", "速度快、费用低，大多数题用它就够"),
    "deepseek-v4-flash": ("快速模型", "速度快、费用低，大多数题用它就够"),
    "deepseek-v4-pro": ("增强模型", "推理更强，但更慢更贵；适合压轴题，或专门用来审题、复核"),
}
# 同一个型号的别名，只留一个
ALIASES = {"deepseek-v4-flash": "deepseek-flash"}


class ModelEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, max_length=120)
    label: str = Field(default="", max_length=30)
    note: str = Field(default="", max_length=120)

    @field_validator("model", "label", "note")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


class ServiceIn(BaseModel):
    """页面提交的一个服务。修改时 api_key 留空表示不改。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=30)
    base_url: str = Field(min_length=8, max_length=300)
    api_key: str = Field(default="", max_length=400)
    api: ApiStyle = "openai"
    thinking: ThinkingStyle = "none"
    # 从哪个模板接入的（presets.py 里的编号）；自己手填的是空串
    preset: str = Field(default="", max_length=30)
    models: list[ModelEntry] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def _thinking_for_api(self) -> "ServiceIn":
        # Claude 的官方接口只有一种思考写法；"effort" 也不能用在兼容 OpenAI 的接口上
        if self.api == "anthropic":
            self.thinking = "effort"
        elif self.thinking == "effort":
            self.thinking = "reasoning_effort"
        return self

    @field_validator("name", "api_key")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @field_validator("base_url")
    @classmethod
    def _url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not re.match(r"^https?://[^\s/]+", value):
            raise ValueError("接口地址要以 http:// 或 https:// 开头")
        return value

    @field_validator("models")
    @classmethod
    def _unique(cls, value: list[ModelEntry]) -> list[ModelEntry]:
        seen: dict[str, ModelEntry] = {}
        for entry in value:
            seen.setdefault(entry.model, entry)
        return list(seen.values())


class Service(ServiceIn):
    id: str = Field(min_length=1, max_length=40)


def key_hint(key: str) -> str:
    """页面上显示的 Key：只露头尾几位。"""
    key = key.strip()
    if not key:
        return ""
    return f"{key[:3]}…{key[-4:]}" if len(key) > 10 else "已保存"


def public(service: Service) -> dict:
    data = service.model_dump(exclude={"api_key"})
    data["key_hint"] = key_hint(service.api_key)
    return data


def make_client(
    *, api: str, base_url: str, api_key: str, model: str, thinking: str, timeout: int
) -> DeepSeekJsonClient:
    """按服务的请求格式建客户端：Claude 的官方接口用官方 SDK（claude.py），其余走兼容 OpenAI 的接口。"""
    if api == "anthropic":
        try:
            from mingti.claude import ClaudeJsonClient
        except ImportError as exc:
            raise DeepSeekClientError(
                "要用 Claude，需要先安装 anthropic 包：重新运行 tetra 会自动装上（requirements.txt 里已经写了）"
            ) from exc
        return ClaudeJsonClient(api_key=api_key, model=model, base_url=base_url, timeout_seconds=timeout)
    return DeepSeekJsonClient(
        api_key=api_key, model=model, base_url=base_url, timeout_seconds=timeout, thinking_style=thinking
    )


def discover_models(base_url: str, api_key: str, *, api: str = "openai", preset: str = "", timeout: int = 15) -> list[str]:
    """问这家服务有哪些模型：兼容 OpenAI 接口的 GET /models，Claude 用官方 SDK 的 Models API。
    从模板接入的按模板挑出对话型号（OpenAI 的列表里还有语音、画图这些）。
    和出题请求一样选路（net.py）：系统设了代理就先走代理（境外的服务在国内要靠它才连得上），走不通再直连。"""
    if api == "anthropic":
        try:
            from mingti.claude import list_models
        except ImportError as exc:
            raise DeepSeekClientError("要用 Claude，需要先安装 anthropic 包") from exc
        return presets.filter_models(preset, list_models(api_key, base_url, timeout))
    try:
        body = fetch(base_url.rstrip("/") + "/models", headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout)
        data = json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise DeepSeekClientError(f"模型服务返回 HTTP {exc.code}，请检查接口地址和 API Key") from exc
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise DeepSeekClientError(f"连不上这个接口地址：{exc}") from exc
    ids = [str(item.get("id")) for item in data.get("data", []) if isinstance(item, dict) and item.get("id")]
    return presets.filter_models(preset, list(dict.fromkeys(ALIASES.get(model, model) for model in ids)))


def test_model(
    base_url: str, api_key: str, model: str, thinking: str, *, api: str = "openai", timeout: int = 60
) -> float:
    """发一次很短的请求，确认这个模型能用、能按 JSON 回答；返回用时（秒）。失败抛 DeepSeekClientError。"""
    client = make_client(api=api, base_url=base_url, api_key=api_key, model=model, thinking=thinking, timeout=timeout)
    start = time.time()
    client.request(
        instructions='这是连接测试。只输出 JSON：{"ok": true}',
        input_text='{"ping": true}',
        action="连接测试",
        max_tokens=50,
    )
    return round(time.time() - start, 1)


class ModelConfigStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    # ---- 读写 ----

    def _read(self) -> dict:
        if not self.path.exists():
            return {"services": [], "migrated_env": False}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"services": [], "migrated_env": True}
        return {"services": data.get("services") or [], "migrated_env": bool(data.get("migrated_env"))}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)

    def services(self) -> list[Service]:
        out = []
        for raw in self._read()["services"]:
            try:
                out.append(Service.model_validate(raw))
            except ValueError:
                continue  # 手改坏了的条目跳过，不让整个列表读不出来
        return out

    def get(self, service_id: str) -> Service | None:
        return next((s for s in self.services() if s.id == service_id), None)

    # ---- 增删改 ----

    def add(self, payload: ServiceIn) -> Service:
        if not payload.api_key:
            raise ValueError("请填写 API Key")
        service = Service(id=f"s{uuid.uuid4().hex[:8]}", **payload.model_dump())
        with self._lock:
            data = self._read()
            data["services"].append(service.model_dump())
            self._write(data)
        return service

    def update(self, service_id: str, payload: ServiceIn) -> Service | None:
        with self._lock:
            data = self._read()
            for index, raw in enumerate(data["services"]):
                if raw.get("id") != service_id:
                    continue
                fields = payload.model_dump()
                if not fields["api_key"]:
                    fields["api_key"] = raw.get("api_key", "")
                service = Service(id=service_id, **fields)
                data["services"][index] = service.model_dump()
                self._write(data)
                return service
        return None

    def delete(self, service_id: str) -> bool:
        with self._lock:
            data = self._read()
            kept = [raw for raw in data["services"] if raw.get("id") != service_id]
            if len(kept) == len(data["services"]):
                return False
            data["services"] = kept
            self._write(data)
            return True

    # ---- 从 .env 搬过来（只做一次） ----

    def migrate_from_env(self, settings) -> None:
        with self._lock:
            data = self._read()
            if data["migrated_env"]:
                return
            services = data["services"]
            if settings.deepseek_api_key and not any(s.get("id") == "deepseek" for s in services):
                configured = ALIASES.get(settings.deepseek_model, settings.deepseek_model)
                try:
                    found = discover_models(settings.deepseek_base_url, settings.deepseek_api_key)
                except DeepSeekClientError:
                    found = []
                names = list(dict.fromkeys([configured, *found]))
                # 编号沿用以前的 "deepseek"，旧的出题记录和个人设置里存的模型编号照样能用
                services.append(Service(
                    id="deepseek",
                    name="默认服务",
                    base_url=settings.deepseek_base_url,
                    api_key=settings.deepseek_api_key,
                    thinking="thinking",
                    models=[
                        ModelEntry(model=name, label=KNOWN_MODELS.get(name, (name, ""))[0], note=KNOWN_MODELS.get(name, ("", ""))[1])
                        for name in names
                    ],
                ).model_dump())
            if settings.compat_api_key and settings.compat_base_url and settings.compat_models:
                services.append(Service(
                    id="compat",
                    name=settings.compat_name[:30] or "其他模型",
                    base_url=settings.compat_base_url,
                    api_key=settings.compat_api_key,
                    thinking="none",
                    models=[ModelEntry(model=name, label=name[:30]) for name in settings.compat_models],
                ).model_dump())
            data["migrated_env"] = True
            self._write(data)
