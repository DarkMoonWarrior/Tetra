"""Shared presentation contract for model-generated mathematical content."""

from __future__ import annotations

import re

from mingti.deepseek import LATEX_N_COMMANDS

MATH_DISPLAY_INSTRUCTIONS = (
    "所有数学表达必须使用 KaTeX 兼容的 LaTeX：行内公式用单个 $...$ 包裹，"
    "需要单独成行的公式用 $$...$$ 包裹；中文说明必须放在公式分隔符之外，"
    "不要使用 Markdown 代码块，也不要把 LaTeX 命令作为未分隔的普通文字输出。"
)


# ---------- 漏写 $ 的公式 ----------
# 题干、选项、答案、解析都要求写成 $…$，但模型偶尔会漏：答案写成裸的 \frac{2}{5}，
# 或者把换行多转义一次，留下字面上的 \n、\par。界面上就会原样露出反斜杠。
# tidy_math_text 只处理 $…$ 之外的这两种情况：字面的 \n、\par 换回换行；
# 含 LaTeX 命令的一段算式补上 $…$。已经写对的内容原样返回，重复调用结果不变。
# 网页上 web/js/automath.js 的 tidyMath 用同一套规则（只在显示时用，处理以前存下的内容）。

# 已经写成公式的部分。和 KaTeX 配对 $ 的方式一样：$…$ 中间可以换行（有些旧内容的公式里夹着换行）
_WRITTEN = re.compile(r"(\$\$[\s\S]+?\$\$|\$[^$]+?\$|\\\([\s\S]+?\\\)|\\\[[\s\S]+?\\\])")
# 可能是一段算式：只由这些 ASCII 字符组成（遇到汉字、中文标点就断开）
_RUN = re.compile(r"[A-Za-z0-9\\{}^_+\-*/=<>()\[\]|,.!:;' ]+")
_COMMAND = re.compile(r"\\[a-zA-Z]")
_CJK = re.compile(r"[一-鿿]")


def _literal_newlines(text: str) -> str:
    """字面上的 \\n、\\par、\\newline 是换行；\\neq、\\nu 这类真的 LaTeX 命令留着。"""

    def replace(match: re.Match) -> str:
        word, space = match.groups()
        if word in ("par", "newline", "n"):
            return "\n"  # 换行后面的空格也去掉
        if word in LATEX_N_COMMANDS:
            return match.group(0)
        return "\n" + word[1:] + space

    return re.sub(r"\\(par|newline|n[a-zA-Z]*)(?![a-zA-Z])([ \t]*)", replace, text)


def _balanced(core: str) -> bool:
    plain = core.replace("\\{", "").replace("\\}", "")
    depth = 0
    for char in plain:
        depth += {"{": 1, "}": -1}.get(char, 0)
        if depth < 0:
            return False
    return depth == 0


def _edges(run: str) -> tuple[str, str, str]:
    """开头的序号 (1)、首尾的空格和标点、配不上对的括号都不算在算式里。"""
    lead = re.match(r"^(?:[\s,.:;]|\(\d\)\s*)*", run).group(0)
    core = run[len(lead):]
    tail = re.search(r"[\s,.:;!]*$", core).group(0)
    core = core[: len(core) - len(tail)]
    while core and core[-1] in ")]" and core.count(")") + core.count("]") > core.count("(") + core.count("["):
        tail = core[-1] + tail
        core = core[:-1]
    while core and core[0] in "([" and core.count("(") + core.count("[") > core.count(")") + core.count("]"):
        lead += core[0]
        core = core[1:]
    return lead, core, tail


def _wrap_runs(part: str) -> str:
    def wrap(match: re.Match) -> str:
        run = match.group(0)
        lead, core, tail = _edges(run)
        if not _COMMAND.search(core) or not _balanced(core) or core.endswith("\\"):
            return run
        before = part[: match.start()]
        after = part[match.end():]
        left = " " if not lead and before and _CJK.match(before[-1]) else ""
        right = " " if not tail and after and _CJK.match(after[0]) else ""
        return f"{lead}{left}${core}${right}{tail}"

    return _RUN.sub(wrap, part)


def tidy_math_text(text: str | None) -> str:
    """把漏写 $ 的公式补上 $…$、字面的 \\n 换回换行；只动 $…$ 之外的部分。"""
    source = str(text or "")
    # $ 的个数是单数时配不上对，分不清哪里是公式，不动
    if "\\" not in source or source.count("$") % 2:
        return source
    pieces = _WRITTEN.split(source)
    return "".join(piece if index % 2 else _wrap_runs(_literal_newlines(piece)) for index, piece in enumerate(pieces))
