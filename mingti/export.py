"""试卷导出。

- 打印页：HTML，公式交给 KaTeX 排。右侧的试卷预览、"打印"和 PDF 用的都是它，所以三者长得一样。
- PDF：用本机的 Edge 或 Chrome 在后台打开打印页，存成 PDF。
- Word：python-docx 排版。公式先用 latex2mathml 转成 MathML，再用 Office 自带的 MML2OMML.XSL
  转成 Word 公式（可以在 Word 里直接改）；转不了的公式保留 LaTeX 原文。
"""

from __future__ import annotations

import html
import io
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Emu, Mm, Pt
from lxml import etree

from mingti.bank import TYPE_ORDER
from mingti.config import RUNTIME_DIR
from mingti.figures import svg_to_png

logger = logging.getLogger("mingti.export")

TYPE_TITLE = {
    "single_choice": "单项选择题",
    "multiple_choice": "多项选择题",
    "fill_blank": "填空题",
    "open_response": "解答题",
}
CN_NUMBERS = "一二三四五六七八九十"
MATH = re.compile(r"\$\$(.+?)\$\$|\\\[(.+?)\\\]|\$(.+?)\$|\\\((.+?)\\\)", re.S)

XSL_CANDIDATES = (
    r"C:\Program Files\Microsoft Office\root\Office16\MML2OMML.XSL",
    r"C:\Program Files (x86)\Microsoft Office\root\Office16\MML2OMML.XSL",
    r"C:\Program Files\Microsoft Office\Office16\MML2OMML.XSL",
    r"C:\Program Files (x86)\Microsoft Office\Office16\MML2OMML.XSL",
)
BROWSER_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
)


class ExportError(Exception):
    """导出失败，消息直接显示给用户。"""


def score_text(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def layout(paper: dict) -> list[dict]:
    """按题型分成几大题，题号从 1 连续往下编；题库里已经删掉的题跳过。"""
    groups: dict[str, list[dict]] = {kind: [] for kind in TYPE_ORDER}
    for item in paper["items"]:
        question = item.get("question")
        if question:
            groups[question["question_type"]].append(item)
    sections: list[dict] = []
    number = 0
    for kind in TYPE_ORDER:
        if not groups[kind]:
            continue
        rows = []
        for item in groups[kind]:
            number += 1
            rows.append({"number": number, "score": item["score"], "question": item["question"]})
        total = sum(row["score"] for row in rows)
        scores = {row["score"] for row in rows}
        each = f"每小题 {score_text(rows[0]['score'])} 分，" if len(scores) == 1 else ""
        note = f"共 {len(rows)} 小题，{each}共 {score_text(total)} 分"
        sections.append(
            {"heading": f"{CN_NUMBERS[len(sections)]}、{TYPE_TITLE[kind]}（{note}）", "kind": kind, "rows": rows, "total": total}
        )
    return sections


def option_columns(options: list[dict]) -> int:
    """和界面里的排法一致：选项短就一行四个，长一点一行两个，再长一行一个。"""
    longest = max(
        (len(re.sub(r"[{}$\s^_]", "", re.sub(r"\\[a-zA-Z]+", "x", option["text"]))) for option in options),
        default=0,
    )
    return 4 if longest <= 10 else 2 if longest <= 24 else 1


def answer_text(question: dict) -> str:
    """选择题的答案按试卷习惯写成"ABC"。"""
    if question["question_type"] in ("single_choice", "multiple_choice"):
        letters = "".join(re.findall(r"[A-H]", question["answer"].upper()))
        return letters or question["answer"]
    return question["answer"]


def paper_meta(paper: dict, total: float) -> str:
    parts = [f"满分 {score_text(total)} 分"]
    if paper.get("minutes"):
        parts.append(f"考试时间 {paper['minutes']} 分钟")
    return "　".join(parts)


# ---------- 打印页（预览、打印、PDF） ----------

PRINT_CSS = """
@page { size: A4; margin: 16mm 16mm 18mm; }
* { box-sizing: border-box; }
body { margin: 0; color: #000; font: 10.5pt/1.85 "SimSun", "Songti SC", "Noto Serif SC", serif; }
@media screen {
  html { background: #ececec; }
  body { padding: 20px 0; }
  .sheet { width: 210mm; min-height: 297mm; margin: 0 auto; padding: 16mm; background: #fff;
           box-shadow: 0 1px 3px rgba(0,0,0,.08), 0 10px 30px -12px rgba(0,0,0,.28); }
}
@media screen and (max-width: 860px) {
  html { background: #fff; }
  body { padding: 0; }
  .sheet { width: auto; min-height: 0; padding: 22px 26px 40px; box-shadow: none; }
}
h1 { margin: 0 0 4pt; text-align: center; font: bold 16pt/1.4 "SimHei", "Microsoft YaHei", sans-serif; }
.subtitle { margin: 0; text-align: center; font-size: 11pt; }
.meta, .fill { margin: 2pt 0 0; text-align: center; font-size: 10pt; }
.fill { margin-bottom: 10pt; }
h2 { margin: 12pt 0 4pt; font: bold 11pt/1.6 "SimHei", "Microsoft YaHei", sans-serif; break-after: avoid; }
.q { margin: 0 0 6pt; break-inside: avoid; }
.no { font-weight: bold; }
.tx { white-space: pre-line; }
.options { display: grid; gap: 0 10pt; margin: 2pt 0 0 1.5em; }
.c4 { grid-template-columns: repeat(4, minmax(0, 1fr)); }
.c2 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.c1 { grid-template-columns: minmax(0, 1fr); }
.space { height: 36mm; }
.fig { margin: 4pt 0 2pt; text-align: center; }
.fig svg { max-width: 64%; height: auto; }
.key { break-before: page; }
.a { margin: 0 0 8pt; break-inside: avoid; }
.a p { margin: 0; }
.a ol { margin: 0; padding-left: 2em; }
.label { font-family: "SimHei", "Microsoft YaHei", sans-serif; }
.empty { padding: 40mm 0; color: #666; text-align: center; }
.katex { font-size: 1.04em; }
/* 行内分数排成和正文一样大，比一行字高：含分数的那一段上下各留一点，相邻两行的分数不会碰在一起 */
.katex .katex-base:has(.mfrac) { margin-block: 0.2em; }
.katex-display .katex-base:has(.mfrac) { margin-block: 0; }
/* 挪进公式里的中文标点（_glue_punctuation）用回正文的字体字号 */
.katex .cjk_fallback { font-family: "SimSun", "Songti SC", "Noto Serif SC", serif; font-size: calc(1em / 1.04); }
"""

# 浏览器在公式和紧跟着的字之间总可以折行，"$…$，"里的"，"会单独跑到下一行开头。
# 把紧挨着行内公式的中文标点挪进公式（\text{，}），和公式排在一起（网页上 shared.js 的 gluePunctuation 同样处理）
_INLINE_MATH = re.compile(r"([（《「『【]*)(?<![$\\])\$(?!\$)((?:\\.|[^$\\])+)\$(?!\$)([，。．；：、！？）》」』】]*)")


def _glue_punctuation(text: str) -> str:
    def glue(match: re.Match) -> str:
        opening, body, closing = match.groups()
        if not opening and not closing:
            return match.group(0)
        before = rf"\text{{{opening}}}" if opening else ""
        after = rf"\text{{{closing}}}" if closing else ""
        return f"${before}{body}{after}$"

    return _INLINE_MATH.sub(glue, text)


def _text(value: str) -> str:
    return f'<span class="tx">{html.escape(_glue_punctuation(value or ""))}</span>'


def print_html(paper: dict, *, answers: bool, auto_print: bool = False, part: str = "all") -> str:
    if paper.get("style") == "standard":
        return standard_html(paper, part=part, answers=answers, auto_print=auto_print)
    sections = layout(paper)
    total = sum(section["total"] for section in sections)
    out = [f"<h1>{html.escape(paper['title'])}</h1>"]
    if paper.get("subtitle"):
        out.append(f'<p class="subtitle">{html.escape(paper["subtitle"])}</p>')
    if sections:
        out.append(f'<p class="meta">{paper_meta(paper, total)}</p>')
        out.append('<p class="fill">班级＿＿＿＿＿＿　姓名＿＿＿＿＿＿　得分＿＿＿＿＿＿</p>')
    else:
        out.append('<p class="empty">这份试卷还没有题目。到题库里勾选题目，再加进来。</p>')
    for section in sections:
        out.append(f"<section><h2>{html.escape(section['heading'])}</h2>")
        for row in section["rows"]:
            question = row["question"]
            score = f"（{score_text(row['score'])} 分）" if section["kind"] == "open_response" else ""
            out.append(f'<div class="q"><div><span class="no">{row["number"]}．</span>{score}{_text(question["stem"])}</div>')
            if question.get("figure_svg"):
                out.append(f'<div class="fig">{question["figure_svg"]}</div>')
            if question["options"]:
                out.append(f'<div class="options c{option_columns(question["options"])}">')
                for option in question["options"]:
                    out.append(f"<div>{html.escape(option['key'])}．{_text(option['text'])}</div>")
                out.append("</div>")
            if section["kind"] == "open_response":
                out.append('<div class="space"></div>')
            out.append("</div>")
        out.append("</section>")
    if answers and sections:
        out.append('<section class="key"><h2>参考答案与解析</h2>')
        for section in sections:
            for row in section["rows"]:
                question = row["question"]
                out.append(f'<div class="a"><p><span class="no">{row["number"]}．</span><span class="label">答案　</span>{_text(answer_text(question))}</p>')
                if question["solution_method"]:
                    out.append(f'<p><span class="label">分析　</span>{_text(question["solution_method"])}</p>')
                steps = question["solution_steps"]
                if steps and (question.get("full_solution_steps") or question.get("imported")):
                    # 整理过的解析、从原卷导入的解答按试卷排：一行一句、不编号；没整理过的完整推导才编号
                    out.append(f'<p><span class="label">详解　</span>{_text(steps[0])}</p>')
                    out.extend(f"<p>{_text(step)}</p>" for step in steps[1:])
                elif steps:
                    out.append("<ol>" + "".join(f"<li>{_text(step)}</li>" for step in steps) + "</ol>")
                out.append("</div>")
        out.append("</section>")
    trigger = "window.addEventListener('load', () => setTimeout(() => window.print(), 300));" if auto_print else ""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{html.escape(paper['title'])}</title>
<link rel="stylesheet" href="/vendor/katex/katex.min.css">
<style>{PRINT_CSS}</style>
</head>
<body>
<main class="sheet">{''.join(out)}</main>
<script src="/vendor/katex/katex.min.js"></script>
<script src="/vendor/katex/contrib/auto-render.min.js"></script>
<script>
renderMathInElement(document.body, {{
  delimiters: [
    {{ left: '$$', right: '$$', display: true }},
    {{ left: '\\\\[', right: '\\\\]', display: true }},
    {{ left: '$', right: '$', display: false }},
    {{ left: '\\\\(', right: '\\\\)', display: false }}
  ],
  // 行内公式也用显示样式，分数和正文一样大（和网页上的题目一致）
  preProcess: (math) => '\\\\displaystyle ' + math,
  throwOnError: false
}});
{trigger}
</script>
</body>
</html>"""


# ---------- 标准试卷：照高考数学卷（D:\高中数学\模拟卷 的版式）----------
# 184×260 mm；Times New Roman + 宋体五号；黑体标题；绝密★启用前、考试名称、"数　学"、三条注意事项；
# 新高考的大题说明；题号"1．"、选项"A．"；句号用"．"；页脚"数学试题　第X页（共Y页）"。
# 答案另起一份：【答案速查】【命题双向细目表】和各题解析，页码单独从 1 编。

STANDARD_CSS = """
@page { size: 184mm 260mm; margin: 17mm 20mm 19mm 20mm; }
@page exam { @bottom-center { content: "数学试题　第" counter(page) "页（共" counter(pages) "页）"; font-family: "Times New Roman", "SimSun"; font-size: 9pt; } }
@page answers { @bottom-center { content: "数学答案　第" counter(page) "页（共" counter(pages) "页）"; font-family: "Times New Roman", "SimSun"; font-size: 9pt; } }
.exam { page: exam; }
.answers { page: answers; }
.answers.after { break-before: page; }
* { box-sizing: border-box; }
body { font-family: "Times New Roman", "SimSun", serif; font-size: 10.5pt; line-height: 1.9; color: #000; margin: 0; }
@media screen {
  html { background: #ececec; }
  body { padding: 20px 0; }
  .exam, .answers { width: 184mm; margin: 0 auto 20px; padding: 17mm 20mm 19mm; background: #fff;
    box-shadow: 0 1px 3px rgba(0,0,0,.08), 0 10px 30px -12px rgba(0,0,0,.28); }
}
@media screen and (max-width: 760px) {
  html { background: #fff; }
  body { padding: 0; }
  .exam, .answers { width: auto; padding: 22px 20px 40px; box-shadow: none; }
}
.katex { font-size: 1.07em; }
.katex, .katex .mathrm, .katex .textrm { font-family: "Times New Roman", KaTeX_Main, serif; }
.katex .mathnormal, .katex .mathit { font-family: "Times New Roman", KaTeX_Math, serif; font-style: italic; }
.katex .mord.mathnormal { margin-right: 0 !important; }
.katex .katex-base:has(.mfrac) { margin-block: 0.2em; }
.katex-display .katex-base:has(.mfrac) { margin-block: 0; }
.katex .cjk_fallback { font-family: "SimSun", "Songti SC", serif; font-size: calc(1em / 1.07); }
p { margin: 0; padding-top: 2px; text-align: justify; break-inside: avoid; }
.secret { display: flex; justify-content: space-between; font-family: "SimHei", sans-serif; font-size: 9pt; line-height: 1.6; margin: 0 0 6px; }
h1 { font-family: "SimSun", serif; font-weight: bold; font-size: 15.5pt; text-align: center; margin: 2px 0 4px; letter-spacing: 0.5px; }
h2 { font-family: "SimHei", sans-serif; font-weight: normal; font-size: 24pt; text-align: center; margin: 2px 0 10px; line-height: 1.6; }
h2.ans-title { font-size: 18pt; }
.notice { font-size: 10.5pt; margin-bottom: 12px; }
.notice p { text-indent: 2em; padding-top: 0; }
.notice p.nh { text-indent: 0; font-family: "SimHei", sans-serif; }
.sec { font-family: "SimHei", sans-serif; margin: 12px 0 6px; padding-left: 2em; text-indent: -2em; break-after: avoid; }
.q { margin: 0 0 9px; break-inside: avoid; }
.q.long { break-inside: auto; margin-bottom: 10px; }
.q.long > p:first-child { break-after: avoid; }
.q p { text-align: left; }
.qn { font-weight: normal; }
.qh1 { padding-left: 1.5em; text-indent: -1.5em; }
.qh2 { padding-left: 2em; text-indent: -2em; }
.q p.stem, .q p.sub-q { text-indent: 2em; }
.tx { white-space: pre-line; }
.opts { display: grid; padding-left: 2em; column-gap: 10px; row-gap: 6px; margin-top: 2px; }
.qh1 ~ .opts { padding-left: 1.5em; }
.o4 { grid-template-columns: repeat(4, minmax(0, 1fr)); }
.o2 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.o1 { grid-template-columns: 1fr; }
.q::after { content: ""; display: block; clear: both; }
.fig { text-align: center; break-inside: avoid; }
.fig.right { float: right; margin: 0 0 4px 14px; }
.fig.center { margin: 4px auto; }
.fig svg { max-width: 100%; height: auto; }
.cap { font-size: 9pt; line-height: 1.2; }
.blank { display: inline-block; border-bottom: 1px solid #000; width: 7em; }
.c { text-align: center; }
.small { font-size: 9pt; }
.sol { margin: 0 0 8px; }
.sol p { margin: 1px 0; text-align: left; }
.an { font-family: "SimHei", sans-serif; font-weight: normal; }
table.key { border-collapse: collapse; margin: 4px auto 10px; line-height: 1.45; break-inside: avoid; }
table.key td, table.key th { border: 1px solid #000; padding: 2px 6px; text-align: center; font-weight: normal; white-space: nowrap; }
table.key td.t { white-space: normal; text-align: left; }
.empty { padding: 40mm 0; color: #666; text-align: center; }
"""

# 选项能排一行就排一行，否则两行两列，再不行每个选项各占一行（照高考卷的排法，量实际宽度）
STANDARD_FIT_SCRIPT = """
document.querySelectorAll('.opts').forEach(function (g) {
  function wraps() {
    return Array.prototype.some.call(g.children, function (s) {
      var old = s.style.whiteSpace; s.style.whiteSpace = 'nowrap';
      var over = s.scrollWidth > s.clientWidth + 1; s.style.whiteSpace = old; return over;
    });
  }
  if (g.classList.contains('o4') && wraps()) { g.classList.remove('o4'); g.classList.add('o2'); }
  if (g.classList.contains('o2') && wraps()) { g.classList.remove('o2'); g.classList.add('o1'); }
});
"""

NOTICE = (
    "答卷前，考生务必将自己的姓名、准考证号填写在答题卡上。",
    "回答选择题时，选出每小题答案后，用铅笔把答题卡上对应题目的答案标号涂黑。如需改动，用橡皮擦干净后，再选涂其他答案标号。"
    "回答非选择题时，将答案写在答题卡上。写在本试卷上无效。",
    "考试结束后，将本试卷和答题卡一并交回。",
)
LEVEL_WORD = {1: "易", 2: "易", 3: "中", 4: "中难", 5: "难"}
STANDARD_ANSWER_TITLE = {
    "single_choice": "单项选择题", "multiple_choice": "多项选择题", "fill_blank": "填空题", "open_response": "解答题",
}


def default_exam_name(today: datetime | None = None) -> str:
    """下一次高考的年份：7 月以后算明年。"""
    today = today or datetime.now()
    return f"{today.year + (1 if today.month >= 7 else 0)}年普通高等学校招生全国统一考试"


def standard_layout(paper: dict) -> list[dict]:
    """标准试卷的大题：按题型分，题号连续；说明照新高考数学卷的写法，分值按实际给的分算。"""
    sections = []
    for section in layout(paper):
        rows, kind = section["rows"], section["kind"]
        scores = {row["score"] for row in rows}
        each = f"每小题 {score_text(rows[0]['score'])} 分，" if len(scores) == 1 else ""
        count, total = len(rows), score_text(section["total"])
        if kind == "single_choice":
            name, desc = "选择题", f"本题共 {count} 小题，{each}共 {total} 分。在每小题给出的四个选项中，只有一项是符合题目要求的。"
        elif kind == "multiple_choice":
            full = score_text(rows[0]["score"]) if len(scores) == 1 else "满"
            desc = (f"本题共 {count} 小题，{each}共 {total} 分。在每小题给出的选项中，有多项符合题目要求。"
                    f"全部选对的得 {full} 分，部分选对的得部分分，有选错的得 0 分。")
            name = "选择题"
        elif kind == "fill_blank":
            name, desc = "填空题", f"本题共 {count} 小题，{each}共 {total} 分。"
        else:
            name, desc = "解答题", f"本题共 {count} 小题，共 {total} 分。解答应写出文字说明、证明过程或演算步骤。"
        sections.append({**section, "name": name, "desc": desc, "heading": f"{CN_NUMBERS[len(sections)]}、{name}："})
    return sections


_CHOICE_TAIL = re.compile(r"[（(][\s\u3000]*[)）][。．.]?\s*$")
_BLANK = re.compile(r"_{3,}|＿{2,}")


def _std(text: str, *, choice: bool = False) -> str:
    """标准试卷里的一段文字：选择题去掉末尾的（　　），句号写成"．"，横线空写成下划线，公式交给 KaTeX。"""
    text = (text or "").strip()
    if choice:
        text = _CHOICE_TAIL.sub("", text).rstrip()
    # 公式外面的文字：句号写成"．"，横线空先换成占位符（转义 HTML 之后再换成下划线），公式原样留给 KaTeX
    plain = lambda part: _BLANK.sub("\x1f", part.replace("。", "．"))  # noqa: E731
    out, position = [], 0
    for match in MATH.finditer(text):
        out.append(plain(text[position:match.start()]))
        out.append(match.group(0))
        position = match.end()
    out.append(plain(text[position:]))
    escaped = html.escape(_glue_punctuation("".join(out))).replace("\x1f", '<span class="blank"></span>')
    return f'<span class="tx">{escaped}</span>'


def _std_plain(text: str, *, choice: bool = False) -> str:
    """Word 里的标准试卷文字：和 _std 一样改写，但不转 HTML；横线空写成一段下划线。"""
    text = (text or "").strip()
    if choice:
        text = _CHOICE_TAIL.sub("", text).rstrip()
    out, position = [], 0
    for match in MATH.finditer(text):
        out.append(_BLANK.sub("＿＿＿＿＿＿", text[position:match.start()].replace("。", "．")))
        out.append(match.group(0))
        position = match.end()
    out.append(_BLANK.sub("＿＿＿＿＿＿", text[position:].replace("。", "．")))
    return "".join(out)


_SUBQ = re.compile(r"^\s*[（(]\s*\d+\s*[)）]")


def _stem_parts(stem: str) -> tuple[str, list[str]]:
    """解答题题干拆成主干和各小问：以（1）(1) 开头的行是小问。"""
    lines = [line for line in (stem or "").split("\n") if line.strip()]
    main, subs = [], []
    for line in lines:
        (subs if _SUBQ.match(line) or subs else main).append(line.strip())
    return "\n".join(main), subs


def _figure(svg: str, number: int) -> str:
    if not svg:
        return ""
    width = re.search(r'width="([\d.]+)"', svg)
    side = "right" if width and float(width.group(1)) <= 300 else "center"
    return f'<div class="fig {side}">{svg}<div class="cap">（第 {number} 题图）</div></div>'


def _standard_question(row: dict, kind: str) -> str:
    question, number = row["question"], row["number"]
    hang = "qh1" if number < 10 else "qh2"
    choice = kind in ("single_choice", "multiple_choice")
    figure = _figure(question.get("figure_svg") or "", number)
    if kind == "open_response":
        main, subs = _stem_parts(question["stem"])
        parts = [f'<p class="{hang}"><b class="qn">{number}．</b>（{score_text(row["score"])} 分）</p>']
        if main:
            parts.append(f'<p class="stem">{_std(main)}</p>')
        parts += [f'<p class="sub-q">{_std(sub)}</p>' for sub in subs]
        return f'<div class="q long">{figure}{"".join(parts)}</div>'
    body = f'<p class="{hang}"><b class="qn">{number}．</b>{_std(question["stem"], choice=choice)}</p>'
    if question["options"]:
        columns = option_columns(question["options"])
        options = "".join(f'<span>{html.escape(o["key"])}．{_std(o["text"])}</span>' for o in question["options"])
        body += f'<div class="opts o{columns}">{options}</div>'
    return f'<div class="q">{figure}{body}</div>'


def _standard_exam(paper: dict, sections: list[dict]) -> str:
    tag = html.escape(paper.get("tag") or "")
    name = html.escape(paper.get("exam_name") or default_exam_name())
    notice = "".join(f"<p>{i}．{html.escape(text)}</p>" for i, text in enumerate(NOTICE, start=1))
    out = [
        f'<div class="secret"><span>绝密★启用前</span><span>{tag}</span></div>',
        f"<h1>{name}</h1><h2>数　学</h2>",
        f'<div class="notice"><p class="nh">注意事项：</p>{notice}</div>',
    ]
    if not sections:
        out.append('<p class="empty">这份试卷还没有题目。到题库里勾选题目，再加进来。</p>')
    for section in sections:
        out.append(f'<div class="sec">{section["heading"]}{section["desc"]}</div>')
        out.extend(_standard_question(row, section["kind"]) for row in section["rows"])
    return f'<section class="exam">{"".join(out)}</section>'


def _standard_answers(paper: dict, sections: list[dict], *, after: bool) -> str:
    name = html.escape(paper.get("exam_name") or default_exam_name())
    tag = paper.get("tag") or ""
    out = [f"<h1>{name}{html.escape('·' + tag) if tag else ''}</h1><h2 class=\"ans-title\">数学参考答案及解析</h2>"]
    choices = [row for s in sections if s["kind"] in ("single_choice", "multiple_choice") for row in s["rows"]]
    fills = [row for s in sections if s["kind"] == "fill_blank" for row in s["rows"]]
    if choices or fills:
        out.append('<div class="sec">【答案速查】</div>')
    for start in range(0, len(choices), 12):
        chunk = choices[start:start + 12]
        out.append(
            '<table class="key"><tr><th>题号</th>' + "".join(f"<td>{row['number']}</td>" for row in chunk)
            + "</tr><tr><th>答案</th>" + "".join(f"<td>{html.escape(answer_text(row['question']))}</td>" for row in chunk)
            + "</tr></table>"
        )
    if fills:
        out.append('<p class="c">' + "　　　".join(f"{row['number']}．{_std(answer_text(row['question']))}" for row in fills) + "</p>")
    if any(s["kind"] == "multiple_choice" for s in sections):
        out.append('<p class="c small">（多选题：全部选对得满分；部分选对的得部分分；有选错的得 0 分．）</p>')
    rows = [row for s in sections for row in s["rows"]]
    if rows:
        def cells(row):
            if row is None:
                return "<td></td><td></td><td></td><td></td>"
            question = row["question"]
            topic = html.escape((question.get("knowledge") or "").split(" › ")[-1] or "—")
            return (f"<td>{row['number']}</td><td class=\"t\">{topic}</td>"
                    f"<td>{LEVEL_WORD.get(question.get('difficulty') or 3, '中')}</td><td>{score_text(row['score'])}</td>")
        half = (len(rows) + 1) // 2
        table = ['<table class="key" style="font-size:9.5pt"><tr>' + "<th>题号</th><th>考查内容</th><th>难度</th><th>分值</th>" * 2 + "</tr>"]
        for i in range(half):
            table.append(f"<tr>{cells(rows[i])}{cells(rows[i + half] if i + half < len(rows) else None)}</tr>")
        out.append('<div class="sec">【命题双向细目表】</div>' + "".join(table) + "</table>")
    for section in sections:
        out.append(f'<div class="sec">{CN_NUMBERS[sections.index(section)]}、{STANDARD_ANSWER_TITLE[section["kind"]]}</div>')
        for row in section["rows"]:
            question = row["question"]
            steps = [step for step in question["solution_steps"] if step.strip()] or [question.get("solution_method") or ""]
            lead = "解：" if section["kind"] == "open_response" else f"{html.escape(answer_text(question))}　"
            if section["kind"] == "fill_blank":
                lead = f"{_std(answer_text(question))}　"
            lines = [f'<p><b class="an">{row["number"]}．{lead}</b>{_std(steps[0])}</p>']
            lines += [f"<p>{_std(step)}</p>" for step in steps[1:]]
            out.append(f'<div class="sol">{"".join(lines)}</div>')
    return f'<section class="answers{" after" if after else ""}">{"".join(out)}</section>'


def standard_html(paper: dict, *, part: str = "all", answers: bool = True, auto_print: bool = False) -> str:
    """part：exam 只要试题，answers 只要答案（导出 PDF 时分开印，各自从第 1 页编页码），all 两份连着（预览、打印）。"""
    sections = standard_layout(paper)
    body = []
    if part in ("all", "exam"):
        body.append(_standard_exam(paper, sections))
    if part == "answers" or (part == "all" and answers and sections):
        body.append(_standard_answers(paper, sections, after=part == "all"))
    trigger = "window.addEventListener('load', () => setTimeout(() => window.print(), 300));" if auto_print else ""
    title = html.escape(paper["title"])
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{title}</title>
<link rel="stylesheet" href="/vendor/katex/katex.min.css">
<style>{STANDARD_CSS}</style>
</head>
<body>
{''.join(body)}
<script src="/vendor/katex/katex.min.js"></script>
<script src="/vendor/katex/contrib/auto-render.min.js"></script>
<script>
renderMathInElement(document.body, {{
  delimiters: [
    {{ left: '$$', right: '$$', display: true }},
    {{ left: '\\\\[', right: '\\\\]', display: true }},
    {{ left: '$', right: '$', display: false }},
    {{ left: '\\\\(', right: '\\\\)', display: false }}
  ],
  preProcess: (math) => '\\\\displaystyle ' + math,
  throwOnError: false,
  strict: false
}});
{STANDARD_FIT_SCRIPT}
{trigger}
</script>
</body>
</html>"""


def merge_pdfs(parts: list[bytes]) -> bytes:
    """试题、答案分开印（各自编页码），再合成一个 PDF。"""
    if len(parts) == 1:
        return parts[0]
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for content in parts:
        for page in PdfReader(io.BytesIO(content)).pages:
            writer.add_page(page)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def find_browser() -> str | None:
    for path in (os.environ.get("PDF_BROWSER"), *BROWSER_CANDIDATES, shutil.which("msedge"), shutil.which("chrome")):
        if path and Path(path).exists():
            return path
    return None


def print_to_pdf(url: str) -> bytes:
    """用无界面的 Edge/Chrome 打开打印页并存成 PDF；用独立的临时用户目录，不影响已经开着的浏览器。"""
    browser = find_browser()
    if not browser:
        raise ExportError("这台电脑上没找到 Edge 或 Chrome，生成不了 PDF。可以点「打印」，在打印窗口里选「另存为 PDF」。")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        target = Path(folder) / "paper.pdf"
        command = [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            f"--user-data-dir={Path(folder) / 'profile'}",
            "--no-pdf-header-footer",
            "--print-to-pdf-no-header",
            "--virtual-time-budget=8000",
            f"--print-to-pdf={target}",
            url,
        ]
        try:
            subprocess.run(
                command,
                capture_output=True,
                timeout=90,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            raise ExportError("生成 PDF 超时了，再试一次；还不行的话，用「打印」另存为 PDF。") from exc
        if not target.exists() or target.stat().st_size < 1000:
            raise ExportError("PDF 没有生成出来。可以用「打印」，在打印窗口里选「另存为 PDF」。")
        return target.read_bytes()


# ---------- Word ----------


@lru_cache
def _omml_transform():
    path = os.environ.get("MML2OMML_XSL") or next((p for p in XSL_CANDIDATES if Path(p).exists()), None)
    if not path:
        logger.warning("没找到 MML2OMML.XSL，Word 里的公式会保留 LaTeX 原文")
        return None
    try:
        return etree.XSLT(etree.parse(path))
    except (etree.XMLSyntaxError, etree.XSLTParseError, OSError) as exc:
        logger.warning("MML2OMML.XSL 读不了：%s", exc)
        return None


def _clean_latex(latex: str) -> str:
    """把 KaTeX 认识、latex2mathml 不太认识的写法换成等价的普通写法。"""
    replacements = {
        r"\dfrac": r"\frac",
        r"\tfrac": r"\frac",
        r"\leqslant": r"\leq",
        r"\geqslant": r"\geq",
        r"\displaystyle": "",
        r"\lvert": "|",
        r"\rvert": "|",
        r"\lVert": r"\|",
        r"\rVert": r"\|",
    }
    for old, new in replacements.items():
        latex = latex.replace(old, new)
    return latex.strip()


def _math_element(latex: str):
    transform = _omml_transform()
    if transform is None:
        return None
    try:
        from latex2mathml.converter import convert

        return transform(etree.fromstring(convert(_clean_latex(latex)))).getroot()
    except Exception:  # noqa: BLE001 - 个别公式转不了时退回原文，不影响整份试卷
        logger.info("公式没转成 Word 公式，保留原文：%s", latex)
        return None


def _font(run, east_asia: str, size: float | None = None, bold: bool | None = None) -> None:
    run.font.name = "Times New Roman"
    run._element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), east_asia)
    if size:
        run.font.size = Pt(size)
    if bold is not None:
        run.bold = bold


def _add_text(paragraph, text: str, *, bold: bool = False, east_asia: str = "宋体") -> None:
    for i, part in enumerate(text.split("\n")):
        if i:
            paragraph.add_run().add_break()
        if part:
            _font(paragraph.add_run(part), east_asia, bold=bold)


def _add_rich(paragraph, text: str) -> None:
    """文字和 $…$ 公式混排：文字按宋体写，公式转成 Word 公式。"""
    position = 0
    for match in MATH.finditer(text or ""):
        _add_text(paragraph, text[position:match.start()])
        latex = next(group for group in match.groups() if group is not None)
        element = _math_element(latex)
        if element is not None:
            paragraph._p.append(element)
        else:
            run = paragraph.add_run(latex.strip())
            run.italic = True
            run.font.name = "Cambria Math"
        position = match.end()
    _add_text(paragraph, (text or "")[position:])


def _heading(document, text: str, size: float, *, center: bool = False):
    paragraph = document.add_paragraph()
    if center:
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _font(paragraph.add_run(text), "黑体", size, bold=True)
    return paragraph


def _add_figure(document, svg: str) -> None:
    """配图在 Word 里放 PNG（Word 不认 SVG），居中；按 SVG 的像素宽度换算成纸上的宽度。"""
    if not svg:
        return
    png = svg_to_png(svg, RUNTIME_DIR / "figure-cache")
    paragraph = document.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if png is None:
        _add_text(paragraph, "（配图没能转成图片，请在 PDF 里查看）")
        return
    size = re.search(r'width="(\d+)"', svg)
    width_mm = min(80.0, max(30.0, int(size.group(1)) * 0.2646 * 0.8)) if size else 60.0
    paragraph.add_run().add_picture(io.BytesIO(png), width=Mm(width_mm))


def _field(paragraph, code: str) -> None:
    """Word 域（PAGE 当前页、SECTIONPAGES 本节页数），打开文档时 Word 自己算。"""
    run = paragraph.add_run()
    _font(run, "宋体", 9)
    for kind, text in (("begin", None), ("instr", f" {code} "), ("separate", None), ("text", "1"), ("end", None)):
        if kind == "instr":
            node = OxmlElement("w:instrText")
            node.set(qn("xml:space"), "preserve")
            node.text = text
        elif kind == "text":
            node = OxmlElement("w:t")
            node.text = text
        else:
            node = OxmlElement("w:fldChar")
            node.set(qn("w:fldCharType"), kind)
        run._r.append(node)


def _standard_page(section, foot: str, *, restart: bool) -> None:
    section.page_width, section.page_height = Mm(184), Mm(260)
    section.top_margin, section.bottom_margin = Mm(17), Mm(19)
    section.left_margin = section.right_margin = Mm(20)
    section.footer_distance = Mm(9)
    footer = section.footer
    footer.is_linked_to_previous = False
    paragraph = footer.paragraphs[0]
    paragraph.text = ""
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for part in (f"{foot}　第", "PAGE", "页（共", "SECTIONPAGES", "页）"):
        if part.isupper():
            _field(paragraph, part)
        else:
            _font(paragraph.add_run(part), "宋体", 9)
    if restart:
        numbering = OxmlElement("w:pgNumType")
        numbering.set(qn("w:start"), "1")
        section._sectPr.append(numbering)


def _std_paragraph(document, *, hang: float = 0, first: float = 0, center: bool = False):
    paragraph = document.add_paragraph()
    form = paragraph.paragraph_format
    if hang:
        form.left_indent, form.first_line_indent = Pt(hang), Pt(-hang)
    elif first:
        form.first_line_indent = Pt(first)
    if center:
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    return paragraph


def _std_options(document, options: list[dict], indent: float) -> None:
    """选项按列排：一行四个、两个或一个，用制表位对齐（在 Word 里也好改）。"""
    columns = option_columns(options)
    width = Mm(144) - Pt(indent)
    for start in range(0, len(options), columns):
        paragraph = document.add_paragraph()
        paragraph.paragraph_format.left_indent = Pt(indent)
        for k in range(1, columns):  # 制表位从版心左边量起
            paragraph.paragraph_format.tab_stops.add_tab_stop(Emu(int(Pt(indent) + width * k / columns)))
        for k, option in enumerate(options[start:start + columns]):
            if k:
                paragraph.add_run("\t")
            _add_text(paragraph, f"{option['key']}．")
            _add_rich(paragraph, _std_plain(option["text"]))


def standard_docx(paper: dict, *, answers: bool) -> bytes:
    sections = standard_layout(paper)
    document = Document()
    normal = document.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(10.5)
    normal.element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "宋体")
    normal.paragraph_format.space_after = Pt(0)
    normal.paragraph_format.line_spacing = 1.5
    document.core_properties.title = paper["title"]
    _standard_page(document.sections[0], "数学试题", restart=False)
    name = paper.get("exam_name") or default_exam_name()
    tag = paper.get("tag") or ""

    top = document.add_paragraph()
    top.paragraph_format.tab_stops.add_tab_stop(Mm(144), WD_TAB_ALIGNMENT.RIGHT)
    _font(top.add_run(f"绝密★启用前\t{tag}"), "黑体", 9)
    _font(_std_paragraph(document, center=True).add_run(name), "宋体", 15.5, bold=True)
    _font(_std_paragraph(document, center=True).add_run("数　学"), "黑体", 24)
    _font(document.add_paragraph().add_run("注意事项："), "黑体", 10.5)
    for i, text in enumerate(NOTICE, start=1):
        _add_text(_std_paragraph(document, first=21), f"{i}．{text}")

    for section in sections:
        heading = _std_paragraph(document, hang=21)
        heading.paragraph_format.space_before = Pt(6)
        heading.paragraph_format.keep_with_next = True
        _font(heading.add_run(section["heading"] + section["desc"]), "黑体", 10.5)
        for row in section["rows"]:
            question, number, kind = row["question"], row["number"], section["kind"]
            hang = 15.75 if number < 10 else 21
            if kind == "open_response":
                head = _std_paragraph(document, hang=hang)
                head.paragraph_format.keep_with_next = True
                _add_text(head, f"{number}．（{score_text(row['score'])} 分）")
                main, subs = _stem_parts(question["stem"])
                for line in ([main] if main else []) + subs:
                    _add_rich(_std_paragraph(document, first=21), _std_plain(line))
                _add_figure(document, question.get("figure_svg") or "")
                continue
            paragraph = _std_paragraph(document, hang=hang)
            _add_text(paragraph, f"{number}．")
            _add_rich(paragraph, _std_plain(question["stem"], choice=kind in ("single_choice", "multiple_choice")))
            if question.get("figure_svg"):
                paragraph.paragraph_format.keep_with_next = True
            _add_figure(document, question.get("figure_svg") or "")
            if question["options"]:
                _std_options(document, question["options"], hang)

    if answers and sections:
        part = document.add_section(WD_SECTION.NEW_PAGE)
        _standard_page(part, "数学答案", restart=True)
        _font(_std_paragraph(document, center=True).add_run(f"{name}{'·' + tag if tag else ''}"), "宋体", 15.5, bold=True)
        _font(_std_paragraph(document, center=True).add_run("数学参考答案及解析"), "黑体", 18)
        choices = [row for s in sections if s["kind"] in ("single_choice", "multiple_choice") for row in s["rows"]]
        fills = [row for s in sections if s["kind"] == "fill_blank" for row in s["rows"]]
        if choices or fills:
            _font(document.add_paragraph().add_run("【答案速查】"), "黑体", 10.5)
        for start in range(0, len(choices), 12):
            chunk = choices[start:start + 12]
            table = document.add_table(rows=2, cols=len(chunk) + 1)
            table.style = "Table Grid"
            table.alignment = WD_TABLE_ALIGNMENT.CENTER
            for r, label in enumerate(("题号", "答案")):
                table.cell(r, 0).paragraphs[0].add_run(label)
            for c, row in enumerate(chunk, start=1):
                table.cell(0, c).paragraphs[0].add_run(str(row["number"]))
                table.cell(1, c).paragraphs[0].add_run(answer_text(row["question"]))
            for line in table.rows:
                for cell in line.cells:
                    cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
        if fills:
            line = _std_paragraph(document, center=True)
            for k, row in enumerate(fills):
                _add_text(line, f"{'　　　' if k else ''}{row['number']}．")
                _add_rich(line, _std_plain(answer_text(row["question"])))
        for k, section in enumerate(sections):
            heading = document.add_paragraph()
            heading.paragraph_format.space_before = Pt(6)
            _font(heading.add_run(f"{CN_NUMBERS[k]}、{STANDARD_ANSWER_TITLE[section['kind']]}"), "黑体", 10.5)
            for row in section["rows"]:
                question = row["question"]
                steps = [step for step in question["solution_steps"] if step.strip()] or [question.get("solution_method") or ""]
                first = document.add_paragraph()
                lead = "解：" if section["kind"] == "open_response" else f"{answer_text(question)}　"
                _font(first.add_run(f"{row['number']}．"), "黑体", 10.5)
                if section["kind"] == "fill_blank":
                    _add_rich(first, _std_plain(answer_text(question)))
                    _add_text(first, "　")
                else:
                    _font(first.add_run(lead), "黑体", 10.5)
                _add_rich(first, _std_plain(steps[0]))
                for step in steps[1:]:
                    _add_rich(document.add_paragraph(), _std_plain(step))

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def to_docx(paper: dict, *, answers: bool) -> bytes:
    if paper.get("style") == "standard":
        return standard_docx(paper, answers=answers)
    sections = layout(paper)
    total = sum(section["total"] for section in sections)
    document = Document()
    page = document.sections[0]
    page.page_width, page.page_height = Mm(210), Mm(297)
    page.left_margin = page.right_margin = Mm(18)
    page.top_margin, page.bottom_margin = Mm(16), Mm(18)
    normal = document.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(10.5)
    normal.element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "宋体")
    normal.paragraph_format.space_after = Pt(2)
    normal.paragraph_format.line_spacing = 1.35
    document.core_properties.title = paper["title"]

    _heading(document, paper["title"], 16, center=True)
    if paper.get("subtitle"):
        subtitle = document.add_paragraph()
        subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _add_text(subtitle, paper["subtitle"])
    info = document.add_paragraph()
    info.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _add_text(info, f"{paper_meta(paper, total)}\n班级＿＿＿＿＿＿　姓名＿＿＿＿＿＿　得分＿＿＿＿＿＿")

    for section in sections:
        # 大题标题和第一道题排在同一页，不留在上一页的末尾
        _heading(document, section["heading"], 11).paragraph_format.keep_with_next = True
        for row in section["rows"]:
            question = row["question"]
            paragraph = document.add_paragraph()
            _add_text(paragraph, f"{row['number']}．", bold=True)
            if section["kind"] == "open_response":
                _add_text(paragraph, f"（{score_text(row['score'])} 分）")
            _add_rich(paragraph, question["stem"])
            if question.get("figure_svg"):
                paragraph.paragraph_format.keep_with_next = True  # 题干和配图排在同一页
            _add_figure(document, question.get("figure_svg") or "")
            if question["options"]:
                columns = option_columns(question["options"])
                table = document.add_table(rows=math.ceil(len(question["options"]) / columns), cols=columns)
                for index, option in enumerate(question["options"]):
                    cell = table.cell(index // columns, index % columns).paragraphs[0]
                    _add_text(cell, f"{option['key']}．")
                    _add_rich(cell, option["text"])
            if section["kind"] == "open_response":
                for _ in range(6):
                    document.add_paragraph()

    if answers and sections:
        document.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
        _heading(document, "参考答案与解析", 13, center=True)
        for section in sections:
            for row in section["rows"]:
                question = row["question"]
                paragraph = document.add_paragraph()
                _add_text(paragraph, f"{row['number']}．", bold=True)
                _add_text(paragraph, "答案　", east_asia="黑体")
                _add_rich(paragraph, answer_text(question))
                if question["solution_method"]:
                    method = document.add_paragraph()
                    _add_text(method, "分析　", east_asia="黑体")
                    _add_rich(method, question["solution_method"])
                condensed = bool(question.get("full_solution_steps") or question.get("imported"))
                for index, step in enumerate(question["solution_steps"], start=1):
                    line = document.add_paragraph()
                    # 整理过的解析按试卷排：一行一句、不编号（第一行前写"详解"）；没整理过的完整推导才编号
                    if condensed:
                        if index == 1:
                            _add_text(line, "详解　", east_asia="黑体")
                    else:
                        line.paragraph_format.left_indent = Mm(6)
                        _add_text(line, f"{index}．")
                    _add_rich(line, step)

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()
