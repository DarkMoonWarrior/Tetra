"""Claude：用 Anthropic 官方 Python SDK 调 Messages API。

其余几家走兼容 OpenAI 的接口（deepseek.py）。Claude 的思考和输出控制写法不同，所以单独一个适配器，
对外和 DeepSeekJsonClient 一样：request(...) 返回带 "output" 和 "usage" 的字典，出题流程不用区分。

- 思考：当前的 Claude 型号默认就是自适应思考（自己决定想多少），不传 thinking 参数；
  思考强度用 output_config.effort 控制：要思考的那几步 → high（选"极高"时 → max），其余 → low
  （哪几步要思考由界面上的低／中／高／极高决定，见 question_authoring/providers.py）。
  没有 max 这一档的型号（Opus 4.5）退到 high；不认 effort 的老型号（Sonnet 4.5、Haiku 4.5）不发。
- 一律用流式请求再取完整结果，输出上限放得再大也不会因为单次请求太久而超时。
- 服务端兜底（fallbacks）：Opus、Sonnet 这几个型号被安全分类器拒答时，由服务端按拒答类别换一个型号重跑；
  Haiku 没有这个功能，不加。
- 走哪条路每次请求现定（_RoutedTransport，规则见 net.py）：系统设了代理就先走代理，没连上再直连，
  程序开着的时候开、关代理都马上生效。
"""

from __future__ import annotations

import socket
import threading

import anthropic

try:
    import httpx2 as _httpx  # Anthropic SDK 2.x 用的 HTTP 库
except ImportError:  # 早一些的 SDK 用 httpx，这里用到的接口两者一样
    import httpx as _httpx

from mingti import abort, net
from mingti.deepseek import _HTTP_HINTS, DeepSeekClientError, DeepSeekJsonClient

DEFAULT_BASE_URL = "https://api.anthropic.com"
FALLBACK_MODELS = ("claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")
FALLBACK_BETA = "server-side-fallback-2026-07-01"
_NETWORK_HINT = "在国内一般要能访问境外网络（开着代理），或者改用能转发 Claude 接口的地址"


def _keepalive() -> list[tuple[int, int, int | bool]]:
    """和 SDK 自己建连接时一样：开着 TCP 保活，长时间等回复时连接不容易被中间的设备掐掉。"""
    options: list[tuple[int, int, int | bool]] = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, True)]
    for name, value in (("TCP_KEEPINTVL", 60), ("TCP_KEEPCNT", 5), ("TCP_KEEPIDLE", 60)):
        option = getattr(socket, name, None)
        if option is not None:
            options.append((socket.IPPROTO_TCP, option, value))
    return options


class _RoutedTransport(_httpx.BaseTransport):
    """SDK 的每个请求都现选一条路（net.Routes）：系统设了代理就先走代理，没连上再直连。

    SDK 自带的做法是建客户端时读一次系统的代理设置，之后不变：出题出到一半关掉代理，后面的请求全往
    已经关掉的代理发；没开代理时建的客户端，之后开了代理也用不上。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._transports: dict[str | None, _httpx.HTTPTransport] = {}

    def _transport(self, route: str | None) -> _httpx.HTTPTransport:
        with self._lock:
            transport = self._transports.get(route)
            if transport is None:
                transport = _httpx.HTTPTransport(
                    proxy=route, socket_options=_keepalive(),
                    limits=_httpx.Limits(max_connections=1000, max_keepalive_connections=100),
                )
                self._transports[route] = transport
            return transport

    def handle_request(self, request):  # noqa: ANN001, ANN201 - 和 httpx 的签名一致
        # 连接这一步单独限时：一条路不通时很快就能换下一条，不用等到整个请求超时
        timeout = dict(request.extensions.get("timeout") or {})
        if (timeout.get("connect") or net.CONNECT_TIMEOUT + 1) > net.CONNECT_TIMEOUT:
            request.extensions["timeout"] = {**timeout, "connect": net.CONNECT_TIMEOUT}
        plan = net.Routes(str(request.url))
        for route in plan:
            try:
                response = self._transport(route).handle_request(request)
            except (_httpx.ConnectError, _httpx.ConnectTimeout, _httpx.ProxyError) as exc:
                if not plan.failed(route, exc):
                    raise
            else:
                plan.succeeded(route)
                return response
        raise _httpx.ConnectError("no route", request=request)  # 走不到这里：Routes 至少给一条路

    def close(self) -> None:
        with self._lock:
            transports, self._transports = list(self._transports.values()), {}
        for transport in transports:
            transport.close()


class _HttpClient(anthropic.DefaultHttpxClient):
    def __del__(self) -> None:  # 和 SDK 自己建的客户端一样：不用了就把连接关掉
        try:
            self.close()
        except Exception:  # noqa: BLE001 - 解释器退出时关不掉也没关系
            pass


def _sdk(api_key: str, base_url: str, timeout: float, max_retries: int = 2) -> anthropic.Anthropic:
    return anthropic.Anthropic(
        api_key=api_key, base_url=base_url or DEFAULT_BASE_URL, timeout=timeout, max_retries=max_retries,
        http_client=_HttpClient(transport=_RoutedTransport()),
    )


def _status_error(exc: anthropic.APIStatusError) -> DeepSeekClientError:
    body = getattr(exc, "body", None)
    error = body.get("error") if isinstance(body, dict) else None
    message = (error.get("message") if isinstance(error, dict) else "") or getattr(exc, "message", "") or str(exc)
    hint = _HTTP_HINTS.get(exc.status_code, "模型服务出错")
    return DeepSeekClientError(f"{hint}（HTTP {exc.status_code}：{str(message)[:200]}）")


def _image_block(url: str) -> dict:
    """OpenAI 格式的图片（data URL 或网址）换成 Claude 的写法。"""
    if url.startswith("data:") and ";base64," in url:
        head, data = url.split(";base64,", 1)
        return {"type": "image", "source": {"type": "base64", "media_type": head[5:] or "image/png", "data": data}}
    return {"type": "image", "source": {"type": "url", "url": url}}


def _to_claude(message: dict) -> dict:
    content = message["content"]
    if isinstance(content, list):
        blocks = []
        for part in content:
            if part.get("type") == "image_url":
                blocks.append(_image_block(str((part.get("image_url") or {}).get("url", ""))))
            elif part.get("type") == "text":
                blocks.append({"type": "text", "text": part.get("text", "")})
        content = blocks
    return {"role": message["role"], "content": content}


class ClaudeJsonClient(DeepSeekJsonClient):
    name = "claude"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "claude-opus-5-5",
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: int = 90,
        thinking_style: str = "effort",
    ) -> None:
        super().__init__(
            api_key=api_key, model=model, base_url=base_url, timeout_seconds=timeout_seconds, thinking_style="effort"
        )
        self.sdk = _sdk(api_key, self.base_url, float(timeout_seconds))

    def _complete(
        self, *, system: str, messages: list[dict], max_tokens: int, thinking: bool | str, action: str,
        timeout: float | None = None,
    ) -> tuple[str, dict]:
        params: dict = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [_to_claude(message) for message in messages],
            "output_config": {
                "effort": "max" if thinking == "max" else "medium" if thinking == "light" else "high" if thinking else "low"
            },
        }
        learned = self._learned.setdefault((self.base_url, self.model), {})
        if learned.get("output_config") == "drop":
            params.pop("output_config")
        elif learned.get("output_config") == "cap:high" and params["output_config"]["effort"] == "max":
            params["output_config"] = {"effort": "high"}
        fixes, rerouted = 0, False
        while True:
            try:
                message = self._stream(params, timeout)
                break
            except anthropic.BadRequestError as exc:
                effort = (params.get("output_config") or {}).get("effort")
                if fixes == 2 or not effort or "effort" not in str(getattr(exc, "message", exc)):
                    raise _status_error(exc) from exc
                fixes += 1
                # 这个型号没有 max 这一档（比如 Opus 4.5）：退到 high 再试；
                # 老一点的型号（Haiku 4.5、Sonnet 4.5）根本不认 effort：去掉再试。都记下来，以后不再发
                if effort == "max":
                    learned["output_config"] = "cap:high"
                    params["output_config"] = {"effort": "high"}
                else:
                    learned["output_config"] = "drop"
                    params.pop("output_config")
            except anthropic.APIStatusError as exc:
                raise _status_error(exc) from exc
            except (anthropic.APIConnectionError, _httpx.TransportError) as exc:
                # 回复读到一半断了，原因是刚才走的代理没了（比如刚关掉代理软件）：重发一次，这次会改走直连
                if not rerouted and net.last_route_gone(self.base_url):
                    rerouted = True
                    continue
                raise DeepSeekClientError(f"模型{action}失败：连不上 Claude 的接口（{exc}）。{_NETWORK_HINT}") from exc
        if message.stop_reason == "refusal":
            raise DeepSeekClientError("Claude 按安全策略拒绝了这次请求。可以换一个型号，或者改一下题目要求再试")
        text = "".join(block.text for block in message.content if block.type == "text")
        if not text.strip():
            raise DeepSeekClientError(f"模型返回的结构异常：没有文字内容（stop_reason={message.stop_reason}）")
        usage = message.usage
        cached = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        raw = {
            "id": message.id,
            "model": message.model,
            "stop_reason": message.stop_reason,
            "usage": {
                "input_tokens": usage.input_tokens + cached,
                "output_tokens": usage.output_tokens,
                "total_tokens": usage.input_tokens + cached + usage.output_tokens,
                "prompt_cache_hit_tokens": cached,
            },
        }
        return text, raw

    def _stream(self, params: dict, timeout: float | None = None):
        sdk = self.sdk.with_options(timeout=float(timeout)) if timeout and timeout > self.timeout_seconds else self.sdk
        # 用户点了停止：关掉这条流（见 abort.py），每收到一段也看一眼，停了就不再往下读
        scope = abort.current()
        if scope is not None:
            scope.check()
        manager = (
            sdk.beta.messages.stream(**params, betas=[FALLBACK_BETA], fallbacks="default")
            if self.model in FALLBACK_MODELS
            else sdk.messages.stream(**params)
        )
        try:
            with manager as stream:
                token = scope.watch(stream.close) if scope is not None else None
                try:
                    for _event in stream:
                        if scope is not None and scope.aborted:
                            raise abort.StopRequested()
                    return stream.get_final_message()
                finally:
                    if token is not None:
                        scope.unwatch(token)
        except abort.StopRequested:
            raise
        except Exception as exc:
            if scope is not None and scope.aborted:
                raise abort.StopRequested() from exc
            raise


def list_models(api_key: str, base_url: str = DEFAULT_BASE_URL, timeout: int = 15) -> list[str]:
    """这个 Key 能用的 Claude 型号（Models API，自动翻页）。"""
    client = _sdk(api_key, base_url.rstrip("/"), float(timeout), max_retries=1)
    try:
        return [model.id for model in client.models.list(limit=100)]
    except anthropic.APIStatusError as exc:
        raise _status_error(exc) from exc
    except anthropic.APIConnectionError as exc:
        raise DeepSeekClientError(f"连不上 Claude 的接口：{exc}。{_NETWORK_HINT}") from exc
