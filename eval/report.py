"""汇总 ablation.py 和 judge.py 的结果：每次出题一行的明细表、每种配置的汇总表、图表和表格片段。

    python eval/report.py small --audit 30   汇总正式实验（精简版），另外抽 30 道题给人工核对（human_audit.md 看题，human_audit.csv 填结论）
    python eval/report.py pilot              汇总试跑，并按试跑估计完整版要花多少钱、多少时间
    python eval/report.py full               汇总完整版

human_audit.csv 的"人工结论"一列填好（对 / 错 / 题目不成立）以后再运行一次，会算出评判模型和人工的一致率。
用装了 matplotlib 的系统 Python 运行即可，不需要项目的 .venv。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import statistics
from collections import Counter
from datetime import datetime

from common import RESULTS, latest, read_jsonl

LABELS = {
    "A": "A 只有命题员",
    "B": "B 加审题员",
    "C": "C 完整闭环",
    "D": "D 再加验算员",
    "C-think": "C 关思考模式",
    "C-construct": "C 关先构造后成题",
    "C-gate": "C 关答案闸门",
    "C-keyres": "C 关关键结果共享",
    "C-xcheck": "C 关交叉核对",
}
# 图上横轴的短标签（两行）：机制消融一幅有 6 根柱子，完整名称放不下
TICK = {
    "A": "A\n只有命题员", "B": "B\n加审题员", "C": "C\n完整闭环", "D": "D\n再加验算员",
    "C-think": "关\n思考模式", "C-construct": "关先构造\n后成题", "C-gate": "关\n答案闸门",
    "C-keyres": "关关键\n结果共享", "C-xcheck": "关\n交叉核对",
}
STRUCTURE = ["A", "B", "C", "D"]
MECHANISMS = ["C-think", "C-construct", "C-gate", "C-keyres", "C-xcheck"]
VERDICT_LABEL = {"correct": "对", "wrong": "错", "uncertain": "说不准", "ill_posed": "题目不成立"}
TYPE_LABEL = {"single_choice": "单选", "multiple_choice": "多选", "fill_blank": "填空", "open_response": "解答"}
# 正式测试集每个（题型，难度）格子有几道题，共 60 道。和 ablation.QUOTA 相同；report.py 用系统 Python 跑，不 import ablation
FULL_QUOTA = {
    ("single_choice", 1): 4, ("single_choice", 2): 4, ("single_choice", 3): 6, ("single_choice", 4): 6,
    ("multiple_choice", 3): 4, ("multiple_choice", 4): 5,
    ("fill_blank", 2): 2, ("fill_blank", 3): 3, ("fill_blank", 4): 5,
    ("open_response", 3): 5, ("open_response", 4): 8, ("open_response", 5): 8,
}


def wilson(k: int, n: int) -> tuple[float, float]:
    """比例的 95% 置信区间（Wilson 法，样本小也不会跑出 0–1 之外）。"""
    if n == 0:
        return 0.0, 0.0
    z, p = 1.96, k / n
    center = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    # k = 0 时下限正好是 0、k = n 时上限正好是 1；直接算会有浮点误差（如 0.9999999999999999），画误差棒时成了负长度
    lo = 0.0 if k == 0 else max(0.0, center - half)
    hi = 1.0 if k == n else min(1.0, center + half)
    return lo, hi


def mcnemar_p(b: int, c: int) -> float:
    """配对比较（同一批题，两种配置）：只看一边对一边错的题，b 和 c 是两个方向的题数；精确二项检验的双侧 p 值。"""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1)) / 2**n
    return min(1.0, 2 * tail)


def load(which: str) -> list[dict]:
    """每次出题一行：出题记录 + 评判结果。出错（网络、程序）没跑完的不算。"""
    folder = RESULTS / which
    judges = latest(read_jsonl(folder / "judge.jsonl"))
    rows = []
    for run in latest(read_jsonl(folder / "runs.jsonl")).values():
        if run["status"] == "error":
            continue
        judge = judges.get(run["id"], {})
        intercepted = any(c["stage"].startswith("core-high-school-repair") for c in run["call_log"]) or any(
            "高中不学" in e["text"] for e in run["events"]
        )
        rows.append({
            "id": run["id"],
            "config": run["config"],
            "spec_id": run["spec_id"],
            "question_type": run["question_type"],
            "difficulty": run["difficulty"],
            "level": "high" if run["difficulty"] >= 4 else "low",
            "topic": run["topic"],
            "status": run["status"],
            "over_budget": run["over_budget"],
            "error": run["error"],
            "calls": run["calls"],
            "tokens": run["input_tokens"] + run["output_tokens"],
            "input_tokens": run["input_tokens"],
            "cache_hit": run["cache_hit"],
            "output_tokens": run["output_tokens"],
            "reasoning_tokens": run["reasoning_tokens"],
            "seconds": run["seconds"],
            "cost_usd": run["cost_usd"],
            "cost_usd_offpeak": run["cost_usd_offpeak"],
            "statement_versions": run["statement_versions"],
            "scope_hits": "、".join(run["scope_hits"]),
            "scope_intercepted": intercepted,
            "estimated_difficulty": (run["question"] or {}).get("estimated_difficulty"),
            "verdict": judge.get("verdict", ""),
            "agree": judge.get("agree", ""),
            "ill_posed_votes": judge.get("ill_posed_votes", ""),
            "topic_match": judge.get("topic_match", ""),
            "judge_difficulty": judge.get("judge_difficulty", ""),
            "judge_cost_usd": judge.get("judge_cost_usd", 0.0),
            "judge_cost_usd_offpeak": judge.get("judge_cost_usd_offpeak", 0.0),
            # 评一道题的用时：3 次重解同时跑，取最慢的一次，再加上比答案的那次调用
            "judge_seconds": max([c["seconds"] for c in judge.get("judge_calls", []) if c["stage"].startswith("solve")] or [0])
            + sum(c["seconds"] for c in judge.get("judge_calls", []) if c["stage"] == "group"),
            "finished_at": run["finished_at"],
        })
    return rows


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    done = [r for r in rows if r["status"] == "succeeded"]
    judged = [r for r in done if r["verdict"]]
    verdicts = Counter(r["verdict"] for r in judged)
    correct = verdicts["correct"]
    mean = lambda key, items=rows: round(statistics.mean(r[key] for r in items), 1) if items else 0  # noqa: E731
    with_judge_difficulty = [r for r in judged if r["judge_difficulty"] != ""]
    base = n - len(done) + len(judged)  # 可用率的分母：没出成的 + 出成且评过的（还没评判的题先不算）
    return {
        "n": n,
        "succeeded": len(done),
        "success_rate": len(done) / n if n else 0,
        "success_ci": wilson(len(done), n),
        "over_budget": sum(r["over_budget"] for r in rows),
        "judged": len(judged),
        "correct": correct,
        "wrong": verdicts["wrong"],
        "uncertain": verdicts["uncertain"],
        "ill_posed": verdicts["ill_posed"],
        "accuracy": correct / len(judged) if judged else 0,  # 交付的题里答案对的比例
        "accuracy_ci": wilson(correct, len(judged)),
        # 可用率：全部尝试里，出成了、题目成立、答案也对的比例
        "usable_rate": correct / base if base else 0,
        "usable_ci": wilson(correct, base),
        "calls": mean("calls"),
        "tokens_k": round(statistics.mean(r["tokens"] for r in rows) / 1000, 1) if rows else 0,
        "seconds": mean("seconds"),
        "median_seconds": round(statistics.median(r["seconds"] for r in rows), 1) if rows else 0,
        "cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
        "cost_usd_offpeak": round(sum(r["cost_usd_offpeak"] for r in rows), 4),
        "cost_per_usable_offpeak": round(sum(r["cost_usd_offpeak"] for r in rows) / correct, 4) if correct else None,
        "statement_versions": mean("statement_versions", done),
        "scope_final": sum(1 for r in done if r["scope_hits"]),
        "scope_intercepted": sum(1 for r in rows if r["scope_intercepted"]),
        "topic_match": sum(1 for r in judged if r["topic_match"] == "符合") / len(judged) if judged else 0,
        "topic_partial": sum(1 for r in judged if r["topic_match"] == "部分符合") / len(judged) if judged else 0,
        "difficulty_within_1": (
            sum(1 for r in with_judge_difficulty if abs(r["judge_difficulty"] - r["difficulty"]) <= 1)
            / len(with_judge_difficulty) if with_judge_difficulty else 0
        ),
        "difficulty_mae": (
            round(statistics.mean(abs(r["judge_difficulty"] - r["difficulty"]) for r in with_judge_difficulty), 2)
            if with_judge_difficulty else 0
        ),
        "judge_cost_usd": round(sum(r["judge_cost_usd"] for r in judged), 4),
        "judge_cost_usd_offpeak": round(sum(r["judge_cost_usd_offpeak"] for r in judged), 4),
    }


def missing(s: dict, key: str) -> bool:
    """还没评判时，和答案对错有关的指标算不出来：表里写"未评"，图上不画柱子，免得看成 0%。"""
    if key == "usable_rate":
        return s["succeeded"] > 0 and s["judged"] == 0
    return key in {"accuracy", "topic_match", "difficulty_within_1"} and s["judged"] == 0


def paired_vs_c(rows: list[dict], name: str) -> dict:
    """和 C 在同一批题上逐题比"可用"（出成且答案对）：b = C 可用而它不可用的题数，c = 反过来的题数。
    只比两边都有结论的题：没出成的，或者出成且评过的。"""
    usable = {(r["config"], r["spec_id"]): r["verdict"] == "correct"
              for r in rows if r["status"] != "succeeded" or r["verdict"]}
    specs = {s for c, s in usable if c == name} & {s for c, s in usable if c == "C"}
    b = sum(1 for s in specs if usable[("C", s)] and not usable[(name, s)])
    c = sum(1 for s in specs if usable[(name, s)] and not usable[("C", s)])
    return {"pairs": len(specs), "c_only": b, "x_only": c, "p": round(mcnemar_p(b, c), 3)}


def pct(x: float) -> str:
    return f"{100 * x:.0f}%"


def table_md(summary: dict, names: list[str], rows: list[dict]) -> str:
    lines = [
        "| 配置 | 题数 | 交付率 | 答案正确率 | 可用率 | 题目不成立 | 平均调用 | 平均 Token | 平均用时 | 每道可用题花费 | 超纲（交付/拦下） | 考点符合 | 难度差≤1 | 和 C 逐题比（C 独有/它独有，p） |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name in names:
        s = summary[name]
        paired = "—"
        if name != "C" and any(r["config"] == "C" for r in rows):
            p = paired_vs_c(rows, name)
            paired = f"{p['c_only']}/{p['x_only']}，p={p['p']}" if p["pairs"] else "未评"
        accuracy = "未评" if missing(s, "accuracy") else f"{pct(s['accuracy'])}（{s['correct']}/{s['judged']}）"
        usable = "未评" if missing(s, "usable_rate") else pct(s["usable_rate"])
        topic = "未评" if missing(s, "topic_match") else pct(s["topic_match"])
        level = "未评" if missing(s, "difficulty_within_1") else pct(s["difficulty_within_1"])
        cost = f"${s['cost_per_usable_offpeak']:.4f}" if s["cost_per_usable_offpeak"] else "—"
        lines.append(
            f"| {LABELS[name]} | {s['n']} | {pct(s['success_rate'])} | {accuracy} | {usable} | {s['ill_posed']} "
            f"| {s['calls']} | {s['tokens_k']}k | {s['seconds']} 秒 | {cost} "
            f"| {s['scope_final']}/{s['scope_intercepted']} | {topic} | {level} | {paired} |"
        )
    return "\n".join(lines)


# ---------- 图 ----------

INK, INK_2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#ffffff"
BLUE, ORANGE, AQUA, YELLOW = "#2a78d6", "#eb6834", "#1baf7a", "#eda100"


def _style(plt) -> None:
    plt.rcParams.update({
        "font.sans-serif": ["Microsoft YaHei", "SimHei", "DejaVu Sans"],
        "axes.unicode_minus": False,
        "font.size": 10,
        "axes.edgecolor": GRID,
        "axes.labelcolor": INK_2,
        "xtick.color": INK_2,
        "ytick.color": INK_2,
        "axes.grid": True,
        "grid.color": GRID,
        "grid.linewidth": 0.6,
        "axes.axisbelow": True,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "figure.facecolor": SURFACE,
        "savefig.dpi": 200,
    })


def rate_panels(plt, folder, summary, names, metrics, filename, title):
    """几个比例指标各画一小幅，横轴是配置，竖线是 95% 置信区间，数值标在区间上方。还没评判的写"未评"。"""
    panel = max(3.4, 0.62 * len(names))
    fig, axes = plt.subplots(1, len(metrics), figsize=(panel * len(metrics), 3.6), sharey=True)
    for ax, (key, label) in zip(axes, metrics):
        ci_key = "accuracy_ci" if key == "accuracy" else key.replace("_rate", "") + "_ci"
        for i, name in enumerate(names):
            s = summary[name]
            if missing(s, key):
                ax.text(i, 0.02, "未评", ha="center", va="bottom", color=INK_2, fontsize=8.5)
                continue
            v, (lo, hi) = s[key], s[ci_key]
            ax.bar(i, v, width=0.6, color=BLUE)
            ax.errorbar(i, v, yerr=[[v - lo], [hi - v]], fmt="none", ecolor=INK_2, elinewidth=1, capsize=3)
            ax.text(i, hi + 0.015, pct(v), ha="center", va="bottom", color=INK, fontsize=8.5)
        ax.set_xticks(range(len(names)), [TICK[n] for n in names], fontsize=8.5 if len(names) <= 4 else 7.8)
        ax.set_ylim(0, 1.14)
        ax.set_yticks([0, 0.2, 0.4, 0.6, 0.8, 1.0])
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0%}"))
        ax.set_title(label, color=INK, fontsize=10.5)
        ax.grid(axis="x", visible=False)
    fig.suptitle(title, color=INK, fontsize=12, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(folder / filename)
    plt.close(fig)


def cost_panels(plt, folder, summary, names, filename):
    """调用次数、Token、用时各一小幅（单位不同，不画在一根轴上）。结构消融蓝色，机制消融橙色。"""
    metrics = [("calls", "平均调用次数"), ("tokens_k", "平均 Token（千）"), ("seconds", "平均用时（秒）")]
    fig, axes = plt.subplots(1, 3, figsize=(10.5, 0.42 * len(names) + 1.2), sharey=True)
    y = list(range(len(names)))[::-1]
    colors = [BLUE if n in STRUCTURE else ORANGE for n in names]
    for ax, (key, label) in zip(axes, metrics):
        values = [summary[n][key] for n in names]
        ax.barh(y, values, height=0.62, color=colors)
        for yi, v in zip(y, values):
            ax.text(v, yi, f" {v:g}", va="center", ha="left", color=INK, fontsize=8.5)
        ax.set_title(label, color=INK, fontsize=10.5)
        ax.set_xlim(0, max(values) * 1.22 if max(values) else 1)
        ax.grid(axis="y", visible=False)
    axes[0].set_yticks(y, [LABELS[n] for n in names])
    handles = [plt.Rectangle((0, 0), 1, 1, color=BLUE), plt.Rectangle((0, 0), 1, 1, color=ORANGE)]
    fig.legend(handles, ["结构消融", "在 C 上关掉一个机制"], loc="lower center", ncol=2, frameon=False, fontsize=9)
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    fig.savefig(folder / filename)
    plt.close(fig)


def level_chart(plt, folder, rows, names, filename):
    """按难度分两组（1–3 星、4–5 星）看可用率。"""
    groups = [("low", "1–3 星"), ("high", "4–5 星")]
    palette = [BLUE, ORANGE, AQUA, YELLOW]
    fig, ax = plt.subplots(figsize=(6.4, 3.4))
    width = 0.8 / len(names)
    for i, name in enumerate(names):
        values, notes = [], []
        for level, _ in groups:
            subset = [r for r in rows if r["config"] == name and r["level"] == level]
            s = summarize(subset) if subset else None
            ok = s is not None and not missing(s, "usable_rate")
            values.append(s["usable_rate"] if ok else 0)
            notes.append(pct(s["usable_rate"]) if ok else ("未评" if s else "—"))
        xs = [g + (i - (len(names) - 1) / 2) * width for g in range(len(groups))]
        ax.bar(xs, values, width=width * 0.9, color=palette[i % len(palette)], label=LABELS[name])
        for x, v, note in zip(xs, values, notes):
            ax.text(x, v + 0.015, note, ha="center", va="bottom", fontsize=8, color=INK if note[-1] == "%" else INK_2)
    ax.set_xticks(range(len(groups)), [label for _, label in groups])
    ax.set_ylim(0, 1.1)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0%}"))
    ax.set_ylabel("可用率")
    ax.grid(axis="x", visible=False)
    ax.legend(frameon=False, fontsize=8.5, ncol=len(names), loc="lower center", bbox_to_anchor=(0.5, 1.0))  # 放在图上方，不挡 100% 的柱子
    fig.tight_layout()
    fig.savefig(folder / filename)
    plt.close(fig)


def make_charts(folder, summary, rows, names) -> list[str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    _style(plt)
    made = []
    structure = [n for n in STRUCTURE if n in summary]
    mechanisms = ["C"] + [n for n in MECHANISMS if n in summary]
    rate_metrics = [("success_rate", "交付率"), ("accuracy", "答案正确率（交付的题里）"), ("usable_rate", "可用率（全部尝试里）")]
    if structure:
        rate_panels(plt, folder, summary, structure, rate_metrics, "fig1_structure.png", "结构消融：一层层加角色")
        level_chart(plt, folder, rows, structure, "fig3_by_difficulty.png")
        made += ["fig1_structure.png", "fig3_by_difficulty.png"]
    if len(mechanisms) > 1:
        rate_panels(plt, folder, summary, mechanisms, rate_metrics, "fig4_mechanisms.png", "机制消融：在完整闭环 C 上每次关掉一个机制")
        made.append("fig4_mechanisms.png")
    cost_panels(plt, folder, summary, names, "fig2_cost.png")
    made.append("fig2_cost.png")
    return made


# ---------- 试跑后估计正式实验 ----------


def _nearest(rows: list[dict], question_type: str, difficulty: int) -> list[dict]:
    """同题型里难度最接近的那几次出题。试跑每个格子只有一道题，单选 2 星、填空 2 星这两个格子试跑里没有。"""
    same = [r for r in rows if r["question_type"] == question_type]
    if not same:
        return []
    gap = min(abs(r["difficulty"] - difficulty) for r in same)
    return [r for r in same if abs(r["difficulty"] - difficulty) == gap]


def estimate_full(rows: list[dict], names: list[str], workers: int = 10, judge_workers: int = 6) -> str:
    """按试跑换算正式实验的花费和用时：正式测试集每个（题型，难度）格子的题数，乘以试跑里同题型、难度最接近的那次出题。
    只有出成的题要评，所以每次尝试的评判花费 = 出成率 × 同题型评一道题的平均花费。"""
    judged = [r for r in rows if r["verdict"]]
    lines = ["| 配置 | 出题花费（闲时） | 评判花费（闲时） | 每次出题平均用时 |", "|---|---|---|---|"]
    total_gen = total_judge = total_seconds = total_judge_seconds = 0.0
    missing: list[str] = []
    for name in names:
        mine = [r for r in rows if r["config"] == name]
        gen = judge = seconds = judge_seconds = 0.0
        covered = 0
        for (question_type, difficulty), count in FULL_QUOTA.items():
            like = _nearest(mine, question_type, difficulty)
            if not like:
                label = f"{LABELS[name]}的{TYPE_LABEL[question_type]}题"
                if label not in missing:
                    missing.append(label)
                continue
            covered += count
            gen += count * statistics.mean(r["cost_usd_offpeak"] for r in like)
            seconds += count * statistics.mean(r["seconds"] for r in like)
            delivered = sum(r["status"] == "succeeded" for r in like) / len(like)
            graded = _nearest(judged, question_type, difficulty) or judged  # 这种题型还没评过时，先用评过的全部题
            if graded:
                judge += count * delivered * statistics.mean(r["judge_cost_usd_offpeak"] for r in graded)
                judge_seconds += count * delivered * statistics.mean(r["judge_seconds"] for r in graded)
        total_gen += gen
        total_judge += judge
        total_seconds += seconds
        total_judge_seconds += judge_seconds
        lines.append(f"| {LABELS[name]} | ${gen:.2f} | ${judge:.2f} | {seconds / covered:.0f} 秒 |" if covered else
                     f"| {LABELS[name]} | — | — | — |")
    lines.append(f"| **合计** | **${total_gen:.2f}** | **${total_judge:.2f}** | |")
    finished = sorted(datetime.fromisoformat(r["finished_at"]) for r in rows)
    wall = (finished[-1] - finished[0]).total_seconds() if len(finished) > 1 else 0
    parallel = min(workers, sum(r["seconds"] for r in rows) / wall) if wall else workers
    lines += [
        "",
        f"出题：各次用时加起来约 {total_seconds / 3600:.1f} 小时；试跑时同时出 {workers} 道，实际并行度约 {parallel:.1f}，"
        f"照此约需 {total_seconds / parallel / 3600:.1f} 小时。评判：同时评 {judge_workers} 道，约 "
        f"{total_judge_seconds / judge_workers / 3600:.1f} 小时。高峰时段（北京时间工作日 9–12 点、14–18 点）价格翻倍。",
        "",
        f"评判花费按试跑里评过的 {len(judged)} 道题估计。",
    ]
    if missing:
        lines += ["", f"试跑里没有数据、上面没算进去：{'、'.join(missing)}。试跑补齐后再运行一次 report.py 就会算进去。"]
    return "\n".join(lines)


# ---------- 人工核对 ----------


def make_audit(folder, rows: list[dict], count: int) -> None:
    """抽题给人工核对评判结论：判错、说不准、题目不成立的尽量都抽，其余从判对的里按配置轮流抽。"""
    runs = latest(read_jsonl(folder / "runs.jsonl"))
    judges = latest(read_jsonl(folder / "judge.jsonl"))
    judged = [r for r in rows if r["verdict"]]
    rng = random.Random(2026)
    doubtful = [r for r in judged if r["verdict"] != "correct"]
    rng.shuffle(doubtful)
    picked = doubtful[: count // 3]
    rest = [r for r in judged if r["verdict"] == "correct"]
    rng.shuffle(rest)
    by_config: dict[str, list] = {}
    for r in rest:
        by_config.setdefault(r["config"], []).append(r)
    while len(picked) < count and any(by_config.values()):
        for name in list(by_config):
            if by_config[name] and len(picked) < count:
                picked.append(by_config[name].pop())
    picked.sort(key=lambda r: (r["spec_id"], r["config"]))

    md = ["# 人工核对：评判模型的结论对不对", "",
          "每道题看题面和交付的答案，自己判断答案对不对，把结论填进 human_audit.csv 的“人工结论”一列（对 / 错 / 题目不成立）。",
          "评判模型的结论和它 3 次重解的答案附在后面，可以参考，也可以先不看。", ""]
    with (folder / "human_audit.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["编号", "配置", "命题要求", "评判结论", "人工结论", "备注"])
        for index, r in enumerate(picked, 1):
            question = runs[r["id"]]["question"]
            judge = judges[r["id"]]
            writer.writerow([r["id"], LABELS[r["config"]], r["topic"], VERDICT_LABEL[r["verdict"]], "", ""])
            md += [f"## {index}. {r['id']}（{LABELS[r['config']]}，{r['difficulty']} 星）", "",
                   f"**命题要求**：{r['topic']}", "", question.get("stem_latex") or question["stem_plain"], ""]
            for option in question.get("options") or []:
                md.append(f"- {option['key']}. {option['text']}")
            md += ["", f"**交付的答案**：{question['final_answer']}", "",
                   "<details><summary>评判模型</summary>", "",
                   f"结论：{VERDICT_LABEL[r['verdict']]}（{r['agree']}/3 次重解和交付答案一致）", ""]
            for i, solve in enumerate(judge["solves"], 1):
                if solve:
                    keys = "".join(str(k) for k in solve.get("choice_keys") or [])
                    md.append(f"- 重解 {i}：{('选 ' + keys + '；') if keys else ''}{solve.get('final_answer', '')}"
                              + ("" if solve.get("well_posed", True) else f"（认为题目有问题：{solve.get('issue', '')}）"))
            md += ["", "</details>", ""]
    (folder / "human_audit.md").write_text("\n".join(md), encoding="utf-8")
    print(f"抽了 {len(picked)} 道题：{folder / 'human_audit.md'}，结论填进 {folder / 'human_audit.csv'}")


def audit_agreement(folder) -> str:
    path = folder / "human_audit.csv"
    if not path.exists():
        return ""
    with path.open(encoding="utf-8-sig") as handle:
        rows = [r for r in csv.DictReader(handle) if r["人工结论"].strip()]
    if not rows:
        return ""
    normalize = {"对": "对", "错": "错", "题目不成立": "题目不成立", "不成立": "题目不成立"}
    same = sum(1 for r in rows if normalize.get(r["人工结论"].strip()) == r["评判结论"])
    lo, hi = wilson(same, len(rows))
    return f"人工核对 {len(rows)} 道，评判模型和人工结论一致 {same} 道（{pct(same / len(rows))}，95% 置信区间 {pct(lo)}–{pct(hi)}）。"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("which", choices=["pilot", "small", "full"])
    parser.add_argument("--audit", type=int, default=0, help="抽多少道题做人工核对")
    args = parser.parse_args()
    folder = RESULTS / args.which
    rows = load(args.which)
    names = [n for n in LABELS if any(r["config"] == n for r in rows)]
    summary = {name: summarize([r for r in rows if r["config"] == name]) for name in names}

    with (folder / "runs.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    with (folder / "summary.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        keys = [k for k in summary[names[0]] if not k.endswith("_ci")]
        writer = csv.writer(handle)
        writer.writerow(["config", "label", *keys])
        for name in names:
            writer.writerow([name, LABELS[name], *(summary[name][k] for k in keys)])
    by_level = {
        level: {name: summarize([r for r in rows if r["config"] == name and r["level"] == level]) for name in names}
        for level in ("low", "high")
    }
    (folder / "summary.json").write_text(
        json.dumps({"overall": summary, "by_level": by_level}, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    parts = [f"# 结果表（{args.which}，{len(rows)} 次出题）", "", table_md(summary, names, rows), ""]
    for level, label in (("low", "1–3 星"), ("high", "4–5 星")):
        parts += [f"## {label}", "", table_md(by_level[level], names, [r for r in rows if r["level"] == level]), ""]
    if args.which == "pilot":
        parts += ["## 按试跑估计正式实验（60 道题 × 9 种配置）", "", estimate_full(rows, names), ""]
    if args.audit:
        make_audit(folder, rows, args.audit)
    agreement = audit_agreement(folder)
    if agreement:
        parts += ["## 人工核对", "", agreement, ""]
    parts += ["## 图", ""] + [f"![]({name})" for name in make_charts(folder, summary, rows, names)]
    (folder / "tables.md").write_text("\n".join(parts), encoding="utf-8")
    print("\n".join(parts[:4]))
    print(f"写好了：{folder}")


if __name__ == "__main__":
    main()
