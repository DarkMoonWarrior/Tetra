"""对比（消融）实验：同一批命题要求，用不同的流程配置各出一遍题，记下成败、调用次数、Token、用时和费用。

在项目目录下用项目的 .venv 运行：
    .venv/Scripts/python eval/ablation.py small --budget 2.5   正式实验（精简版）：30 道题 × A、B、C、D 四种结构配置，约 ¥14
    .venv/Scripts/python eval/ablation.py pilot             试跑：10 道题 × 9 种配置
    .venv/Scripts/python eval/ablation.py full              完整版：60 道题 × 9 种配置，约 ¥118，嫌贵没跑
    .venv/Scripts/python eval/ablation.py small --configs C-think,C-gate   以后想补机制消融，可以只加几种配置

每出完一道题往 eval/results/<pilot|small|full>/runs.jsonl 追加一行；中途停了，再运行会接着跑没出完的。
出题直接用 mingti 里的 TeamProvider，和界面上的"快速出题"一样：一个方案，所有角色都用 DeepSeek Flash，
思考强度"高"；调用超过 70 次或超过 8 分钟就停下，算没出成。不经过网页服务，也不写应用自己的命题记录和题库。
题目通过检查之后的整理解析、配图两步不跑，它们不改题目和答案。
"""

from __future__ import annotations

import argparse
import json
import random
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

from common import (
    OUT_OF_BALANCE, RATE_LIMITED, RESULTS, ROOT, JsonlWriter, call_cost, latest, patient_client, read_jsonl, usage_of,
)

from mingti.config import CURRICULUM_CSV, load_settings
from mingti.curriculum import CsvCurriculumCatalog
from mingti.curriculum_tree import load_tree
from mingti.deepseek import DeepSeekClientError, DeepSeekJsonResponseError
from mingti.figures import FIGURE_RULE
from mingti.math_verifier import MathVerifier
from mingti.models import ModelRoute
from mingti.question_authoring import (
    QuestionAuthoring,
    QuestionAuthoringContext,
    QuestionAuthoringError,
    QuestionAuthoringProviderError,
    QuestionDesignBrief,
)
from mingti.question_authoring.providers import PipelineSwitches
from mingti.question_authoring.scope import beyond_curriculum_methods, university_only_terms
from mingti.team import ROLES, TeamProvider, role_of
from mingti.tracking import StopRequested

MODEL = "deepseek-flash"
SPENT = {"usd": 0.0}  # 这次运行已经花掉的钱，超过 --budget 就不再开始新的出题


@dataclass(frozen=True)
class Config:
    label: str
    switches: PipelineSwitches = field(default_factory=PipelineSwitches)
    verify: bool = False


# 结构消融：一层层加角色。机制消融：在 C 上每次只关掉一个 11 月 4 日加的机制。
CONFIGS = {
    "A": Config("A 只有命题员", PipelineSwitches(stop_after="compose")),
    "B": Config("B 加审题员", PipelineSwitches(stop_after="review")),
    "C": Config("C 加解题员、复核员（完整闭环）"),
    "D": Config("D 再加验算员", verify=True),
    "C-think": Config("C 关思考模式", PipelineSwitches(thinking=False)),
    "C-construct": Config("C 关先构造后成题", PipelineSwitches(construct_first=False)),
    "C-gate": Config("C 关答案闸门", PipelineSwitches(answer_gate=False)),
    "C-keyres": Config("C 关关键结果共享", PipelineSwitches(key_results=False)),
    "C-xcheck": Config("C 关审题答案交叉核对", PipelineSwitches(reviewer_cross_check=False)),
    "C-reroll": Config("C 关定点修改（退回后整份重写，到上限算失败）", PipelineSwitches(targeted_repair=False)),
}

# ---------- 测试集：从 9 套模拟卷的 171 条命题要求里分层抽 ----------

# 每个（题型，难度）格子抽几道。4、5 星的格子抽得多，返工和算错主要出在这些题上：60 道里 32 道是 4、5 星
QUOTA = {
    ("single_choice", 1): 4, ("single_choice", 2): 4, ("single_choice", 3): 6, ("single_choice", 4): 6,
    ("multiple_choice", 3): 4, ("multiple_choice", 4): 5,
    ("fill_blank", 2): 2, ("fill_blank", 3): 3, ("fill_blank", 4): 5,
    ("open_response", 3): 5, ("open_response", 4): 8, ("open_response", 5): 8,
}
# 精简版（30 道）：每个格子取上面 60 道里的前几道，所以试跑的 10 道都在里面；4、5 星 16 道
SMALL_QUOTA = {
    ("single_choice", 1): 2, ("single_choice", 2): 2, ("single_choice", 3): 3, ("single_choice", 4): 3,
    ("multiple_choice", 3): 2, ("multiple_choice", 4): 3,
    ("fill_blank", 2): 1, ("fill_blank", 3): 2, ("fill_blank", 4): 3,
    ("open_response", 3): 2, ("open_response", 4): 4, ("open_response", 5): 3,
}
STRUCTURE = ("A", "B", "C", "D")  # 精简版默认只跑结构消融
# 试跑：从下面 10 个格子各取正式测试集里的第一道
PILOT_CELLS = [
    ("single_choice", 1), ("single_choice", 3), ("single_choice", 4), ("multiple_choice", 3), ("multiple_choice", 4),
    ("fill_blank", 3), ("fill_blank", 4), ("open_response", 3), ("open_response", 4), ("open_response", 5),
]
SEED = 20261104


def pick_specs(which: str) -> list[dict]:
    specs = json.loads((ROOT / "data" / "blueprint_specs.json").read_text(encoding="utf-8"))
    cells = defaultdict(list)
    for spec in sorted(specs, key=lambda s: s["id"]):
        cells[(spec["question_type"], spec["target_difficulty"])].append(spec)
    rng = random.Random(SEED)
    chosen = {cell: rng.sample(cells[cell], count) for cell, count in QUOTA.items()}
    if which == "pilot":
        return [chosen[cell][0] for cell in PILOT_CELLS]
    if which == "small":
        return [spec for cell, count in SMALL_QUOTA.items() for spec in chosen[cell][:count]]
    return [spec for cell in QUOTA for spec in chosen[cell]]


# ---------- 命题要求 → 出题请求：和界面上点"用这条"之后发出去的一样 ----------

TYPE_LABEL = {"single_choice": "单选题", "multiple_choice": "多选题", "fill_blank": "填空题", "open_response": "解答题"}
DIFFICULTY_LABEL = {1: "基础", 2: "较易", 3: "中档", 4: "较难", 5: "压轴"}
PURPOSE = "高考复习"  # 命题要求都来自高三模拟卷


def _knowledge_points() -> list[dict]:
    points = []
    for volume in load_tree(CURRICULUM_CSV):
        for chapter in volume["children"]:
            for section in chapter["children"]:
                for kp in section["children"]:
                    points.append({"id": kp["id"], "name": kp["name"], "grade": volume.get("grade") or None})
    return points


KNOWLEDGE_POINTS = _knowledge_points()


def match_knowledge_point(topic: str) -> dict | None:
    """界面的做法（app.js 的 applyBlueprint）：命题要求里包含哪个知识点的名字，就选名字最长的那个。"""
    key = re.sub(r"\s+", "", topic).lower()
    best = None
    for kp in KNOWLEDGE_POINTS:
        name = re.sub(r"\s+", "", kp["name"]).lower()
        if len(name) >= 2 and name in key and (best is None or len(name) > len(best["key"])):
            best = {**kp, "key": name}
    return best


def make_brief(spec: dict) -> tuple[QuestionDesignBrief, dict | None]:
    kp = match_knowledge_point(spec["topic"])
    grade = kp["grade"] if kp else None
    difficulty = spec["target_difficulty"]
    text = f"请出一道{grade or '高中'}数学{TYPE_LABEL[spec['question_type']]}"
    if kp:
        text += f"，考查「{kp['name']}」"
    text += f"，难度 {difficulty}/5（{DIFFICULTY_LABEL[difficulty]}），用于{PURPOSE}。"
    brief = QuestionDesignBrief(
        request_text=text,
        grade=grade,
        curriculum_node_id=kp["id"] if kp else None,
        knowledge_point_query=kp["name"] if kp else None,
        question_type=spec["question_type"],
        target_difficulty=difficulty,
        teaching_purpose=PURPOSE,
        # jobs.py 也会在要求后面加上配图规则：图里的信息要写进题干
        additional_constraints=f"题目内容：{spec['topic']}\n{FIGURE_RULE}"[:2000],
    )
    return brief, kp


# ---------- 一次出题 ----------


class _Usage:
    def __init__(self) -> None:
        self.payload: dict = {}

    def capture_response(self, payload: dict) -> None:
        self.payload = payload


class Run:
    """一次出题。实现 team.TeamHooks 接收各角色的进展，同时充当模型调用的记录器：记 Token、用时，管调用上限。"""

    MAX_CALLS, MAX_SECONDS = 70, 480  # 和 jobs.Job 一样

    def __init__(self) -> None:
        self.started = time.time()
        self.calls: list[dict] = []
        self.outputs: list[str] = []  # 每次调用模型返回的原文，出题失败时留最后两条备查
        self.events: list[dict] = []
        self.statements = 0
        self.over_budget = False
        self.has_candidate = False
        self.network_errors: list[str] = []
        self._halt = threading.Event()
        self._lock = threading.Lock()

    # team.TeamHooks
    def recorder_for(self, lane: str, **_) -> "Run":
        return self

    def should_stop(self) -> bool:
        return self._halt.is_set()

    def halt(self) -> None:
        self._halt.set()

    def candidate_ready(self) -> None:
        self.has_candidate = True

    def lane_status(self, lane: str, status: str, note: str = "") -> None:
        if status == "failed":
            self.event(lane, "check", "reject", note)

    def lane_progress(self, lane: str, stage: str) -> None:
        pass

    def lane_statement(self, lane: str, preview: dict) -> None:
        with self._lock:
            self.statements += 1

    def event(self, lane: str, role: str, kind: str, text: str, details: list[str] | None = None) -> None:
        with self._lock:
            self.events.append(
                {"at": round(time.time() - self.started, 1), "role": role, "kind": kind, "text": text,
                 "details": list(details or [])}
            )

    # 记录器：provider 每次调用模型都包在 track() 里
    @contextmanager
    def track(self, *, model: str, prompt_version: str, **_):
        if self.should_stop():
            raise StopRequested()
        stage = prompt_version.partition(":")[2]
        call = {"stage": stage, "role": role_of(stage), "model": model, "started": time.time()}
        with self._lock:
            self.calls.append(call)
            # 超过上限：这次调用照常做完，下一次调用前停下（同 jobs.Job._on_call）
            allow = 1 if self.has_candidate else 1.5
            if len(self.calls) > self.MAX_CALLS * allow or call["started"] - self.started > self.MAX_SECONDS * allow:
                self.over_budget = True
                self._halt.set()
        usage = _Usage()
        try:
            yield usage
        except DeepSeekClientError as exc:
            if not isinstance(exc, DeepSeekJsonResponseError):  # 模型回的 JSON 坏了算流程的事，网络和接口出错不算
                self.network_errors.append(str(exc)[:200])
            raise
        finally:
            call.update(seconds=round(time.time() - call["started"], 2), **usage_of(usage.payload))
            output = (usage.payload.get("output") or [{}])[0].get("content", [{}])[0].get("text", "")
            with self._lock:
                self.outputs.append(f"[{stage}] {output[:3000]}")


def scope_hits(question: dict) -> list[str]:
    """交付的题面、选项、解析、答案里有没有高中不学的内容（和 mingti 里用的是同一套规则）。"""
    parts = [
        question.get("stem_plain") or "", question.get("stem_latex") or "",
        *(option["text"] for option in question.get("options") or []),
        question.get("solution_method") or "", *(question.get("solution_steps") or []),
        question.get("final_answer") or "", *(question.get("knowledge_points") or []),
    ]
    return university_only_terms(*parts) + beyond_curriculum_methods(*parts)


QUESTION_FIELDS = (
    "question_type", "stem_plain", "stem_latex", "options", "answer_value", "final_answer", "solution_method",
    "solution_steps", "estimated_difficulty", "knowledge_points", "solution_status", "solution_issue",
)


def run_one(spec: dict, name: str, routes: dict, catalog, verifier, budget: float) -> dict | None:
    if OUT_OF_BALANCE.is_set() or (budget and SPENT["usd"] > budget):
        return None  # 余额用完或超出预算：不再开始新的出题
    config = CONFIGS[name]
    run = Run()
    team = TeamProvider(lanes=1, routes=routes, verify=config.verify, hooks=run, thinking=True, switches=config.switches)
    authoring = QuestionAuthoring(curriculum=catalog, provider=team, math_verifier=verifier)
    brief, kp = make_brief(spec)
    status, error, question = "succeeded", "", None
    try:
        if config.switches.stop_after == "solve":
            # 和界面出题同一条路，包括最后的结构、超纲和可信检查
            question = authoring.create_draft(brief).model_dump(mode="json")
        else:
            # 只跑到命题或审题的配置没有解析，过不了 create_draft 最后的检查，直接拿 TeamProvider 交出的题
            context = QuestionAuthoringContext(
                brief=brief,
                question_type=brief.question_type,
                curriculum=authoring._resolve_curriculum_guidance(brief),
                curriculum_options=authoring._curriculum_options(),
            )
            draft = QuestionAuthoring._canonicalize_generated_content(team.generate(context))
            question = draft.model_dump(mode="json")
    except (QuestionAuthoringError, QuestionAuthoringProviderError) as exc:
        status, error = "failed", str(exc)
    except Exception as exc:  # noqa: BLE001 - 程序出错记下来，下次再跑
        status, error = "error", f"{type(exc).__name__}: {exc}"
    if status == "failed" and run.network_errors and not run.over_budget:
        status, error = "error", f"{error}（网络或接口出错：{run.network_errors[-1]}）"

    calls = run.calls
    return {
        "id": f"{name}|{spec['id']}",
        "config": name,
        "spec_id": spec["id"],
        "question_type": spec["question_type"],
        "difficulty": spec["target_difficulty"],
        "topic": spec["topic"],
        "knowledge_point": kp["name"] if kp else None,
        "status": status,
        "error": error[:600],
        "over_budget": run.over_budget,
        "flagged": bool(question and question.get("solution_issue")),  # 返工到上限交付的，复核还有意见
        "seconds": round(time.time() - run.started, 1),
        "calls": len(calls),
        "input_tokens": sum(c.get("input_tokens", 0) for c in calls),
        "cache_hit": sum(c.get("cache_hit", 0) for c in calls),
        "output_tokens": sum(c.get("output_tokens", 0) for c in calls),
        "reasoning_tokens": sum(c.get("reasoning_tokens", 0) for c in calls),
        "cost_usd": round(sum(call_cost(c) for c in calls if "seconds" in c), 5),
        "cost_usd_offpeak": round(sum(call_cost(c, peak=False) for c in calls if "seconds" in c), 5),
        "statement_versions": run.statements,
        "scope_hits": scope_hits(question) if question else [],
        "question": {key: question.get(key) for key in QUESTION_FIELDS} if question else None,
        "events": run.events,
        "call_log": calls,
        "last_outputs": run.outputs[-2:] if status == "failed" else [],
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("which", choices=["pilot", "small", "full"])
    parser.add_argument("--configs", default="", help="逗号分隔；默认 small 跑 A,B,C,D，pilot 和 full 跑全部 9 种")
    parser.add_argument("--workers", type=int, default=8, help="同时出几道题")
    parser.add_argument("--limit", type=int, default=0, help="只跑前几次（调试用）")
    parser.add_argument("--budget", type=float, default=0, help="这次最多花多少美元（闲时价算），超过就不再开始新的出题")
    args = parser.parse_args()

    names = [name.strip() for name in args.configs.split(",") if name.strip()]
    names = names or list(STRUCTURE if args.which == "small" else CONFIGS)
    unknown = [name for name in names if name not in CONFIGS]
    if unknown:
        raise SystemExit(f"没有这些配置：{unknown}；可选 {list(CONFIGS)}")
    specs = pick_specs(args.which)
    out = RESULTS / args.which / "runs.jsonl"
    done = {key for key, row in latest(read_jsonl(out)).items() if row["status"] != "error"}
    # 按题目轮流排各配置，跑到一半停下时每种配置跑过的题数也差不多
    todo = [(spec, name) for spec in specs for name in names if f"{name}|{spec['id']}" not in done]
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(specs)} 道题 × {len(names)} 种配置，已完成 {len(done)} 次，这次要跑 {len(todo)} 次", flush=True)
    if not todo:
        return

    settings = load_settings()
    client = patient_client(
        api_key=settings.deepseek_api_key, model=MODEL, base_url=settings.deepseek_base_url,
        timeout_seconds=settings.deepseek_timeout_seconds,
    )
    route = ModelRoute(id=f"deepseek:{MODEL}", provider="deepseek", model=MODEL, client=client)
    routes = {role: route for role in ROLES}
    catalog = CsvCurriculumCatalog(CURRICULUM_CSV)
    verifier = MathVerifier()
    writer = JsonlWriter(out)
    started = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run_one, spec, name, routes, catalog, verifier, args.budget) for spec, name in todo]
        for index, future in enumerate(as_completed(futures), 1):
            row = future.result()
            if row is None:
                continue
            SPENT["usd"] += row["cost_usd_offpeak"]
            writer.append(row)
            tokens = (row["input_tokens"] + row["output_tokens"]) / 1000
            print(
                f"[{index}/{len(todo)} {round((time.time() - started) / 60, 1)}分] {row['config']:<12} {row['spec_id']:<7} "
                f"{row['status']:<9} {row['calls']:>3}次 {tokens:>6.1f}k {row['seconds']:>6.1f}s ${row['cost_usd']:.4f}"
                + (f"  {row['error'][:80]}" if row["error"] else ""),
                flush=True,
            )
    print(f"这次共花 ${SPENT['usd']:.3f}（闲时价）", flush=True)
    if RATE_LIMITED["times"]:
        print(f"其间被限流（HTTP 429）等了 {RATE_LIMITED['times']} 次、共 {RATE_LIMITED['seconds']:.0f} 秒，"
              "这些题的用时偏长；余额多一些或者 --workers 调小可以避免", flush=True)
    if OUT_OF_BALANCE.is_set():
        print("DeepSeek 账户余额不足（HTTP 402），已经停下；充值后再运行同一条命令，会接着跑没出完的。", flush=True)
    elif args.budget and SPENT["usd"] > args.budget:
        print(f"超过预算 ${args.budget}，已经停下；再运行同一条命令会接着跑。", flush=True)


if __name__ == "__main__":
    main()
