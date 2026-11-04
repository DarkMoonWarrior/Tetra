"""给 ablation.py 出的题判分。

1. 答案对不对：评判模型只看题面独立解 3 次，3 次里至少 2 次和交付的答案一致就算对（多数表决）；
   至少 2 次一致、但和交付答案不同算错；3 次各不相同算"说不准"。
2. 题目成不成立：3 次里至少 2 次认为条件矛盾、条件不足或答案不唯一，记为"题目不成立"。
3. 考点和难度：对照命题要求，看题目是不是考了要求的内容，再按 1–5 星估一个难度。

    .venv/Scripts/python eval/judge.py pilot [--model deepseek-v4-pro] [--workers 6]

结果追加到 eval/results/<pilot|full>/judge.jsonl，一行对应 runs.jsonl 里出成的一道题，停了可以接着跑。
评判默认用 deepseek-v4-pro：和出题用的 Flash 不是同一个模型，两边的错不容易撞在一起。
"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

from common import OUT_OF_BALANCE, RESULTS, JsonlWriter, call_cost, latest, patient_client, read_jsonl, usage_of

from mingti.config import load_settings
from mingti.deepseek import DeepSeekClientError
from mingti.math_content import MATH_DISPLAY_INSTRUCTIONS

TYPE_LABEL = {
    "single_choice": "单选题（恰有一个正确选项）",
    "multiple_choice": "多选题（有不止一个正确选项）",
    "fill_blank": "填空题",
    "open_response": "解答题",
}
DIFFICULTY_LABEL = {1: "基础", 2: "较易", 3: "中档", 4: "较难", 5: "压轴"}

SOLVE_PROMPT = (
    "你是高中数学老师。下面是一道题的题面（选择题附选项），请独立把它完整解一遍：在内部推导，只输出结果。"
    "如果题目条件互相矛盾、条件不足、结论不唯一，或者单选题不是恰有一个正确选项、多选题没有正确选项，"
    "well_posed 写 false，issue 用一句话说明问题；否则 well_posed 写 true，issue 写空字符串。"
    "选择题在 choice_keys 里写出全部正确选项的字母；填空题和解答题 choice_keys 写空数组。"
    "final_answer 按小问顺序写出每一问的最终结果，证明题写“结论成立”，不写过程。"
    "只能用高中数学知识求解。" + MATH_DISPLAY_INSTRUCTIONS
)
SOLVE_SCHEMA = {
    "type": "object",
    "properties": {
        "well_posed": {"type": "boolean"},
        "issue": {"type": "string"},
        "final_answer": {"type": "string"},
        "choice_keys": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["well_posed", "issue", "final_answer", "choice_keys"],
}

GROUP_PROMPT = (
    "下面是同一道高中数学题的 4 个答案，编号 0 到 3。按最终结果在数学上是否等价给它们分组："
    "逐问比较每一问的结论（数值、表达式、取值范围、存在与否、选了哪些选项）；"
    "写法不同（分数和小数、是否化简、区间和不等式、多写了几句说明）算等价，有一问的结论不同就不等价；"
    "证明题的小问，两边都认为结论成立就算等价。先两两比较，再编组号。"
    "groups 是长度为 4 的整数数组，第 i 个数是答案 i 的组号：等价的答案组号必须相同，组号从 0 开始编。"
    "reason 用一两句话说明分组依据。"
)
GROUP_SCHEMA = {
    "type": "object",
    "properties": {"groups": {"type": "array", "items": {"type": "integer"}}, "reason": {"type": "string"}},
    "required": ["groups", "reason"],
}

MATCH_PROMPT = (
    "你在检查一道自动生成的高中数学题是否符合命题要求。命题要求给出了题型、题目内容和目标难度。"
    "topic_match：题目考的内容和要求一致写“符合”；考了相关的知识点，但情境、方法或设问和要求明显不同写“部分符合”；"
    "考的不是要求的内容写“不符合”。"
    "difficulty：按高考题的标准估计这道题实际的难度，1 基础（直接套用一个概念或公式）、2 较易（一两步常规推理）、"
    "3 中档（多步推理，或综合两个知识点）、4 较难（综合性强，需要转化、分类讨论或较长的运算）、"
    "5 压轴（高考压轴题水平，需要构造或较深的推理）。reason 用一两句话说明。"
)
MATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "topic_match": {"type": "string", "enum": ["符合", "部分符合", "不符合"]},
        "difficulty": {"type": "integer", "minimum": 1, "maximum": 5},
        "reason": {"type": "string"},
    },
    "required": ["topic_match", "difficulty", "reason"],
}


class Judge:
    def __init__(self, model: str) -> None:
        settings = load_settings()
        self.model = model
        self.client = patient_client(
            api_key=settings.deepseek_api_key, model=model, base_url=settings.deepseek_base_url, timeout_seconds=300
        )

    def ask(self, calls: list, stage: str, prompt: str, value: dict, schema: dict, max_tokens: int, thinking: bool) -> dict:
        for attempt in range(2):  # 网络出错重试一次
            started = time.time()
            try:
                raw = self.client.request(
                    instructions=prompt,
                    input_text=json.dumps(value, ensure_ascii=False),
                    action=f"评判（{stage}）",
                    output_schema=schema,
                    max_tokens=max_tokens,
                    thinking=thinking,
                )
            except DeepSeekClientError:
                if attempt == 1:
                    raise
                continue
            calls.append({"stage": stage, "model": self.model, "started": started,
                          "seconds": round(time.time() - started, 1), **usage_of(raw)})
            return json.loads(raw["output"][0]["content"][0]["text"])
        raise AssertionError("unreachable")


def question_view(question: dict) -> dict:
    view = {
        "题型": TYPE_LABEL[question["question_type"]],
        "题面": question.get("stem_plain"),
        "题面（LaTeX）": question.get("stem_latex") or "",
    }
    if question.get("options"):
        view["选项"] = {option["key"]: option["text"] for option in question["options"]}
    return view


def choice_keys(text: str, valid: set[str]) -> frozenset[str] | None:
    """答案是选项字母（"B" "A,C" "ACD"，或者字母后面跟括号说明，如 "B（55.5）"）时取出字母；
    夹着别的文字就返回 None，交给模型比。"""
    text = (text or "").strip().upper()
    match = re.fullmatch(r"(?:选\s*)?([A-H](?:\s*[,，、;；]?\s*[A-H])*)\s*(?:[（(].*)?", text, flags=re.DOTALL)
    if not match:
        return None
    keys = frozenset(re.findall(r"[A-H]", match.group(1)))
    return keys if keys <= valid else None


def delivered_keys(question: dict, valid: set[str]) -> frozenset[str] | None:
    """交付答案选了哪些选项。审题员有时把数值写进 answer_value、把字母写进 final_answer，两个字段都看。"""
    for text in (question.get("answer_value", ""), question.get("final_answer", "")):
        keys = choice_keys(text, valid)
        if keys is not None:
            return keys
    return None


def group_answers(judge: Judge, calls: list, question: dict, solves: list[dict | None]) -> tuple[list[int], str]:
    """把交付的答案（0 号）和 3 次重解（1–3 号）按是否等价分组。选择题能按字母比就按字母比。"""
    valid = {option["key"].strip().upper() for option in question.get("options") or []}
    if valid:
        delivered = delivered_keys(question, valid)
        if delivered is not None:
            sets = [delivered] + [
                frozenset(str(k).strip().upper() for k in (s.get("choice_keys") or []) if str(k).strip().upper() in valid)
                if s else None
                for s in solves
            ]
            groups, seen = [], []
            for index, keys in enumerate(sets):
                if not keys:  # 没解出来：单独一组
                    groups.append(100 + index)
                    continue
                if keys not in seen:
                    seen.append(keys)
                groups.append(seen.index(keys))
            return groups, "按选项字母比较"

    def text(answer: dict | None) -> str:
        if answer is None:
            return "（没有解出来）"
        keys = "".join(sorted(str(k) for k in answer.get("choice_keys") or []))
        return (f"选 {keys}；" if keys else "") + str(answer.get("final_answer", ""))

    delivered_text = question.get("final_answer") or ""
    if valid and question.get("answer_value") and question["answer_value"] not in delivered_text:
        delivered_text = f"{question['answer_value']}；{delivered_text}"
    result = judge.ask(
        calls, "group", GROUP_PROMPT,
        {"题目": question_view(question), "答案": [delivered_text] + [text(s) for s in solves]},
        GROUP_SCHEMA, 800, True,  # 不开思考时试过：理由里说等价，组号却给成两组
    )
    groups = [int(g) for g in result.get("groups", [])][:4]
    if len(groups) != 4:
        raise ValueError(f"分组结果长度不对：{result}")
    groups = [100 + i if solve is None and i else g for i, (g, solve) in enumerate(zip(groups, [None, *solves]))]
    return groups, str(result.get("reason", ""))


def verdict(groups: list[int], solves: list[dict | None]) -> tuple[str, int, int]:
    """返回（结论，几次重解和交付答案一致，几次认为题目不成立）。"""
    ill = sum(1 for s in solves if s is not None and s.get("well_posed") is False)
    agree = sum(1 for g, s in zip(groups[1:], solves) if s is not None and g == groups[0])
    if ill >= 2:
        return "ill_posed", agree, ill
    counts = Counter(g for g, s in zip(groups[1:], solves) if s is not None)
    top, size = counts.most_common(1)[0] if counts else (None, 0)
    if size < 2:
        return "uncertain", agree, ill
    return ("correct" if groups[0] == top else "wrong"), agree, ill


def judge_one(judge: Judge, run: dict) -> dict | None:
    if OUT_OF_BALANCE.is_set():
        return None  # 余额用完：剩下的题下次再评
    question = run["question"]
    calls: list[dict] = []
    view = question_view(question)
    spec = {
        "题型": TYPE_LABEL[run["question_type"]],
        "题目内容": run["topic"],
        "目标难度": f"{run['difficulty']}/5（{DIFFICULTY_LABEL[run['difficulty']]}）",
    }
    with ThreadPoolExecutor(max_workers=4) as pool:
        solve_futures = [
            pool.submit(judge.ask, calls, f"solve-{i}", SOLVE_PROMPT, {"题目": view}, SOLVE_SCHEMA, 4000, True)
            for i in (1, 2, 3)
        ]
        match_future = pool.submit(
            judge.ask, calls, "match", MATCH_PROMPT, {"命题要求": spec, "生成的题目": view}, MATCH_SCHEMA, 800, False
        )
        solves, errors = [], []
        for future in solve_futures:
            try:
                solves.append(future.result())
            except Exception as exc:  # noqa: BLE001 - 一次没解出来不影响另外两次
                solves.append(None)
                errors.append(f"{type(exc).__name__}: {exc}"[:200])
        match = match_future.result()
    groups, reason = group_answers(judge, calls, question, solves)
    result, agree, ill = verdict(groups, solves)
    return {
        "id": run["id"],
        "config": run["config"],
        "spec_id": run["spec_id"],
        "judge_model": judge.model,
        "verdict": result,
        "agree": agree,
        "ill_posed_votes": ill,
        "groups": groups,
        "group_reason": reason,
        "solves": solves,
        "solve_errors": errors,
        "topic_match": match.get("topic_match"),
        "judge_difficulty": match.get("difficulty"),
        "match_reason": match.get("reason"),
        "judge_calls": calls,
        "judge_cost_usd": round(sum(call_cost(c) for c in calls), 5),
        "judge_cost_usd_offpeak": round(sum(call_cost(c, peak=False) for c in calls), 5),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("which", choices=["pilot", "full"])
    parser.add_argument("--model", default="deepseek-v4-pro")
    parser.add_argument("--workers", type=int, default=6, help="同时评几道题（每道题内部再并行 4 个调用）")
    args = parser.parse_args()

    folder = RESULTS / args.which
    runs = [r for r in latest(read_jsonl(folder / "runs.jsonl")).values() if r["status"] == "succeeded" and r["question"]]
    done = set(latest(read_jsonl(folder / "judge.jsonl")))
    todo = [run for run in runs if run["id"] not in done]
    print(f"出成的题 {len(runs)} 道，已评 {len(done)} 道，这次评 {len(todo)} 道（评判模型 {args.model}）", flush=True)
    judge = Judge(args.model)
    writer = JsonlWriter(folder / "judge.jsonl")
    started = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(judge_one, judge, run): run for run in todo}
        for index, future in enumerate(as_completed(futures), 1):
            run = futures[future]
            try:
                row = future.result()
            except Exception as exc:  # noqa: BLE001 - 这道下次再评
                print(f"[{index}/{len(todo)}] {run['id']:<22} 评判出错：{type(exc).__name__}: {exc}"[:200], flush=True)
                continue
            if row is None:
                continue
            writer.append(row)
            print(
                f"[{index}/{len(todo)} {round((time.time() - started) / 60, 1)}分] {row['id']:<22} {row['verdict']:<9} "
                f"一致 {row['agree']}/3  考点 {row['topic_match']}  难度 {row['judge_difficulty']}  ${row['judge_cost_usd']:.4f}",
                flush=True,
            )
    if OUT_OF_BALANCE.is_set():
        print("DeepSeek 账户余额不足（HTTP 402），已经停下；充值后再运行同一条命令，会接着评没评完的。", flush=True)


if __name__ == "__main__":
    main()
