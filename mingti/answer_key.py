"""把审核通过的完整推导整理成标准答案式的解析。

出题流程里的解题员按"讲题"的要求写，步骤多、说明多；试卷后面的参考答案要短。
所以题目通过全部检查之后，再请"整理员"调一次模型做压缩改写：思路和数值都来自已经核对过的完整推导，
最后一句"故选…/故答案为…"由程序按核对过的答案补上，保证答案不会在改写里变掉。
完整推导保留在 full_solution_method / full_solution_steps 里，界面上可以展开看。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Callable

from mingti.question_authoring import PROMPT_REVISION, DeepSeekQuestionAuthoringProvider
from mingti.question_authoring.scope import beyond_curriculum_methods

logger = logging.getLogger("mingti.answer_key")

STAGE = "solution-condense"
# 解析详略 → 改写成什么样；"详细"保留完整推导，不改写
STYLES = {
    "concise": (
        "考试参考答案里的【解析】",
        "只写得出答案必需的推理和计算，一般 3～6 行，很简单的题一两行就够。"
        "method 是【分析】：一句话点明思路（不超过 30 字），没有必要时留空字符串。",
    ),
    "standard": (
        "教辅书上的【分析】【详解】",
        "写出关键式子和依据，一般 4～10 行。method 是【分析】：一两句话概括思路（不超过 60 字），"
        "解答题按小问分别概括，如“(1)……；(2)……”。",
    ),
}
MAX_LINES = 16
SCHEMA = {
    "type": "object",
    "properties": {
        "method": {"type": "string"},
        "steps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["method", "steps"],
}
# 结论句由程序补，模型写了也去掉，免得重复
_CLOSING = re.compile(r"^(故|所以|因此|综上[，,]?)\s*(选|答案为|答案是|应选)")

Call = Callable[[str, str, dict, dict, int], dict]


def closing_line(draft: dict) -> str:
    answer = str(draft.get("answer_value") or "").strip()
    if draft.get("question_type") in ("single_choice", "multiple_choice"):
        # 试卷上多选的答案写成"ABC"，不写"A,B,C"
        letters = "".join(re.findall(r"[A-H]", answer.upper()))
        answer = letters or answer
        return f"故选：{answer}" if answer else ""
    if draft.get("question_type") == "fill_blank":
        final = str(draft.get("final_answer") or answer).strip().rstrip("。")
        return f"故答案为：{final}" if final else ""
    return ""


def condense(draft: dict, *, detail: str, call: Call) -> dict:
    """返回改写后的新 dict；改写失败或不需要改写时原样返回。"""
    if detail not in STYLES or not draft.get("solution_steps"):
        return draft
    style, length = STYLES[detail]
    instructions = (
        f"你是高中数学试卷的答案编写人。下面是一道题、核对过的答案和一份完整但冗长的推导。"
        f"请按标准试卷解析的格式，把推导改写成{style}那样的解析：\n"
        "1. steps 的每一项是解析里的一行：一句推理或一个算式。行与行之间用“因为……所以……”“由……得……”"
        "“则”“又”“故”这类词衔接，连起来读是一段连贯的解答。\n"
        "2. 不要写“第一步”“步骤 1”“1.”“①”这类编号，也不要写小标题。\n"
        "3. 解答题每一问另起一行，以“(1)”“(2)”开头，这一问后面的行不再重复编号；每一问的结论写在这一问的最后一行。\n"
        "4. 需要分类讨论时，每一类单独一行，以“当……时，”开头；立体几何可以写“如图所示”“建立如图所示的空间直角坐标系”。\n"
        "5. 只保留得出答案必需的推理和计算；删掉复述题意、“目标是……”、“验算……”、“这一步说明……”、易错提醒之类的话。\n"
        "6. 沿用原推导的解法和数值，不引入新的结论，不改动答案。\n"
        f"7. {length}\n"
        "8. 公式用 $…$ 包起来，写法和原推导保持一致。\n"
        "9. 选择题和填空题不要写最后一句“故选……”或“故答案为……”，程序会按核对过的答案补上。\n"
        "只输出 JSON。"
    )
    payload = {
        "question_type": draft.get("question_type"),
        "question_statement": {"stem": draft.get("stem_latex") or draft.get("stem_plain"), "options": draft.get("options") or []},
        "verified_answer": draft.get("answer_value"),
        "verified_final_answer": draft.get("final_answer"),
        "full_solution": {"method": draft.get("solution_method") or "", "steps": draft.get("solution_steps") or []},
    }
    try:
        result = call(STAGE, instructions, payload, SCHEMA, 2500)
    except Exception as exc:  # noqa: BLE001 - 整理不成功就保留完整推导，不影响出题结果
        logger.warning("解析整理失败，保留完整推导：%s", exc)
        return draft
    steps = [str(step).strip() for step in result.get("steps") or [] if str(step).strip()]
    steps = [step for step in steps if not _CLOSING.match(step)]
    if not steps or len(steps) > MAX_LINES:
        logger.warning("解析整理的结果不合要求（%d 步），保留完整推导", len(steps))
        return draft
    if beyond_curriculum_methods(str(result.get("method") or ""), *steps):
        logger.warning("解析整理时用了高中不学的方法，保留完整推导")
        return draft
    closing = closing_line(draft)
    if closing:
        steps.append(closing)
    condensed = dict(draft)
    condensed["full_solution_method"] = draft.get("solution_method") or ""
    condensed["full_solution_steps"] = list(draft.get("solution_steps") or [])
    condensed["solution_method"] = str(result.get("method") or "").strip()
    condensed["solution_steps"] = steps
    condensed["solution_detail"] = detail
    return condensed


def model_call(route, recorder) -> Call:
    """把一个角色的模型和调用记录器包成 condense 需要的 call。"""

    def call(stage: str, instructions: str, input_value: dict, schema: dict, max_tokens: int) -> dict:
        with recorder.track(
            feature="question_authoring",
            provider=route.provider,
            model=route.model,
            prompt_version=f"{PROMPT_REVISION}:{stage}",
        ) as run:
            raw = route.client.request(
                instructions=instructions,
                input_text=json.dumps(input_value, ensure_ascii=False),
                action=f"{'配图' if stage.startswith('figure') else '整理解析'}（{stage}）",
                output_schema=schema,
                max_tokens=max_tokens,
                recovery_instruction="只修复 JSON 结构。",
            )
            run.capture_response(raw)
        payload = json.loads(DeepSeekQuestionAuthoringProvider._extract_output_text(raw))
        if not isinstance(payload, dict):
            raise ValueError("顶层结果必须是 JSON 对象")
        return payload

    return call
