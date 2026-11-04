"""把知识树 CSV（人教 A 版或 B 版）整理成"册 → 章 → 节 → 知识点"的嵌套结构，给界面的选择器用。"""

from __future__ import annotations

import csv
from pathlib import Path

GRADE_BY_VOLUME = {
    "必修第一册": "高一",
    "必修第二册": "高一",
    "必修第三册": "高一",  # B 版有四本必修
    "必修第四册": "高一",
    "选择性必修第一册": "高二",
    "选择性必修第二册": "高二",
    "选择性必修第三册": "高二",
}


def load_tree(csv_path: Path) -> list[dict]:
    with Path(csv_path).open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    nodes = {
        row["node_id"]: {
            "id": row["node_id"],
            "name": row["name"],
            "type": row["node_type"],
            "children": [],
        }
        for row in rows
    }
    roots: list[dict] = []
    for row in rows:
        node = nodes[row["node_id"]]
        if row["node_type"] == "volume":
            node["grade"] = GRADE_BY_VOLUME.get(row["name"], "")
            roots.append(node)
        parent = nodes.get(row["parent_id"])
        if parent is not None and row["node_type"] != "volume":
            parent["children"].append(node)
    return roots
