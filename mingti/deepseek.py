from __future__ import annotations

import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class DeepSeekClientError(RuntimeError):
    pass


# 常见的出错情况先说人话，原始信息放在括号里
_HTTP_HINTS = {
    400: "请求被模型服务拒绝，可能是型号写错了，或者这家不支持 JSON 输出",
    401: "API Key 不对或已经失效，请到「个人设置 → 模型服务」里检查",
    402: "模型服务的账户余额不足，请到服务商那里充值后再试",
    403: "这个 API Key 没有调用这个模型的权限",
    404: "接口地址或型号不对，请到「个人设置 → 模型服务」里检查",
    429: "请求太频繁或者额度用完了，稍后再试",
    500: "模型服务暂时出错，稍后再试",
    502: "模型服务暂时出错，稍后再试",
    503: "模型服务正忙，稍后再试",
}


# LaTeX（KaTeX）里以 \n 开头的命令。读 JSON 时"\n"后面紧跟字母，拼出来是这些之一才当 LaTeX 命令，否则是换行
LATEX_N_COMMANDS = frozenset(
    """nabla natural ncong ne nearrow neg negthickspace negmedspace negthinspace neq newcommand newline nexists
    ngeq ngeqq ngeqslant ngtr ni nleftarrow nLeftarrow nleftrightarrow nLeftrightarrow nleq nleqq nleqslant nless
    nmid nobreak nobreakspace noexpand nolimits nonumber normalfont normalsize not notag notin notni nparallel nprec
    npreceq nrightarrow nRightarrow nshortmid nshortparallel nsim nsubset nsubseteq nsubseteqq nsucc nsucceq
    nsupset nsupseteq nsupseteqq ntriangleleft ntrianglelefteq ntriangleright ntrianglerighteq nu nvdash nvDash
    nVdash nVDash nwarrow""".split()
)


class DeepSeekJsonResponseError(DeepSeekClientError):
    """The provider responded, but its model-generated JSON was unreadable."""


class _UnsupportedParameter(Exception):
    """模型服务明确说不认请求里的某个可选参数（OpenAI 的 unsupported_parameter 报错）。"""

    def __init__(self, param: str, error: DeepSeekClientError) -> None:
        super().__init__(param)
        self.param = param
        self.error = error


# 这几种思考方式下，模型就算思考强度选"低"也会先想一会儿（只是想得少），输出上限要留出余量
ALWAYS_REASONING = ("reasoning_effort", "effort")
# 可以去掉再试的可选参数：有的型号不认（比如老一点的 GPT 不认 reasoning_effort）
_OPTIONAL_PARAMS = ("reasoning_effort", "thinking", "enable_thinking", "response_format")
# reasoning_effort 从低到高的取值。思考强度"极高"先发 max（DeepSeek、智谱 GLM-5.3 最深的一档）；
# 这家不认就往下退一档再试，并记下来（OpenAI 没有 max，最高是 xhigh）
EFFORT_LADDER = ("low", "medium", "high", "xhigh", "max")
# 极高档：单次调用最多等 10 分钟；输出上限放到 64K（思考的内容也算输出，DeepSeek 最多能输出 384K）
MAX_EFFORT_TIMEOUT = 600
MAX_EFFORT_TOKENS = 65536


class DeepSeekJsonClient:
    """Minimal OpenAI-compatible Chat Completions adapter for JSON workflows."""

    name = "deepseek"
    # 运行中发现某个型号不认某个可选参数后记下来，之后不再发：{(接口地址, 型号): {参数名: 处理办法}}
    _learned: dict[tuple[str, str], dict[str, str]] = {}

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "deepseek-v4-flash",
        base_url: str = "https://api.deepseek.com",
        timeout_seconds: int = 90,
        thinking_style: str = "thinking",
    ) -> None:
        """thinking_style：这家服务怎么开关思考模式。"thinking" 传 {"thinking": {"type": "enabled"/"disabled"}}，
        "enable_thinking" 传 {"enable_thinking": true/false}，"reasoning_effort" 传 {"reasoning_effort": "high"/"low"}
        （OpenAI、智谱 GLM-5 这类一直在思考、只能调强度的），"none" 不支持、什么都不传。"""
        if not api_key:
            raise ValueError("模型的 API Key 不能为空")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.thinking_style = thinking_style
        # OpenAI 的推理模型不认 max_tokens，要写 max_completion_tokens
        self.token_param = "max_completion_tokens" if "api.openai.com" in self.base_url else "max_tokens"

    def request(
        self,
        *,
        instructions: str,
        input_text: str,
        action: str,
        output_schema: dict | None = None,
        max_tokens: int = 4096,
        image_data_url: str | None = None,
        recovery_instruction: str = "",
        thinking: bool | str = False,
        image_data_urls: list[str] | None = None,
    ) -> dict:
        """thinking=True 时打开模型的思考模式：先在内部推演再输出，算得更准，但慢几秒、多用一些 Token；
        thinking="max" 再请模型用最深的一档思考（思考强度"极高"），更慢，等待时间和输出上限都放宽；
        thinking="light" 打开思考但用最轻的一档（聊天回答选"中"时）。"""
        schema_instruction = ""
        if output_schema:
            schema = output_schema.get("schema", output_schema)
            schema_instruction = (
                "\n输出必须严格符合以下 JSON Schema，不得增加额外字段：\n"
                + json.dumps(schema, ensure_ascii=False)
            )
        system = (
            f"{instructions}{schema_instruction}\n必须只输出合法 JSON，不要使用 Markdown 代码块。"
        )
        user_content: str | list[dict[str, object]] = input_text
        # 图片：一张（image_data_url，导入时一页一页地认）或者好几张（image_data_urls，聊天里一次发几张）
        images = [url for url in [image_data_url, *(image_data_urls or [])] if url]
        if images:
            user_content = [
                {"type": "text", "text": input_text},
                *({"type": "image_url", "image_url": {"url": url, "detail": "original"}} for url in images),
            ]
        messages: list[dict] = [{"role": "user", "content": user_content}]
        if self.thinking_style == "none":
            thinking = False
        deepest = thinking == "max"
        # 思考的内容也算输出 Token，打开思考时把上限放宽，免得把正式回答截断；
        # 一直在思考的型号（ALWAYS_REASONING）强度"低"时也要留一些
        if deepest:
            limit = min(MAX_EFFORT_TOKENS, max_tokens + 48000)
        elif thinking:
            limit = min(32768, max_tokens + 16000)
        elif self.thinking_style in ALWAYS_REASONING:
            limit = min(16384, max_tokens + 6000)
        else:
            limit = max(256, min(max_tokens, 8192))
        ceiling = MAX_EFFORT_TOKENS if deepest else 32768 if thinking or self.thinking_style in ALWAYS_REASONING else 8192
        timeout = max(self.timeout_seconds, MAX_EFFORT_TIMEOUT) if deepest else self.timeout_seconds
        first_parse_error: json.JSONDecodeError | ValueError | None = None
        for attempt in range(2):
            content, raw = self._complete(
                system=system, messages=messages, max_tokens=limit, thinking=thinking, action=action, timeout=timeout
            )
            try:
                normalized = self._normalize_json(content)
            except (json.JSONDecodeError, ValueError) as exc:
                if attempt == 1:
                    cause = first_parse_error or exc
                    raise DeepSeekJsonResponseError(
                        f"模型返回的 JSON 无法解析：{cause}；格式恢复重试仍未成功：{exc}"
                    ) from exc
                first_parse_error = exc
                messages = [
                    *messages,
                    {
                        "role": "user",
                        "content": (
                            "上一份回答因长度截断或 JSON 格式不完整而无法读取。"
                            "请重新从头输出完整、合法且符合既定 Schema 的 JSON；"
                            "适当压缩表述，但不得省略必填字段。"
                            f"{recovery_instruction.strip()}"
                        ),
                    },
                ]
                limit = min(ceiling, max(4096, limit * (1 if recovery_instruction.strip() else 2)))
                continue
            return {
                **raw,
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": normalized}]}
                ],
            }
        raise AssertionError("unreachable")

    def _thinking_params(self, thinking: bool | str) -> dict:
        """thinking：False 不思考；"light" 想一会儿（模型最轻的一档，聊天回答选"中"时用）；True 正常思考；"max" 最深的一档。"""
        deepest = thinking == "max"
        light = thinking == "light"
        if self.thinking_style == "thinking":
            # DeepSeek 等：打开思考时默认强度是 high；极高档写 reasoning_effort: max，"light" 写 low
            params = {"thinking": {"type": "enabled" if thinking else "disabled"}}
            if deepest:
                return {**params, "reasoning_effort": "max"}
            return {**params, "reasoning_effort": "low"} if light else params
        if self.thinking_style == "enable_thinking":
            return {"enable_thinking": bool(thinking)}  # 只能开、关，极高、高和"light"都一样
        if self.thinking_style == "reasoning_effort":
            return {"reasoning_effort": "max" if deepest else "low" if light else "high" if thinking else "low"}
        return {}

    def _complete(
        self, *, system: str, messages: list[dict], max_tokens: int, thinking: bool | str, action: str,
        timeout: float | None = None,
    ) -> tuple[str, dict]:
        """发一次请求，返回（模型写的文字，原始返回）。Claude 的适配器（claude.py）换成自己的写法。"""
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "response_format": {"type": "json_object"},
            self.token_param: max_tokens,
            "stream": False,
            **self._thinking_params(thinking),
        }
        learned = self._learned.setdefault((self.base_url, self.model), {})
        for _ in range(len(_OPTIONAL_PARAMS) + len(EFFORT_LADDER) + 1):
            for param, fix in learned.items():
                self._apply_fix(payload, param, fix)
            try:
                raw = self._post_json(payload=payload, action=action, timeout=timeout)
                break
            except _UnsupportedParameter as exc:
                lower = self._lower_effort(payload, exc.param)
                if lower:  # 这家不认这么高的思考强度：退一档再试，以后也不再发更高的
                    learned[exc.param] = f"cap:{lower}"
                    continue
                fix = "rename" if exc.param == "max_tokens" else "drop"
                known = learned.get(exc.param)
                if (known and not known.startswith("cap:")) or (fix == "drop" and exc.param not in _OPTIONAL_PARAMS):
                    raise exc.error from exc
                learned[exc.param] = fix
        else:
            raise DeepSeekClientError(f"模型{action}失败：请求参数一直不被接受")
        try:
            content = raw["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("返回内容为空")
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise DeepSeekClientError(f"模型返回的结构异常：{exc}") from exc
        return content, raw

    @staticmethod
    def _lower_effort(payload: dict, param: str) -> str:
        """被拒的是 high 以上的思考强度（max、xhigh）时，返回低一档的取值；别的情况返回空串。"""
        value = payload.get(param)
        if param != "reasoning_effort" or value not in EFFORT_LADDER:
            return ""
        index = EFFORT_LADDER.index(value)
        return EFFORT_LADDER[index - 1] if index > EFFORT_LADDER.index("high") else ""

    @staticmethod
    def _apply_fix(payload: dict, param: str, fix: str) -> None:
        if param not in payload:
            return
        if fix.startswith("cap:"):  # 这家的思考强度最高只到某一档
            cap, value = fix[4:], payload[param]
            if value in EFFORT_LADDER and EFFORT_LADDER.index(value) > EFFORT_LADDER.index(cap):
                payload[param] = cap
            return
        value = payload.pop(param)
        if fix == "rename" and param == "max_tokens":
            payload["max_completion_tokens"] = value

    def _post_json(self, *, payload: dict, action: str, timeout: float | None = None) -> dict:
        request = Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout or self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")[:2000]
            try:
                error = json.loads(details)["error"]
                message = error["message"]
            except (ValueError, KeyError, TypeError):
                error, message = {}, details
            failure = DeepSeekClientError(
                f"{_HTTP_HINTS.get(exc.code, '模型服务出错')}（HTTP {exc.code}：{str(message)[:200]}）"
            )
            param = self._unsupported_param(exc.code, error, str(message))
            if param and param in payload:
                raise _UnsupportedParameter(param, failure) from exc
            raise failure from exc
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise DeepSeekClientError(f"模型{action}失败：{exc}") from exc

    @staticmethod
    def _unsupported_param(status: int, error: object, message: str) -> str:
        """OpenAI 式的"不支持这个参数"报错里，被拒的参数名；不是这类报错时返回空串。"""
        if status != 400:
            return ""
        code = error.get("code") if isinstance(error, dict) else None
        param = error.get("param") if isinstance(error, dict) else None
        if code in ("unsupported_parameter", "unsupported_value") and isinstance(param, str):
            return param.split(".")[0]
        match = re.search(r"[Uu]nsupported (?:parameter|value):?\s*'([\w.]+)'", message)
        return match.group(1).split(".")[0] if match else ""

    @staticmethod
    def _normalize_json(content: str) -> str:
        """Recover one JSON object while leaving schema checks to providers."""
        value = content.strip()
        fence = re.match(r"^```(?:json)?\s*(.*?)\s*```", value, flags=re.DOTALL | re.IGNORECASE)
        if fence:
            value = fence.group(1).strip()
        # JSON considers \b, \f, \n, \r, \t and \u valid escape prefixes,
        # while the same prefixes start many LaTeX commands. Preserve any
        # multi-letter command with those prefixes before decoding so commands
        # such as \frac, \nabla, \right and \underbrace are not converted into
        # controls or rejected as malformed unicode escapes.
        value = DeepSeekJsonClient._protect_latex_control_prefixes(value)
        decoder = json.JSONDecoder(strict=False)
        try:
            parsed, _end = decoder.raw_decode(value)
        except json.JSONDecodeError:
            # LaTeX backslashes are occasionally not JSON-escaped by the model.
            repaired = DeepSeekJsonClient._escape_invalid_json_backslashes(value)
            parsed, _end = decoder.raw_decode(repaired)
        if not isinstance(parsed, dict):
            raise ValueError("顶层结果必须是 JSON 对象")
        return json.dumps(parsed, ensure_ascii=False)

    @staticmethod
    def _escape_invalid_json_backslashes(value: str) -> str:
        """Escape invalid JSON backslashes without touching existing pairs."""
        repaired: list[str] = []
        index = 0
        length = len(value)
        while index < length:
            character = value[index]
            if character != "\\":
                repaired.append(character)
                index += 1
                continue
            if index + 1 >= length:
                repaired.append("\\\\")
                index += 1
                continue
            escaped = value[index + 1]
            if escaped in '"\\/bfnrt':
                repaired.extend(("\\", escaped))
                index += 2
                continue
            if (
                escaped == "u"
                and index + 5 < length
                and all(item in "0123456789abcdefABCDEF" for item in value[index + 2:index + 6])
            ):
                repaired.append(value[index:index + 6])
                index += 6
                continue
            repaired.append("\\\\")
            index += 1
        return "".join(repaired)

    @staticmethod
    def _protect_latex_control_prefixes(value: str) -> str:
        """Escape single LaTeX slashes that JSON would treat as controls.

        \\n 后面跟字母的，只有拼出来是 LaTeX 里以 n 开头的命令（\\neq、\\nabla……）才当命令保留；
        其余是模型写的换行（比如 cases 里换行后紧跟 x^2），照常当换行，不然公式里会多出一个认不出的"\\nx"。"""
        protected: list[str] = []
        index = 0
        length = len(value)
        while index < length:
            character = value[index]
            if character != "\\" or index + 1 >= length:
                protected.append(character)
                index += 1
                continue
            if value[index + 1] == "\\":
                protected.append("\\\\")
                index += 2
                continue
            command = re.match(r"[bfnrtu][A-Za-z]+", value[index + 1:])
            if command and command.group(0)[0] == "n" and command.group(0) not in LATEX_N_COMMANDS:
                command = None
            protected.append("\\\\" if command else "\\")
            index += 1
        return "".join(protected)
