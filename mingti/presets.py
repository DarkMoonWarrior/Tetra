"""常用模型服务的模板：在「个人设置 → 模型服务」里选一家，填上 API Key 就能用。

每个模板写好了接口地址、请求格式、思考强度怎么传，以及几个推荐型号和它们在界面上的名字。
名字按用途起（快速／均衡／增强），出题界面上看到的还是这些名字；服务名默认是厂商名，可以改。
型号和参数照各家 2026 年 11 月的官方文档：
- DeepSeek：https://api-docs.deepseek.com/zh-cn/quick_start/pricing （思考开关 thinking.type）
- 智谱：https://docs.bigmodel.cn/cn/guide/models/text/glm-5.3 （GLM-5.3 一直在思考，强度用 reasoning_effort）
- OpenAI：https://developers.openai.com/api/docs/models （Chat Completions，reasoning_effort，max_completion_tokens）
- Claude：Anthropic 官方 SDK 的 Messages API（见 claude.py）
"""

from __future__ import annotations

import copy
import re

PRESETS: list[dict] = [
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "api": "openai",
        "base_url": "https://api.deepseek.com",
        "thinking": "thinking",
        "key_url": "https://platform.deepseek.com/api_keys",
        "key_placeholder": "sk-…",
        "intro": "国内直连，价格低。快速模型出大多数题，增强模型用来审题、复核。",
        "models": [
            {"model": "deepseek-flash", "label": "快速模型", "note": "速度快、费用低，大多数题用它就够"},
            {"model": "deepseek-v4-pro", "label": "增强模型", "note": "推理更强，但更慢更贵；适合压轴题，或专门用来审题、复核"},
        ],
    },
    {
        "id": "zhipu",
        "name": "智谱 GLM",
        "api": "openai",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "thinking": "reasoning_effort",
        "key_url": "https://bigmodel.cn/usercenter/proj-mgmt/apikeys",
        "key_placeholder": "粘贴智谱开放平台的 API Key",
        "intro": "国内直连。GLM-5.3 系列一直在思考，思考强度选“低”时少想一些。",
        "models": [
            {"model": "glm-5.3-flash", "label": "快速模型", "note": "价格是旗舰的十分之一，大多数题用它就够"},
            {"model": "glm-5.3", "label": "增强模型", "note": "智谱的旗舰模型，推理更强；适合压轴题，或专门用来审题、复核"},
        ],
        "discover_include": r"^glm-",
        "discover_exclude": r"image|ocr|asr|tts|embed|video|legal|cogview",
    },
    {
        "id": "openai",
        "name": "OpenAI",
        "api": "openai",
        "base_url": "https://api.openai.com/v1",
        "thinking": "reasoning_effort",
        "key_url": "https://platform.openai.com/api-keys",
        "key_placeholder": "sk-…",
        "intro": "需要能访问境外网络。均衡模型出大多数题，增强模型最强也最贵。",
        "overseas": True,
        "models": [
            {"model": "gpt-6.1-sol", "label": "均衡模型", "note": "能力接近旗舰，价格低得多；大多数题用它"},
            {"model": "gpt-6-astra", "label": "增强模型", "note": "最强也最贵；适合压轴题，或专门用来审题、复核"},
            {"model": "gpt-6-luna", "label": "快速模型", "note": "最便宜、最快，适合基础题"},
        ],
        "discover_include": r"^(gpt-|o\d)",
        "discover_exclude": r"audio|realtime|live|transcri|tts|speech|image|search|embed|moderation|whisper|dall|sora|instruct|translat|cyber|rosalind",
    },
    {
        "id": "anthropic",
        "name": "Claude",
        "api": "anthropic",
        "base_url": "https://api.anthropic.com",
        "thinking": "effort",
        "key_url": "https://platform.claude.com/settings/keys",
        "key_placeholder": "sk-ant-…",
        "intro": "需要能访问境外网络。用 Anthropic 官方接口，模型自己决定想多少，思考强度控制想得深浅。",
        "overseas": True,
        "models": [
            {"model": "claude-opus-5-5", "label": "增强模型", "note": "Claude 最新的 Opus，推理最强"},
            {"model": "claude-sonnet-5-5", "label": "均衡模型", "note": "速度和能力兼顾，价格是 Opus 的一半"},
            {"model": "claude-haiku-5-5", "label": "快速模型", "note": "最快最便宜，适合基础题"},
        ],
    },
]

# 页面上用得到的字段（发现型号用的过滤规则留在后台）
_PUBLIC_FIELDS = ("id", "name", "api", "base_url", "thinking", "key_url", "key_placeholder", "intro", "overseas", "models")


def public_presets() -> list[dict]:
    return [{key: copy.deepcopy(preset[key]) for key in _PUBLIC_FIELDS if key in preset} for preset in PRESETS]


def find(preset_id: str) -> dict | None:
    return next((preset for preset in PRESETS if preset["id"] == preset_id), None)


def filter_models(preset_id: str, ids: list[str]) -> list[str]:
    """按模板挑出能用来出题的对话型号（OpenAI 的列表里还有语音、画图、向量这些）。"""
    preset = find(preset_id) or {}
    include = preset.get("discover_include")
    exclude = preset.get("discover_exclude")
    return [
        model for model in ids
        if (not include or re.search(include, model, re.IGNORECASE))
        and not (exclude and re.search(exclude, model, re.IGNORECASE))
    ]
