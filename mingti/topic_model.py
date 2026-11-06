"""专题识别：判断一道题属于哪个专题，出题时用来缩小交给"规划解法"那一步的知识点表。

模型是一个很小的文本分类器：题干按字切成 1–3 个字的片段，TF-IDF 加权后用逻辑回归打分，分 9 个专题
（集合与逻辑、函数、三角函数与解三角形、数列、导数、立体几何、直线与圆、圆锥曲线、计数原理与概率统计）。
它在 3317 道真人编写的讲义题上训练，权重存在 data/topic_model.json.gz，只有词表和系数，没有题目原文。
计算只用标准库，一道题不到 1 毫秒。

它只影响题目挂哪个知识点，不影响题目和答案：知识点表只在题目写好以后、给题目选知识点时才用到。
为了不挂错，拿得准才缩小：
- 最高概率 ≥ 0.9，只给这一个专题的知识点；
- 前两个专题的概率加起来 ≥ 0.9，给这两个专题的；
- 否则照旧给全部知识点。
复数、平面向量两章分类器认不出来，缩小时总是带上。出任何问题都退回全部知识点。
"""

from __future__ import annotations

import csv
import gzip
import json
import math
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODEL_PATH = ROOT / "data" / "topic_model.json.gz"
TREE_PATH = ROOT / "data" / "curriculum" / "pep-a-full-knowledge-tree-v1.csv"

# 教材的章 → 专题。用的是 A 版的章名；B 版沿用 A 版的知识点编号，按知识点编号对照，两版都适用
CHAPTER_TOPIC = {
    "集合与常用逻辑用语": "集合与逻辑", "一元二次函数方程和不等式": "函数", "函数的概念与性质": "函数",
    "指数函数与对数函数": "函数", "三角函数": "三角函数与解三角形", "立体几何初步": "立体几何",
    "空间向量与立体几何": "立体几何", "直线和圆的方程": "直线与圆", "圆锥曲线的方程": "圆锥曲线", "数列": "数列",
    "一元函数的导数及其应用": "导数", "计数原理": "计数原理与概率统计", "统计": "计数原理与概率统计",
    "概率": "计数原理与概率统计", "随机变量及其分布": "计数原理与概率统计", "成对数据的统计分析": "计数原理与概率统计",
}
ALWAYS = "*"  # 复数、平面向量及其应用：分类器没有这两类，缩小时总是带上
ALWAYS_CHAPTERS = {"复数", "平面向量及其应用"}
MIN_KEPT = 10  # 缩小后少于这么多行就不缩小（不该发生，防万一）


def normalize(text: str) -> str:
    """和训练时一样的写法统一：去掉 $ 和排版命令，\\sin、\\ln 这类命令留下名字，括号、上下标符号去掉。"""
    text = text.replace("$", " ")
    text = re.sub(r"\\(?:left|right|displaystyle|mathrm|boldsymbol|mathbf|text|quad|qquad|[,;!])", " ", text)
    text = re.sub(r"\\[dt]?frac", " ", text)
    text = re.sub(r"\\([a-zA-Z]+)", r" \1 ", text)
    text = re.sub(r"[{}^_\\]", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


@lru_cache(maxsize=1)
def _model() -> dict:
    return json.loads(gzip.decompress(MODEL_PATH.read_bytes()).decode("utf-8"))


@lru_cache(maxsize=1)
def _knowledge_point_topics() -> dict[str, str]:
    """知识点编号 → 专题；复数、平面向量两章记作 ALWAYS。"""
    with TREE_PATH.open(encoding="utf-8-sig") as handle:
        nodes = {row["node_id"]: row for row in csv.DictReader(handle)}
    topics = {}
    for node_id, node in nodes.items():
        if node["node_type"] != "knowledge_point":
            continue
        chapter = nodes.get(node["parent_id"])
        while chapter is not None and chapter["node_type"] != "chapter":
            chapter = nodes.get(chapter["parent_id"])
        name = chapter["name"] if chapter else ""
        if name in ALWAYS_CHAPTERS:
            topics[node_id] = ALWAYS
        elif name in CHAPTER_TOPIC:
            topics[node_id] = CHAPTER_TOPIC[name]
    return topics


def probabilities(text: str) -> list[tuple[str, float]]:
    """各专题的概率，从高到低。字 1–3 元组计数 → 1+log(次数) → 乘 idf → 长度归一 → 线性打分 → softmax。"""
    model = _model()
    doc = re.sub(r"\s\s+", " ", normalize(text))
    counts = Counter(doc[i:i + n] for n in range(1, 4) for i in range(len(doc) - n + 1))
    vocabulary, idf = model["vocabulary"], model["idf"]
    weights = {vocabulary[g]: (1 + math.log(c)) * idf[vocabulary[g]] for g, c in counts.items() if g in vocabulary}
    norm = math.sqrt(sum(w * w for w in weights.values())) or 1.0
    scores = [bias + sum(row[j] * w / norm for j, w in weights.items())
              for row, bias in zip(model["coef"], model["intercept"])]
    top = max(scores)
    exp = [math.exp(s - top) for s in scores]
    total = sum(exp)
    return sorted(((topic, e / total) for topic, e in zip(model["classes"], exp)), key=lambda x: -x[1])


def narrow_curriculum_lines(lines: list[str], text: str) -> tuple[list[str], dict]:
    """lines：每行"知识点编号 章 › 节 › 名称"。返回（缩小后的行，判断记录）；拿不准或出错时原样返回。"""
    try:
        thresholds = _model()["thresholds"]
        ranked = probabilities(text)
        (first, p1), (second, p2) = ranked[0], ranked[1]
        if p1 >= thresholds["one_topic"]:
            chosen = {first}
        elif p1 + p2 >= thresholds["two_topics"]:
            chosen = {first, second}
        else:
            return lines, {"topics": [], "p": round(p1, 3), "kept": len(lines), "total": len(lines)}
        topics = _knowledge_point_topics()
        # 认不出专题的知识点（不在课程树里，或者复数、平面向量）一律留着
        kept = [line for line in lines if topics.get(line.split(" ", 1)[0], ALWAYS) in chosen | {ALWAYS}]
        if len(kept) < MIN_KEPT:
            return lines, {"topics": sorted(chosen), "p": round(p1, 3), "kept": len(lines), "total": len(lines)}
        return kept, {"topics": sorted(chosen), "p": round(p1, 3), "kept": len(kept), "total": len(lines)}
    except Exception:  # noqa: BLE001 - 识别出任何问题都不能影响出题，照旧给全部知识点
        return lines, {"topics": [], "p": None, "kept": len(lines), "total": len(lines), "error": True}
