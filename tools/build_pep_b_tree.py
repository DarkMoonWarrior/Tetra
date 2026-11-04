"""生成人教 B 版（2019）的知识点目录：python tools/build_pep_b_tree.py

B 版和 A 版依据同一份课程标准，知识点基本相同，只是分册、章、节的编排不一样。
所以这里不重新编知识点：A 版的 302 个知识点原样保留（编号不变，出题流程照常认），
按 B 版教材的目录挂到 B 版的册、章、节下面，写成 data/curriculum/pep-b-full-knowledge-tree-v1.csv。

B 版目录照人教社 B 版教材（必修第一～四册、选择性必修第一～三册）：
- 不放没有对应知识点的数学建模、数学探究活动小节
- 每个知识点必须且只能放一次，运行时会检查
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "curriculum" / "pep-a-full-knowledge-tree-v1.csv"
TARGET = ROOT / "data" / "curriculum" / "pep-b-full-knowledge-tree-v1.csv"

# (册, 编号) → [(章, 章号, [(节, [来源…])])]
# 来源："pep_a_…_s1" 表示 A 版这一节剩下的全部知识点；"kp:节编号/知识点名" 表示单独挑出的一个知识点
TREE = [
    ("必修第一册", "r1", [
        ("集合与常用逻辑用语", 1, [
            ("集合", ["pep_a_r1_c1_s1", "pep_a_r1_c1_s2", "pep_a_r1_c1_s3"]),
            ("常用逻辑用语", ["pep_a_r1_c1_s4", "pep_a_r1_c1_s5"]),
        ]),
        ("等式与不等式", 2, [
            ("等式", ["kp:pep_a_r1_c2_s1/等式的基本性质", "kp:pep_a_r1_c2_s3/一元二次方程根与判别式"]),
            ("不等式", ["pep_a_r1_c2_s1", "pep_a_r1_c2_s2", "kp:pep_a_r1_c2_s3/一元二次不等式求解",
                       "kp:pep_a_r1_c2_s3/含参数二次不等式", "kp:pep_a_r1_c2_s3/二次不等式实际应用"]),
        ]),
        ("函数", 3, [
            ("函数的概念与性质", ["pep_a_r1_c3_s1", "pep_a_r1_c3_s2"]),
            ("函数与方程、不等式之间的关系", ["kp:pep_a_r1_c2_s3/二次函数图象与零点", "kp:pep_a_r1_c4_s5/函数零点",
                                          "kp:pep_a_r1_c4_s5/零点存在定理", "kp:pep_a_r1_c4_s5/二分法"]),
            ("函数的应用（一）", ["pep_a_r1_c3_s4"]),
        ]),
    ]),
    ("必修第二册", "r2", [
        ("指数函数、对数函数与幂函数", 4, [
            ("指数与指数函数", ["pep_a_r1_c4_s1", "pep_a_r1_c4_s2"]),
            ("对数与对数函数", ["pep_a_r1_c4_s3", "pep_a_r1_c4_s4"]),
            ("指数函数与对数函数的关系", ["kp:pep_a_r1_c4_s4/指数对数互逆关系"]),
            ("幂函数", ["pep_a_r1_c3_s3"]),
            ("函数的应用（二）", ["pep_a_r1_c4_s5"]),
        ]),
        ("统计与概率", 5, [
            ("统计", ["pep_a_r2_c9_s1", "pep_a_r2_c9_s2"]),
            ("概率", ["pep_a_r2_c10_s1", "pep_a_r2_c10_s2", "pep_a_r2_c10_s3"]),
            ("统计与概率的应用", ["pep_a_r2_c9_s3"]),
        ]),
        ("平面向量初步", 6, [
            ("平面向量及其线性运算", ["pep_a_r2_c6_s1", "pep_a_r2_c6_s2"]),
            ("向量基本定理与向量的坐标", ["pep_a_r2_c6_s3"]),
            ("平面向量线性运算的应用", ["kp:pep_a_r2_c6_s4/向量在几何中的应用", "kp:pep_a_r2_c6_s4/向量在物理中的应用"]),
        ]),
    ]),
    ("必修第三册", "r3", [
        ("三角函数", 7, [
            ("任意角的概念与弧度制", ["pep_a_r1_c5_s1"]),
            ("任意角的三角函数", ["pep_a_r1_c5_s2", "pep_a_r1_c5_s3"]),
            ("三角函数的性质与图像", ["pep_a_r1_c5_s4", "pep_a_r1_c5_s6"]),
            ("周期现象的描述", ["pep_a_r1_c5_s7"]),
        ]),
        ("向量的数量积与三角恒等变换", 8, [
            ("向量的数量积", ["kp:pep_a_r2_c6_s2/向量数量积", "kp:pep_a_r2_c6_s2/向量夹角与模",
                         "kp:pep_a_r2_c6_s3/数量积的坐标表示"]),
            ("三角恒等变换", ["pep_a_r1_c5_s5"]),
        ]),
    ]),
    ("必修第四册", "r4", [
        ("解三角形", 9, [
            ("正弦定理与余弦定理", ["kp:pep_a_r2_c6_s4/余弦定理", "kp:pep_a_r2_c6_s4/正弦定理"]),
            ("正弦定理与余弦定理的应用", ["pep_a_r2_c6_s4"]),
        ]),
        ("复数", 10, [
            ("复数及其几何意义", ["pep_a_r2_c7_s1"]),
            ("复数的运算", ["pep_a_r2_c7_s2"]),
        ]),
        ("立体几何初步", 11, [
            ("空间几何体", ["pep_a_r2_c8_s1", "pep_a_r2_c8_s2", "pep_a_r2_c8_s3"]),
            ("平面的基本事实与推论", ["kp:pep_a_r2_c8_s4/平面的基本事实", "kp:pep_a_r2_c8_s4/空间点线面位置关系"]),
            ("空间中的平行关系", ["pep_a_r2_c8_s4", "pep_a_r2_c8_s5"]),
            ("空间中的垂直关系", ["pep_a_r2_c8_s6"]),
        ]),
    ]),
    ("选择性必修第一册", "s1", [
        ("空间向量与立体几何", 1, [
            ("空间向量及其运算", ["pep_a_s1_c1_s1", "pep_a_s1_c1_s2", "pep_a_s1_c1_s3"]),
            ("空间向量在立体几何中的应用", ["pep_a_s1_c1_s4"]),
        ]),
        ("平面解析几何", 2, [
            ("坐标法", ["kp:pep_a_s1_c2_s3/两点间距离"]),
            ("直线及其方程", ["pep_a_s1_c2_s1", "pep_a_s1_c2_s2", "pep_a_s1_c2_s3"]),
            ("圆及其方程", ["pep_a_s1_c2_s4"]),
            ("椭圆及其方程", ["pep_a_s1_c3_s1"]),
            ("双曲线及其方程", ["pep_a_s1_c3_s2"]),
            ("抛物线及其方程", ["pep_a_s1_c3_s3"]),
            ("直线与圆锥曲线的位置关系", ["kp:pep_a_s1_c3_s1/直线与椭圆", "kp:pep_a_s1_c3_s2/直线与双曲线",
                                        "kp:pep_a_s1_c3_s3/直线与抛物线", "kp:pep_a_s1_c3_s3/圆锥曲线综合"]),
        ]),
    ]),
    ("选择性必修第二册", "s2", [
        ("排列、组合与二项式定理", 3, [
            ("排列与组合", ["pep_a_s3_c6_s1", "pep_a_s3_c6_s2"]),
            ("二项式定理与杨辉三角", ["pep_a_s3_c6_s3"]),
        ]),
        ("概率与统计", 4, [
            ("条件概率与事件的独立性", ["pep_a_s3_c7_s1"]),
            ("随机变量", ["pep_a_s3_c7_s2", "pep_a_s3_c7_s3", "pep_a_s3_c7_s4", "pep_a_s3_c7_s5"]),
            ("统计模型", ["pep_a_s3_c8_s1", "pep_a_s3_c8_s2", "pep_a_s3_c8_s3"]),
        ]),
    ]),
    ("选择性必修第三册", "s3", [
        ("数列", 5, [
            ("数列基础", ["pep_a_s2_c4_s1"]),
            ("等差数列", ["pep_a_s2_c4_s2"]),
            ("等比数列", ["pep_a_s2_c4_s3"]),
            ("数学归纳法", ["pep_a_s2_c4_s4"]),
        ]),
        ("导数及其应用", 6, [
            ("导数", ["pep_a_s2_c5_s1", "pep_a_s2_c5_s2"]),
            ("利用导数研究函数的性质", ["pep_a_s2_c5_s3"]),
            ("利用导数解决实际问题", ["kp:pep_a_s2_c5_s3/导数实际应用"]),
        ]),
    ]),
]


def main() -> int:
    with SOURCE.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = reader.fieldnames
        rows = list(reader)
    kps = [row for row in rows if row["node_type"] == "knowledge_point"]
    by_section: dict[str, list[dict]] = {}
    for row in kps:
        by_section.setdefault(row["parent_id"], []).append(row)

    # 先找出单独挑出来的知识点，整节搬的时候跳过它们
    picked: set[str] = set()
    for _, _, chapters in TREE:
        for _, _, sections in chapters:
            for _, sources in sections:
                for source in sources:
                    if source.startswith("kp:"):
                        section, name = source[3:].split("/", 1)
                        match = [row for row in by_section.get(section, []) if row["name"] == name]
                        if len(match) != 1:
                            print(f"找不到知识点：{source}")
                            return 1
                        picked.add(match[0]["node_id"])

    blank = {column: "" for column in columns}
    out = [{**blank, "node_id": "pep_b", "volume": "全册", "node_type": "textbook", "code": "PEP-B",
            "name": "人教B版高中数学", "description": "普通高中教科书数学B版（2019）七册课程树",
            "status": "draft_for_teacher_review"}]
    placed: list[str] = []
    for volume, vcode, chapters in TREE:
        vid = f"pep_b_{vcode}"
        out.append({**blank, "node_id": vid, "parent_id": "pep_b", "volume": volume, "node_type": "volume",
                    "code": vcode.upper(), "name": volume, "description": f"人教B版普通高中教科书数学{volume}"})
        for chapter, number, sections in chapters:
            cid = f"{vid}_c{number}"
            out.append({**blank, "node_id": cid, "parent_id": vid, "volume": volume, "node_type": "chapter",
                        "code": f"{vcode.upper()}-C{number}", "name": chapter, "description": f"第{number}章 {chapter}"})
            for index, (section, sources) in enumerate(sections, start=1):
                sid = f"{cid}_s{index}"
                out.append({**blank, "node_id": sid, "parent_id": cid, "volume": volume, "node_type": "section",
                            "code": f"{vcode.upper()}-C{number}-S{index}", "name": section,
                            "description": f"{number}.{index} {section}"})
                for source in sources:
                    if source.startswith("kp:"):
                        section_id, name = source[3:].split("/", 1)
                        chosen = [row for row in by_section[section_id] if row["name"] == name]
                    else:
                        chosen = [row for row in by_section.get(source, []) if row["node_id"] not in picked]
                    for row in chosen:
                        out.append({**row, "parent_id": sid, "volume": volume})
                        placed.append(row["node_id"])

    missing = [row["name"] for row in kps if row["node_id"] not in placed]
    twice = sorted({node for node in placed if placed.count(node) > 1})
    if missing or twice:
        print("没放的知识点：", missing, "\n放了两次的：", twice)
        return 1

    # 先修关系里引用的 A 版节、章：换成 B 版里放着它的知识点的那一节；对不上的去掉
    where = {row["node_id"]: row["parent_id"] for row in out if row["node_type"] == "knowledge_point"}
    a_parent = {row["node_id"]: row["parent_id"] for row in rows}

    def under(node: str, ancestor: str) -> bool:
        while node:
            if node == ancestor:
                return True
            node = a_parent.get(node, "")
        return False

    existing = {row["node_id"] for row in out}
    for row in out:
        refs = []
        for ref in filter(None, row.get("prerequisite_node_ids", "").split("|")):
            if ref not in existing:
                first = next((kp["node_id"] for kp in kps if under(kp["node_id"], ref)), None)
                ref = where.get(first) if first else None
            if ref and ref not in refs and ref != row["node_id"]:
                refs.append(ref)
        row["prerequisite_node_ids"] = "|".join(refs)
    with TARGET.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(out)
    counts = {kind: sum(1 for row in out if row["node_type"] == kind) for kind in ("volume", "chapter", "section", "knowledge_point")}
    print(f"已写 {TARGET.name}：{counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
