"""对话的上下文：助手回答问题、判断出题要求、定组卷方案时带上的前文。

- 前文按"轮"算：一问一答、一道题（要求 + 出好的题）、一批题、一次组卷各算一轮。
- 带多少：从最近的一轮往前放，放到 LIMIT（约 2.4 万 Token）为止；压缩过的话，更早的内容用摘要代替。
- 压缩：把最近 KEEP 轮以前的内容请模型写成一段摘要，存成对话里的一条记录（store.messages，kind = "summary"，
  时间记在被压缩的最后一轮之后，所以在对话里正好排在分界处）。以后带"摘要 + 摘要之后各轮的原文"。
  前文超过 LIMIT 的 AUTO（85%）时，发下一句前自动压缩；也可以在界面上手动压缩。
- Token 是估算的（中文每字约 0.65 个，其余每 3.6 个字符 1 个），只用来显示和决定什么时候压缩。
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone

from mingti.question_authoring import PROMPT_REVISION, DeepSeekQuestionAuthoringProvider

LIMIT = 24000
AUTO = 0.85
KEEP = 4
STAGE = "context-compress"
TYPES = {"single_choice": "单选题", "multiple_choice": "多选题", "fill_blank": "填空题", "open_response": "解答题"}
LEVELS = {1: "基础", 2: "较易", 3: "中档", 4: "较难", 5: "压轴"}
_WIDE = re.compile(r"[　-〿㐀-鿿豈-﫿＀-￯]")

SUMMARY_INSTRUCTIONS = """你在帮一个高中数学出题助手整理对话记录。下面是老师和助手之前的对话（previous_summary 是更早内容的摘要，可能为空）。
请把它们压缩成一段摘要，供之后继续对话时参考：
- 保留：老师的身份、年级、教材和偏好；讨论过的知识点、方法和结论；出过的题（题型、考点、难度、题干要点和答案）；组过的试卷（名称、结构）；老师提过但还没完成的要求。
- 删掉：寒暄、重复的内容、助手的客套话。
- 用中文，条理清楚，可以分几行；公式用 $…$；不超过 800 字。
只输出 JSON：{"summary": "摘要全文"}。"""

SUMMARY_SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}


class NothingToCompress(ValueError):
    pass


def estimate_tokens(text: object) -> int:
    value = str(text or "")
    wide = len(_WIDE.findall(value))
    return int(wide * 0.65 + (len(value) - wide) / 3.6) + 1


def _clip(text: object, size: int) -> str:
    value = str(text or "").strip()
    return value if len(value) <= size else f"{value[:size]}…"


def _when(stamp: str | None) -> datetime:
    try:
        moment = datetime.fromisoformat(str(stamp))
    except ValueError:
        return datetime(2000, 1, 1, tzinfo=timezone.utc)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _question_text(record: dict) -> str:
    request = record.get("request") or {}
    brief = request.get("brief") or {}
    result = record.get("result") or {}
    if record.get("status") != "succeeded" or not result:
        return f"这道题没出成：{_clip(record.get('error'), 160)}"
    kind = TYPES.get(result.get("question_type") or brief.get("question_type"), "题")
    level = brief.get("target_difficulty") or 3
    stem = result.get("stem_latex") or result.get("stem_plain") or ""
    options = "；".join(f"{o.get('key')}. {o.get('text')}" for o in result.get("options") or [])
    answer = result.get("final_answer") or result.get("answer_value") or ""
    solution = " ".join([result.get("solution_method") or "", *(result.get("solution_steps") or [])])
    parts = [
        f"出了一道{kind}（{record.get('knowledge') or '考点不限'}，{LEVELS.get(level, '中档')}）：{_clip(stem, 1500)}",
        f"选项：{_clip(options, 500)}" if options else "",
        f"答案：{_clip(answer, 200)}",
        f"解析：{_clip(solution, 700)}" if solution.strip() else "",
    ]
    return "\n".join(part for part in parts if part)


def _ask_text(record: dict) -> str:
    request = record.get("request") or {}
    brief, meta = request.get("brief") or {}, request.get("meta") or {}
    said = meta.get("said") or meta.get("follow_up") or ""
    note = meta.get("note") or ""
    return "\n".join(part for part in (said, brief.get("request_text"), note if note != said else "") if part)


def turns(entries: list[dict]) -> list[dict]:
    """对话里的内容 → 一轮一轮：{kind, at, end, lines: [{role, content}], tokens}。
    同一批出的题合成一轮；组卷时现场出的题算在组卷那一轮里，不单列。"""
    grouped: list[dict] = []
    batches: dict[str, dict] = {}
    for entry in entries:
        kind = entry.get("kind") or "question"
        at = entry.get("created_at") or ""
        if kind == "summary":
            grouped.append({"kind": "summary", "at": at, "end": at, "entry": entry,
                            "lines": [{"role": "summary", "content": entry.get("text") or ""}]})
            continue
        files = "、".join(item.get("name", "") for item in entry.get("attachments") or [])
        attached = f"（附件：{files}）" if files else ""
        if kind == "chat":
            lines = [{"role": "user", "content": _clip(f"{entry.get('text') or ''}{attached}", 3000)},
                     {"role": "assistant", "content": _clip(entry.get("reply"), 4000)}]
            grouped.append({"kind": kind, "at": at, "end": at, "lines": lines})
            continue
        if kind == "import":
            found = entry.get("questions") or []
            done = {"saved": f"存进题库 {entry.get('saved', 0)} 道", "declined": "用户没有存", "review": "等用户核对后决定存不存"}
            stems = "；".join(_clip(q.get("stem"), 120) for q in found[:6])
            reply = f"从附件里识别出 {len(found)} 道题（{done.get(entry.get('status'), '没识别出题目')}）：{stems}"
            grouped.append({"kind": kind, "at": at, "end": at,
                            "lines": [{"role": "user", "content": _clip(f"{entry.get('text') or '把题存进题库'}{attached}", 600)},
                                      {"role": "assistant", "content": _clip(reply, 1500)}]})
            continue
        if kind == "paper":
            paper = entry.get("paper") or {}
            slots = "；".join(
                f"{slot.get('no')}. {TYPES.get(slot.get('type'), '')} {slot.get('topic') or ''}（{LEVELS.get(slot.get('difficulty'), '')}）"
                for slot in entry.get("slots") or []
            )
            reply = (f"组好了一份试卷《{paper.get('title') or entry.get('title')}》：{paper.get('count', 0)} 道题，"
                     f"满分 {paper.get('total', 0)} 分。题目：{_clip(slots, 1200)}") if paper else f"组卷没完成：{_clip(entry.get('error'), 200)}"
            grouped.append({"kind": kind, "at": at, "end": at,
                            "lines": [{"role": "user", "content": _clip(entry.get("text"), 600)}, {"role": "assistant", "content": reply}]})
            continue
        meta = (entry.get("request") or {}).get("meta") or {}
        if meta.get("assembly_id"):
            continue
        batch = meta.get("batch_id")
        if batch:
            group = batches.get(batch)
            if group is None:
                group = {"kind": "batch", "at": at, "end": at, "records": []}
                batches[batch] = group
                grouped.append(group)
            group["records"].append(entry)
            group["end"] = max(group["end"], at, key=_when)
            continue
        grouped.append({"kind": "question", "at": at, "end": at,
                        "lines": [{"role": "user", "content": _clip(_ask_text(entry), 800)},
                                  {"role": "assistant", "content": _question_text(entry)}]})
    for group in grouped:
        if group["kind"] == "batch":
            records = group.pop("records")
            done = [record for record in records if record.get("status") == "succeeded" and record.get("result")]
            stems = "\n".join(
                f"{index}. {_clip((record.get('result') or {}).get('stem_latex') or '', 220)} 答案：{_clip((record.get('result') or {}).get('final_answer') or (record.get('result') or {}).get('answer_value'), 80)}"
                for index, record in enumerate(done, start=1)
            )
            group["lines"] = [
                {"role": "user", "content": _clip(_ask_text(records[0]), 800)},
                {"role": "assistant", "content": f"出了一批题，出好 {len(done)} 道、没出成 {len(records) - len(done)} 道：\n{stems}".strip()},
            ]
        group["tokens"] = sum(estimate_tokens(line["content"]) + 4 for line in group["lines"])
    return grouped


def _split(entries: list[dict]) -> tuple[dict | None, list[dict], int]:
    """（最近一次的摘要，摘要之后的各轮，摘要一共概括了几轮）"""
    all_turns = turns(entries)
    summaries = [turn for turn in all_turns if turn["kind"] == "summary"]
    summary = summaries[-1] if summaries else None
    if summary is None:
        return None, all_turns, 0
    until = _when(summary["entry"].get("covers_until") or summary["at"])
    after = [turn for turn in all_turns if turn["kind"] != "summary" and _when(turn["at"]) > until]
    return summary, after, int(summary["entry"].get("covers_count") or 0)


def build(entries: list[dict], limit: int = LIMIT) -> dict:
    """要带给模型的前文：{summary, lines, tokens, limit, turns（带了几轮原文）, summarized（摘要概括了几轮）, dropped（放不下没带的轮数）}。"""
    summary, after, summarized = _split(entries)
    used = summary["tokens"] if summary else 0
    included: list[dict] = []
    for turn in reversed(after):
        if included and used + turn["tokens"] > limit:
            break
        included.insert(0, turn)
        used += turn["tokens"]
    return {
        "summary": summary["lines"][0]["content"] if summary else None,
        "lines": [line for turn in included for line in turn["lines"]],
        "tokens": used,
        "limit": limit,
        "turns": len(included),
        "summarized": summarized,
        "dropped": len(after) - len(included),
    }


def stats(entries: list[dict]) -> dict:
    """界面上的上下文用量：Token 估算、上限、带了几轮、摘要概括了几轮、能不能再压缩。"""
    view = build(entries)
    _, after, _ = _split(entries)
    return {
        "tokens": view["tokens"],
        "limit": LIMIT,
        "auto": AUTO,
        "keep": KEEP,
        "turns": view["turns"],
        "summarized": view["summarized"],
        "dropped": view["dropped"],
        "compressible": len(after) > KEEP,
        # 超额了（超过 AUTO，或者有放不下的轮次）、而且有能压缩的：界面马上自动压缩
        "needs": (view["tokens"] > LIMIT * AUTO or view["dropped"] > 0) and len(after) > KEEP,
    }


def needs_compress(entries: list[dict]) -> bool:
    view = build(entries)
    _, after, _ = _split(entries)
    return (view["tokens"] > LIMIT * AUTO or view["dropped"] > 0) and len(after) > KEEP


def compress(entries: list[dict], *, session_id: str, route, recorder, keep: int = KEEP) -> dict:
    """把最近 keep 轮以前的内容（连同上一次的摘要）压缩成一段新摘要，返回要存的记录（kind = "summary"）。"""
    summary, after, summarized = _split(entries)
    older = after[:-keep] if keep else after
    if not older:
        raise NothingToCompress("对话还很短，不用压缩")
    before_tokens = (summary["tokens"] if summary else 0) + sum(turn["tokens"] for turn in older)
    payload = {
        "previous_summary": summary["lines"][0]["content"] if summary else "",
        "conversation": [line for turn in older for line in turn["lines"]],
    }
    with recorder.track(feature="assistant", provider=route.provider, model=route.model,
                        prompt_version=f"{PROMPT_REVISION}:{STAGE}") as run:
        raw = route.client.request(
            instructions=SUMMARY_INSTRUCTIONS,
            input_text=json.dumps(payload, ensure_ascii=False),
            action=f"压缩上下文（{STAGE}）",
            output_schema=SUMMARY_SCHEMA,
            max_tokens=2000,
            recovery_instruction="只修复 JSON 结构。",
        )
        run.capture_response(raw)
    result = json.loads(DeepSeekQuestionAuthoringProvider._extract_output_text(raw))
    text = str((result or {}).get("summary") or "").strip()
    if not text:
        raise ValueError("模型没有写出摘要")
    until = max((_when(turn["end"]) for turn in older), default=datetime.now(timezone.utc))
    return {
        "id": f"m_{uuid.uuid4().hex[:12]}",
        "session_id": session_id,
        # 记在被压缩的最后一轮之后：对话里这条分界线正好排在压缩和没压缩的内容之间
        "created_at": (until + timedelta(microseconds=1)).isoformat(),
        "kind": "summary",
        "title": "上下文摘要",
        "text": text,
        "covers_until": until.isoformat(),
        "covers_count": summarized + len(older),
        "tokens_before": before_tokens,
        "tokens_after": estimate_tokens(text),
    }
