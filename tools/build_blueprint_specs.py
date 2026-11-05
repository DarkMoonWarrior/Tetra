"""把 D:\\高中数学\\模拟卷\\进度.md 里 9 套卷、每套 19 道题的命题要求整理成 JSON。

这些要求在界面里作为"示例要求"，也是后面对比实验的测试集。
每条有两种写法：topic 是纯文本（填进输入框、搜索、发给模型）；topic_tex 是同一句话的排版写法，
公式写在 $…$ 里，界面上用 KaTeX 排。topic_tex 是对照试卷的记号手写的，放在 tools/blueprint_tex.json，
没有公式也没有数字的那些条不用写。
用法：python tools/build_blueprint_specs.py [进度.md 路径]
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = Path("D:/高中数学/模拟卷/进度.md")
TEX_SOURCE = Path(__file__).with_name("blueprint_tex.json")

TYPE_BY_LABEL = {
    "单选": "single_choice",
    "多选": "multiple_choice",
    "填空": "fill_blank",
    "解答": "open_response",
}
FIRST_NUMBER = {"单选": 1, "多选": 9, "填空": 12, "解答": 15}

# 题号 → 目标难度，依据 命题蓝图.md：单选 1～4 送分、5～7 中档、8 中难；
# 填空 12 送分、13 中档、14 中难；解答题由易到难。
DIFFICULTY_BY_NUMBER = {
    1: 1, 2: 1, 3: 2, 4: 2, 5: 3, 6: 3, 7: 3, 8: 4,
    9: 3, 10: 3, 11: 4,
    12: 2, 13: 3, 14: 4,
    15: 3, 16: 3, 17: 4, 18: 4, 19: 5,
}


def parse(text: str) -> list[dict]:
    specs: list[dict] = []
    for block in re.split(r"\n### ", text)[1:]:
        title = block.splitlines()[0]
        paper = re.match(r"(卷[一二三四五六七八九十]+)", title)
        if not paper:
            continue
        for line in block.splitlines():
            match = re.match(r"^(单选|多选|填空|解答)：(.*)", line.strip())
            if not match:
                continue
            label, items = match.groups()
            topics = [item.strip() for item in items.split("｜") if item.strip()]
            for offset, topic in enumerate(topics):
                number = FIRST_NUMBER[label] + offset
                # 只有解答题写成"15 四棱锥……"，去掉开头的题号；其他题型开头的数字是内容（"1−i 为…""5 人取礼物…"）
                if label == "解答":
                    topic = re.sub(r"^\d+\s+", "", topic)
                # eˣ 的上标字母在普通字体里小得看不清，输入框里写成和其他条一样的 e^x
                topic = topic.replace("eˣ", "e^x")
                specs.append(
                    {
                        "id": f"{paper.group(1)}-{number:02d}",
                        "paper": paper.group(1),
                        "number": number,
                        "question_type": TYPE_BY_LABEL[label],
                        "type_label": label + "题",
                        "topic": topic,
                        "target_difficulty": DIFFICULTY_BY_NUMBER.get(number, 3),
                    }
                )
    return specs


def chinese(text: str) -> str:
    return "".join(re.findall(r"[一-鿿]", text))


def add_tex(specs: list[dict]) -> None:
    """并进手写的排版写法；进度.md 改过而这里没跟着改时（中文对不上）报出来，不让两种写法悄悄不一致。"""
    tex = json.loads(TEX_SOURCE.read_text(encoding="utf-8"))
    known = {spec["id"] for spec in specs}
    problems = [f"{key}：进度.md 里没有这一条" for key in tex if key not in known]
    for spec in specs:
        written = tex.get(spec["id"])
        if written is None:
            if re.search(r"[0-9a-zA-Z=√π²³]", re.sub(r"\bAI\b|\bAQI\b|Logistic|\bvs\b", "", spec["topic"])):
                problems.append(f"{spec['id']}：有公式但没写排版写法：{spec['topic']}")
            continue
        if chinese(written) != chinese(spec["topic"]):
            problems.append(f"{spec['id']}：中文和原文不一致：{spec['topic']} ／ {written}")
        spec["topic_tex"] = written
    if problems:
        raise SystemExit("blueprint_tex.json 要更新：\n" + "\n".join(problems))


def main() -> None:
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SOURCE
    specs = parse(source.read_text(encoding="utf-8"))
    add_tex(specs)
    target = ROOT / "data" / "blueprint_specs.json"
    target.write_text(json.dumps(specs, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(specs)} 条命题要求（{sum('topic_tex' in spec for spec in specs)} 条带公式）→ {target}")


if __name__ == "__main__":
    main()
