"""可选模型：来自用户在界面里接入的模型服务（见 model_config.py）。

界面上的模型选择器读这里的列表；每个角色（命题员、审题员……）都可以单独指定模型。
接入、修改、删除服务后调用 reload()，新出的题马上用上，不用重启。
"""

from __future__ import annotations

from dataclasses import dataclass

from mingti.config import RUNTIME_DIR, Settings
from mingti.deepseek import DeepSeekJsonClient
from mingti.model_config import ModelConfigStore, make_client


@dataclass(frozen=True)
class ModelOption:
    id: str
    provider: str
    provider_label: str
    model: str
    label: str
    note: str
    base_url: str
    api_key: str
    thinking: str
    api: str = "openai"

    def public(self) -> dict:
        return {
            "id": self.id,
            "service": self.provider,
            "provider": self.provider_label,
            "model": self.model,
            "label": self.label,
            "note": self.note,
        }


@dataclass(frozen=True)
class ModelRoute:
    """某个角色实际使用的模型和客户端。"""

    id: str
    provider: str
    model: str
    client: DeepSeekJsonClient


class ModelRegistry:
    def __init__(self, settings: Settings, store: ModelConfigStore | None = None) -> None:
        self.settings = settings
        self.store = store or ModelConfigStore(RUNTIME_DIR / "models.json")
        self.store.migrate_from_env(settings)
        self.options: list[ModelOption] = []
        self.reload()

    def reload(self) -> None:
        """按配置重新列出可用的模型（服务没填 Key 的跳过）。"""
        options: list[ModelOption] = []
        seen: set[str] = set()
        for service in self.store.services():
            if not service.api_key:
                continue
            for entry in service.models:
                if f"{service.id}:{entry.model}" in seen:
                    continue
                seen.add(f"{service.id}:{entry.model}")
                options.append(
                    ModelOption(
                        id=f"{service.id}:{entry.model}",
                        provider=service.id,
                        provider_label=service.name,
                        model=entry.model,
                        label=entry.label or entry.model,
                        note=entry.note,
                        base_url=service.base_url,
                        api_key=service.api_key,
                        thinking=service.thinking,
                        api=service.api,
                    )
                )
        self.options = options

    @property
    def default_id(self) -> str | None:
        return self.options[0].id if self.options else None

    def public(self) -> list[dict]:
        return [option.public() for option in self.options]

    def route(self, model_id: str | None) -> ModelRoute:
        options = self.options
        option = next((o for o in options if o.id == model_id), None) or options[0]
        client = make_client(
            api=option.api,
            base_url=option.base_url,
            api_key=option.api_key,
            model=option.model,
            thinking=option.thinking,
            timeout=self.settings.deepseek_timeout_seconds,
        )
        return ModelRoute(id=option.id, provider=option.provider, model=option.model, client=client)
