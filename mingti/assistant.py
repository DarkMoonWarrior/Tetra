"""对话助手：先看用户这句话想做什么——聊天、出题还是组卷。

- 判断：一次不开思考的模型调用（一两秒），同时提取出题设置。
- 聊天（打招呼、问知识点、讲题、解题、教学建议……）：像一般的大模型助手那样回答。回答的思考强度按输入框
  右下角显示的那一档：低 = 不另外思考，判断时顺手写好回答（一次调用）；中 = 想一会儿（模型最轻的一档思考）；
  高 = 正常思考；极高 = 最深的一档。
- 出题：把这句话里说到的题型、难度、数量、知识点等提取出来，交给界面填到输入框下方的选项里；
  再把这句话连同前文里相关的要求写成一段完整的出题要求（命题员看不到前面的对话），走出题流程。
- 组卷：交给自动组卷（assemble.py）。

前文（上下文）由 context.py 按 Token 预算整理：最近几轮原文 + 更早内容的摘要；太长时先自动压缩。
聊天记录存进对话（store.messages）。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone

from mingti import context
from mingti.deepseek import DeepSeekClientError
from mingti.question_authoring import PROMPT_REVISION, DeepSeekQuestionAuthoringProvider
from mingti.tracking import JobRecorder

logger = logging.getLogger("mingti.assistant")

TYPES = {"single_choice": "单选题", "multiple_choice": "多选题", "fill_blank": "填空题", "open_response": "解答题"}
GRADES = ("高一", "高二", "高三")
PURPOSES = ("课堂练习", "课后作业", "单元测验", "高考复习")
# 一次调用（聊天回答、定组卷方案）的思考：低 = 不思考，中 = 最轻的一档（"light"），高 = 正常思考，极高 = 最深的一档
CHAT_THINKING = {"low": False, "medium": "light", "high": True, "max": "max"}

ROUTE_STAGE = "assistant-route"
REPLY_STAGE = "assistant-reply"

ROUTE_INSTRUCTIONS = """你是 Tetra，一个帮高中数学老师出题、组卷的智能助手，也能像一般的大模型助手一样聊天、答疑。summary 和 history 是这个对话的前文（summary 是更早内容的摘要）。
先判断用户这条消息（message）想让你做什么，填 intent：
- generate：要你现在出新题，比如“出一道……题”“来 3 道……”“编一道……”“再来一道”；也包括要你把刚出的题改一改再出（“难一点”“换个情境”“改成填空题”）。
- paper：要你组一套试卷、测验卷、练习卷，比如“帮我组一套高一期末试卷”“从题库里挑题组一份单元测验”“出一套模拟卷”。
- import：用户附上了图片或文件（attachments 不为空），要你把里面的题目存进题库，比如“把这些题存进题库”“录到题库里”“导入题库”“帮我收藏这几道题”。attachments 为空时不能选 import。
- chat：其余所有情况：打招呼、闲聊、问数学知识、请你讲解或解一道题、问教学建议、问这个应用怎么用、问题库里有什么等。只是讨论出题（比如“怎样出好一道导数题”），没有要你现在出，也算 chat。
  用户附上了图片或文件、要你看里面的内容回答（解题、讲解、检查答案、总结文件……）也是 chat；附上了文件但 message 是空的，当作“识别里面的题并解答”，选 chat，needs_reasoning 填 true。
  附上了一道题、要你照着它出类似的新题（“照这道题出一道类似的”“仿照这道题改编”），选 generate；附件里的题会由系统读出来附在 requirement 后面，requirement 只写用户对新题的要求，不用写怎么看图。
这是一个出题应用：用户只写了对题目的要求（情境、考点、难度、设问方式……）、没有说别的，也当作要出题（generate）。
拿不准时：touched 为 true（用户刚在输入框下方调过题型、难度等出题选项）而且这句话像是对题目的要求，选 generate；否则选 chat。

reply 写给用户看，用 reply_language 指定的语言（中文或 English）：
- chat：write_reply 为 true 时直接回答，自然、简洁、友好，不要客套话；数学公式用 LaTeX，行内写在 $…$ 里，单独一行的写在 $$…$$ 里；需要时可以用 Markdown 的列表和加粗；打招呼时简单回应，再用一两句话说说你能做什么（按要求出题并逐道审核、从题库自动组卷、解答数学问题）；问题库、试卷的情况时参考 bank。write_reply 为 false 时 reply 留空字符串（会另外请模型回答）。
- generate、paper、import：一句话说明接下来要做什么，比如“好的，出 3 道椭圆离心率的单选题。”“好的，先识别里面的题目，核对后再存进题库。”

needs_reasoning：intent 为 chat 且用户要你解一道具体的数学题、证明、计算或判断对错时填 true（这时 reply 留空字符串），其余填 false。

generate：只在 intent 为 generate 时填，其余情况填 null。只提取用户明确说到的出题设置；没说到的一律填 null，不要猜，也不要照抄 current（current 只用来算“难一点”“再来两道”这类相对的说法）：
- question_type：single_choice（单选）、multiple_choice（多选）、fill_blank（填空）、open_response（解答题、大题）
- difficulty：1 基础、2 较易、3 中档、4 较难、5 压轴；“难一点”“简单一点”在 current.difficulty 上加一或减一
- count：要出几道（1～20）；“一道”“再来一道”是 1
- grade：用户说了高一、高二或高三才填
- topic：考查的知识点或章节，用教材里的叫法（比如“椭圆的离心率”“利用导数研究函数单调性”）；解题方法（分类讨论、数形结合）和情境不是知识点，不填在这里；在前文的题基础上改的，沿用那道题的知识点
- purpose：课堂练习、课后作业、单元测验或高考复习
- requirement：给命题员的补充要求。命题员看不到前面的对话，所以要把这句话连同前文里相关的内容（讨论过的方法、情境、要避开的题、要在哪道题基础上改）写成一段完整、独立的要求；题型、难度、数量这些已经单独提取的设置不用再写；这句话本身已经说清楚、前文也没有相关内容的，照抄原话；没有任何额外要求时填空字符串。
只输出 JSON。"""

ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["chat", "generate", "paper", "import"]},
        "reply": {"type": "string"},
        "needs_reasoning": {"type": "boolean"},
        "generate": {
            "type": ["object", "null"],
            "properties": {
                "question_type": {"type": ["string", "null"], "enum": [*TYPES, None]},
                "difficulty": {"type": ["integer", "null"], "minimum": 1, "maximum": 5},
                "count": {"type": ["integer", "null"], "minimum": 1, "maximum": 20},
                "grade": {"type": ["string", "null"], "enum": [*GRADES, None]},
                "topic": {"type": ["string", "null"]},
                "purpose": {"type": ["string", "null"], "enum": [*PURPOSES, None]},
                "requirement": {"type": "string"},
            },
        },
    },
    "required": ["intent", "reply", "needs_reasoning", "generate"],
}

REPLY_INSTRUCTIONS = """你是 Tetra，一个高中数学老师的智能助手。请回答用户这条消息（message）；summary 和 history 是这个对话的前文（summary 是更早内容的摘要），bank 是题库的概况。
- 解题、证明、计算：写出思路和关键步骤，结论要正确；只用高中阶段的知识和方法（不用洛必达法则、泰勒展开、定积分之类超出高中的方法），像老师给学生讲题那样清楚、简洁。
- 讲知识点、教学建议：条理清楚，结合高考和平时考试的实际，给得出具体的例子。
- 打招呼、闲聊：简短自然，可以顺带说说你能做什么（按要求出题并逐道审核、从题库自动组卷、解答数学问题）。
数学公式用 LaTeX，行内写在 $…$ 里，单独一行的写在 $$…$$ 里；需要时可以用 Markdown 的标题、列表和加粗。用 reply_language 指定的语言回答（中文或 English），不要客套话。
只输出 JSON：{"reply": "回答的全文"}。"""

REPLY_SCHEMA = {"type": "object", "properties": {"reply": {"type": "string"}}, "required": ["reply"]}

# 用户附上了图片、文件时，回答前要先看清内容
ATTACHED_NOTE = """
这次用户附上了图片或文件：随消息发来的图片是图片本身或 PDF 的页面（最多前 8 页，pages 是一共几页），files 是文本文件的内容。
先认真看清里面的内容（题目的条件、数字、图形、公式），再回答。要解题时，先用 LaTeX 把题目原样写出来（选择题连同选项），再写解答；有好几道题就逐道来。看不清或者缺了条件的地方直说，不要猜。"""

READ_INSTRUCTIONS = """把图片或文件里的数学题原样写出来：条件、数字、小问一个都不能漏，公式用 LaTeX 写在 $…$ 里，选择题连同选项；有好几道就都写出来。不要解题。
question_type 写（第一道）题的题型：single_choice 单选、multiple_choice 多选、fill_blank 填空、open_response 解答；
difficulty 估计它的难度 1～5（1 基础、3 中档、5 压轴）。
只输出 JSON：{"questions": "题目全文", "question_type": "...", "difficulty": 3}"""
READ_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {"type": "string"},
        "question_type": {"type": "string", "enum": list(TYPES)},
        "difficulty": {"type": "integer", "minimum": 1, "maximum": 5},
    },
    "required": ["questions", "question_type", "difficulty"],
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(text: object, size: int) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= size else f"{value[:size]}…"


def bank_summary(items: list[dict]) -> dict:
    """题库概况：一共几道、各册各题型几道（回答"题库里有什么"用）。"""
    by_volume: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for item in items:
        volume = (item.get("knowledge") or "").split(" › ")[0] or "未归类"
        by_volume[volume] = by_volume.get(volume, 0) + 1
        label = TYPES.get(item.get("question_type"), "其他")
        by_type[label] = by_type.get(label, 0) + 1
    return {"total": len(items), "by_volume": by_volume, "by_type": by_type}


def _normalize_generate(raw: object) -> dict:
    """模型提取的出题设置：不认识的值一律当作没说（None）。"""
    raw = raw if isinstance(raw, dict) else {}

    def number(key: str, low: int, high: int) -> int | None:
        try:
            value = int(raw.get(key))
        except (TypeError, ValueError):
            return None
        return value if low <= value <= high else None

    topic = str(raw.get("topic") or "").strip()
    return {
        "question_type": raw.get("question_type") if raw.get("question_type") in TYPES else None,
        "difficulty": number("difficulty", 1, 5),
        "count": number("count", 1, 20),
        "grade": raw.get("grade") if raw.get("grade") in GRADES else None,
        "topic": topic[:40] or None,
        "purpose": raw.get("purpose") if raw.get("purpose") in PURPOSES else None,
        "requirement": str(raw.get("requirement") or "").strip()[:1500],
    }


class Assistant:
    def __init__(self, models, store, model_runs, uploads=None) -> None:
        self.models = models
        self.store = store
        self.model_runs = model_runs
        self.uploads = uploads  # attachments.Uploads：聊天里的附件
        # 每个对话一把锁：自动压缩、手动压缩、发下一句前的压缩不会同时做两次
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock(self, session_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(session_id, threading.Lock())

    def recorder(self, usage: list) -> JobRecorder:
        def listen(event: str, call: dict, updates: dict) -> None:
            if event == "end":
                usage.append(updates)

        return JobRecorder(self.model_runs, listen, lane="A", role_of=lambda _stage: "assistant")

    def vision_route(self, model_id: str | None):
        """看图用的模型：选的模型能看图就用它，不能就换一个能看图的；一个都没有时说清楚。"""
        from mingti.attachments import AttachmentError
        from mingti.importer import is_vision

        route = self.models.route(model_id)
        if is_vision(route.model):
            return route
        option = next((o for o in self.models.options if is_vision(o.model)), None)
        if option is None:
            raise AttachmentError("现在接入的模型都看不了图片：请在「个人设置 → 模型服务」里接入一个能看图的模型（比如 deepseek-flash）")
        return self.models.route(option.id)

    def _ask(self, route, *, stage: str, instructions: str, payload: dict, schema: dict, max_tokens: int,
             thinking: bool | str, usage: list, images: list[str] | None = None) -> dict:
        def call(think: bool | str) -> dict:
            with self.recorder(usage).track(
                feature="assistant",
                provider=route.provider,
                model=route.model,
                prompt_version=f"{PROMPT_REVISION}:{stage}",
            ) as run:
                raw = route.client.request(
                    instructions=instructions,
                    input_text=json.dumps(payload, ensure_ascii=False),
                    action=f"回答（{stage}）",
                    output_schema=schema,
                    max_tokens=max_tokens,
                    recovery_instruction="只修复 JSON 结构，回答内容不变。",
                    thinking=think,
                    image_data_urls=images or None,
                )
                run.capture_response(raw)
            return raw

        try:
            raw = call(thinking)
        except DeepSeekClientError as exc:
            # 带图片时，有的模型不支持同时打开思考：关掉思考再试一次
            if not (images and thinking and "HTTP 400" in str(exc)):
                raise
            raw = call(False)
        result = json.loads(DeepSeekQuestionAuthoringProvider._extract_output_text(raw))
        if not isinstance(result, dict):
            raise ValueError("顶层结果必须是 JSON 对象")
        return result

    def attachment_cards(self, attachments: list[str]) -> list[dict]:
        """存进聊天记录的附件信息：名字、种类、页数、缩略图（重新打开对话时显示在你的那句话上面）。"""
        if not attachments or not self.uploads:
            return []
        return [self.uploads.public(item) for item in self.uploads.get(attachments)]

    def _read_questions(self, attachments: list[str], model_id: str | None, usage: list) -> dict:
        """把附件里的题目原样读出来（照着出类似题时用）：{questions, question_type, difficulty}；读不出来返回 {}。"""
        try:
            images, _ = self.uploads.page_images(attachments, limit=4)
            texts = self.uploads.texts(attachments)
            if not images and not texts:
                return {}
            read = self._ask(
                self.vision_route(model_id) if images else self.models.route(model_id),
                stage="assistant-read", instructions=READ_INSTRUCTIONS, payload={"files": texts},
                schema=READ_SCHEMA, max_tokens=3000, thinking=False, usage=usage, images=images,
            )
            return {**read, "questions": str(read.get("questions") or "").strip()[:1200]}
        except Exception:  # noqa: BLE001 - 读不出来就只按用户的话出题
            logger.exception("没能读出附件里的题目")
            return {}

    def compress(self, session_id: str, model_id: str | None, *, auto: bool = False) -> dict | None:
        """把这个对话前面的内容压缩成摘要，存进对话；返回存好的摘要记录。
        auto：只在上下文超额时才压缩（没超额返回 None）；手动压缩时没什么可压的会抛 NothingToCompress。"""
        with self._lock(session_id):
            entries = self.store.session(session_id)
            if auto and not context.needs_compress(entries):
                return None
            usage: list[dict] = []
            summary = context.compress(
                entries, session_id=session_id, route=self.models.route(model_id), recorder=self.recorder(usage),
            )
            return self.store.save_message(summary)

    def respond(
        self,
        *,
        text: str,
        session_id: str,
        model_id: str | None,
        thinking: str,
        touched: bool,
        current: dict,
        profile: dict,
        bank: dict,
        attachments: list[str] | None = None,
        language: str = "zh",
    ) -> dict:
        """判断这句话的意图。聊天：回答好、存进对话，返回 {intent: "chat", message}；
        出题：返回 {intent: "generate", reply, generate}；组卷：返回 {intent: "paper", reply}；
        把附件里的题存进题库：返回 {intent: "import", reply}（由 /api/chat-imports 去识别、补解析、给用户核对）。
        都带上 context（用了多少上下文）；发之前自动压缩过的，再带上 compressed（摘要记录）。
        attachments：附件编号（attachments.Uploads）；language：界面语言，en 时回答用英文。"""
        route = self.models.route(model_id)
        started = time.time()
        usage: list[dict] = []
        attachments = list(attachments or [])
        files = self.uploads.summary(attachments) if attachments and self.uploads else []
        entries = self.store.session(session_id) if session_id else []
        compressed = None
        if context.needs_compress(entries):
            try:
                compressed = self.compress(session_id, model_id, auto=True)
                entries = self.store.session(session_id)
            except Exception:  # noqa: BLE001 - 压缩失败就照常带最近的几轮，不耽误这一句
                logger.exception("对话 %s 自动压缩上下文失败", session_id)
        past = context.build(entries)
        today = datetime.now()
        # 不常变的放前面（个人设置、前文），模型服务能复用上一次的缓存；这一句和当前选项放最后
        payload = {
            "user": profile,
            "summary": past["summary"],
            "history": past["lines"],
            "bank": bank,
            "today": f"{today:%Y-%m-%d}",
            "current": current,
            "touched": touched,
            "write_reply": thinking == "low" and not files,
            "reply_language": "English" if language == "en" else "中文",
            "attachments": files,
            "message": text or ("（只发了附件，没有写字）" if files else ""),
        }
        decided = self._ask(
            route, stage=ROUTE_STAGE, instructions=ROUTE_INSTRUCTIONS, payload=payload, schema=ROUTE_SCHEMA,
            max_tokens=2500, thinking=False, usage=usage,
        )
        intent = decided.get("intent") if decided.get("intent") in ("chat", "generate", "paper", "import") else "chat"
        if intent == "import" and not files:
            intent = "chat"
        reply = str(decided.get("reply") or "").strip()
        extra = {"compressed": compressed}
        if intent == "import":
            return {"intent": intent, "reply": reply, **extra}
        if intent == "generate":
            settings = _normalize_generate(decided.get("generate"))
            if files:
                # 照着附件里的题出类似的：先把那道题读出来，写进给命题员的要求；没说题型、难度的，和原题一样
                seen = self._read_questions(attachments, model_id, usage)
                if seen.get("questions"):
                    settings["requirement"] = f"{settings['requirement']}\n参考题目（用户上传的）：{seen['questions']}".strip()[:1800]
                if not settings["question_type"] and seen.get("question_type") in TYPES:
                    settings["question_type"] = seen["question_type"]
                if not settings["difficulty"] and str(seen.get("difficulty")) in ("1", "2", "3", "4", "5"):
                    settings["difficulty"] = int(seen["difficulty"])
            return {"intent": intent, "reply": reply, "generate": settings, **extra}
        if intent == "paper":
            return {"intent": intent, "reply": reply, **extra}

        # 聊天：按思考强度另外请模型回答；只有选"低"、不用解题、没有附件时，直接用判断时写好的回答
        think = CHAT_THINKING.get(thinking, "light")
        if files or think or decided.get("needs_reasoning") or not reply:
            asked = {k: payload[k] for k in ("user", "summary", "history", "bank", "today", "reply_language", "message")}
            images: list[str] = []
            reply_route, instructions = route, REPLY_INSTRUCTIONS
            if files:
                images, pages = self.uploads.page_images(attachments)
                asked.update(files=self.uploads.texts(attachments), pages=pages, pages_shown=len(images))
                instructions = REPLY_INSTRUCTIONS + ATTACHED_NOTE
                if images:
                    reply_route = self.vision_route(model_id)
            answered = self._ask(
                reply_route, stage=REPLY_STAGE, instructions=instructions, payload=asked,
                schema=REPLY_SCHEMA, max_tokens=6000 if files else 4000, thinking=think, usage=usage, images=images,
            )
            reply = str(answered.get("reply") or "").strip() or reply
            route = reply_route
        if not reply:
            raise ValueError("模型没有写出回答")
        message = {
            "id": f"m_{uuid.uuid4().hex[:12]}",
            "session_id": session_id,
            "created_at": _now(),
            "kind": "chat",
            "title": _clip(text or "、".join(f["name"] for f in files), 40),
            "text": text,
            "attachments": self.attachment_cards(attachments),
            "reply": reply,
            "model": route.id,
            "thinking": thinking if think else "low",
            "seconds": round(time.time() - started, 1),
            "calls": len(usage),
            "tokens": sum(u.get("input_tokens", 0) + u.get("output_tokens", 0) for u in usage),
        }
        self.store.save_message(message)
        return {"intent": "chat", "message": message, **extra}
