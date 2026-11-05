"""生成英文界面的对照表 web/js/i18n-en.js。

1. 提取：从 web/js/*.js、web/index.html 和后端几个会把文字显示到界面上的文件里，找出带中文的字符串。
   JS 的模板字符串 `出好 ${done} 道` 记成"出好 {0} 道"（${} 里的字符串另外提取）；Python 的 f-string 同样处理。
2. 翻译：已经翻过的（tools/i18n_en.json）直接用；新的请接入的模型批量翻译（带术语表，占位符 {0}{1} 原样保留）。
   想改某一条的译法：写进 tools/i18n_overrides.json（{"中文": "English"}），它优先。
3. 生成：没有占位符的放进 exact（整句查表），有占位符的放进 patterns（按模板匹配，变量部分再翻译）。

用法（在项目目录下）：
    .venv\\Scripts\\python tools\\build_i18n.py            # 提取、翻译新的、生成
    .venv\\Scripts\\python tools\\build_i18n.py --dry-run  # 只看要翻译的有多少条
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CJK = re.compile(r"[㐀-鿿]")
CACHE = ROOT / "tools" / "i18n_en.json"
OVERRIDES = ROOT / "tools" / "i18n_overrides.json"
OUTPUT = ROOT / "web" / "js" / "i18n-en.js"
# 后端里会显示到界面上的文字（出题过程的说明、报错）：只取不太长的字符串，提示词这类长文本不要
BACKEND = ["jobs.py", "assemble.py", "importer.py", "server.py", "attachments.py", "assistant.py", "context.py", "bank.py", "deepseek.py"]
BACKEND_MAX = 90


# ---------------- 提取 ----------------

def _js_strings(source: str) -> list[str]:
    """JS 源码里的字符串（单引号、双引号、模板字符串）；模板里的 ${…} 记成 {0}、{1}…，里面的字符串另外提取。"""
    out: list[str] = []
    i, n = 0, len(source)
    last = ""  # 上一个有意义的字符，用来判断 / 是除号还是正则

    def read_quoted(start: int, quote: str) -> tuple[str, int]:
        j, text = start + 1, []
        while j < n and source[j] != quote:
            if source[j] == "\\" and j + 1 < n:
                nxt = source[j + 1]
                text.append({"n": "\n", "t": "\t"}.get(nxt, nxt))
                j += 2
                continue
            if source[j] == "\n":
                break
            text.append(source[j])
            j += 1
        return "".join(text), j + 1

    def read_template(start: int) -> tuple[str, int]:
        j, parts, slot = start + 1, [], 0
        while j < n and source[j] != "`":
            if source[j] == "\\" and j + 1 < n:
                parts.append(source[j + 1])
                j += 2
                continue
            if source.startswith("${", j):
                depth, k = 1, j + 2
                inner_start = k
                while k < n and depth:
                    ch = source[k]
                    if ch in "'\"":
                        _, k = read_quoted(k, ch)
                        continue
                    if ch == "`":
                        _, k = read_template(k)
                        continue
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                    k += 1
                out.extend(_js_strings(source[inner_start:k - 1]))
                parts.append(f"{{{slot}}}")
                slot += 1
                j = k
                continue
            parts.append(source[j])
            j += 1
        return "".join(parts), j + 1

    while i < n:
        ch = source[i]
        if source.startswith("//", i):
            i = source.find("\n", i)
            i = n if i < 0 else i
            continue
        if source.startswith("/*", i):
            i = source.find("*/", i + 2)
            i = n if i < 0 else i + 2
            continue
        if ch in "'\"":
            text, i = read_quoted(i, ch)
            out.append(text)
            last = "a"
            continue
        if ch == "`":
            text, i = read_template(i)
            out.append(text)
            last = "a"
            continue
        if ch == "/" and (last in "(,=:[!&|?{};+-*%<>~^" or last == ""):
            # 正则字面量：跳到没转义的下一个 /（字符类 [] 里的 / 不算）
            j, in_class = i + 1, False
            while j < n and source[j] != "\n":
                if source[j] == "\\":
                    j += 2
                    continue
                if source[j] == "[":
                    in_class = True
                elif source[j] == "]":
                    in_class = False
                elif source[j] == "/" and not in_class:
                    break
                j += 1
            i = j + 1
            last = "a"
            continue
        if not ch.isspace():
            last = ch if not (ch.isalnum() or ch in "_$") else "a"
            # return / typeof 这类关键字后面的 / 是正则
            if ch.isalpha():
                m = re.match(r"[A-Za-z_$][\w$]*", source[i:])
                if m:
                    word = m.group(0)
                    last = "(" if word in ("return", "typeof", "case", "in", "of", "new", "delete", "void", "throw") else "a"
                    i += len(word)
                    continue
        i += 1
    return out


def _html_strings(source: str) -> list[str]:
    out = re.findall(r">([^<>]*[㐀-鿿][^<>]*)<", source)
    out += re.findall(r'(?:title|placeholder|aria-label)="([^"]*[㐀-鿿][^"]*)"', source)
    return [" ".join(text.split()) for text in out]


def _py_strings(source: str) -> list[str]:
    out = []
    for prefix, quote, body in re.findall(r'(\b[fFrRbB]{0,2})("|\')((?:\\.|(?!\2).)*)\2', source):
        if "f" in prefix.lower():
            slot = iter(range(100))
            body = re.sub(r"\{[^{}]*\}", lambda _m: f"{{{next(slot)}}}", body)
        elif "{" in body:
            continue
        if len(body) <= BACKEND_MAX:
            out.append(body)
    return out


def extract() -> list[str]:
    found: dict[str, None] = {}
    for path in sorted((ROOT / "web" / "js").glob("*.js")):
        if path.name.startswith("i18n"):
            continue
        for text in _js_strings(path.read_text(encoding="utf-8")):
            found[text] = None
    for text in _html_strings((ROOT / "web" / "index.html").read_text(encoding="utf-8")):
        found[text] = None
    for name in BACKEND:
        for text in _py_strings((ROOT / "mingti" / name).read_text(encoding="utf-8")):
            found[text] = None
    keep = []
    for text in found:
        core = text.strip()
        # 只要显示给人看的中文：去掉正则、CSS 选择器、空白
        if not core or not CJK.search(core) or core.startswith(("^", "(?")) or "\\u" in core:
            continue
        keep.append(core)
    return keep


# ---------------- 翻译 ----------------

GLOSSARY = """术语（照这个译，保持一致）：
出题 Generate；组卷 Assemble a paper；题库 Question bank；试卷 Papers / paper；对话 Chat；新对话 New chat；个人设置 Settings；
考点 Topic；知识点 Knowledge point；题型 Type；难度 Difficulty；题量 Count；用途 Purpose；年级 Grade；思考强度 Reasoning；
低 Low；中 Medium；高 High；极高 Max；单选题 Single choice；多选题 Multiple choice；填空题 Fill-in；解答题 Free response；
基础 Basic；较易 Easy；中档 Medium；较难 Hard；压轴 Challenge；课堂练习 Classwork；课后作业 Homework；单元测验 Unit test；高考复习 Gaokao review；
高一 Grade 10；高二 Grade 11；高三 Grade 12；必修 Required；选择性必修 Elective；人教 A 版 PEP Edition A；人教 B 版 PEP Edition B；
命题员 Composer；审题员 Reviewer；解题员 Solver；复核员 Auditor；验算员 Verifier；整理员 Editor；配图员 Illustrator；
题干 Stem；选项 Options；答案 Answer；解析 Solution；分析 Analysis；详解 Details；完整推导 Full derivation；配图 Figure；
上下文 Context；压缩 Compress；附件 Attachment；模型服务 Model services；导入 Import；导出 Export；置顶 Pin；重命名 Rename。"""

INSTRUCTIONS = f"""把下面这些界面文字从中文翻译成英文，用在一个给高中数学老师出题的应用（Tetra）里。
要求：
- 简洁自然，像常见软件的界面文字；按钮、标签用短语，句子用完整的句子。
- {{0}}、{{1}} 这类占位符原样保留，可以按英文语序调整位置，但每个占位符都要出现、不能改编号。
- 型号名（deepseek-flash 等）、文件格式、数学符号、LaTeX、标点以外的英文原样保留。
- 中文标点换成英文标点。
{GLOSSARY}
输入 items 是 [{{"id": 编号, "zh": 中文}}]，只输出 JSON：{{"items": [{{"id": 编号, "en": "English"}}]}}"""

SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "integer"}, "en": {"type": "string"}}}}},
    "required": ["items"],
}


def translate(texts: list[str], batch: int = 100) -> dict[str, str]:
    from mingti.config import load_settings
    from mingti.models import ModelRegistry

    models = ModelRegistry(load_settings())
    route = models.route(None)
    print(f"用 {route.model} 翻译 {len(texts)} 条（每批 {batch} 条）")

    def one(chunk: list[str]) -> dict[str, str]:
        items = [{"id": i, "zh": text} for i, text in enumerate(chunk)]
        raw = route.client.request(
            instructions=INSTRUCTIONS,
            input_text=json.dumps({"items": items}, ensure_ascii=False),
            action="翻译界面文字",
            output_schema=SCHEMA,
            max_tokens=8000,
            recovery_instruction="只修复 JSON 结构。",
        )
        result = json.loads(raw["output"][0]["content"][0]["text"])
        done = {}
        for entry in result.get("items") or []:
            index = entry.get("id")
            english = str(entry.get("en") or "").strip()
            if isinstance(index, int) and 0 <= index < len(chunk) and english:
                # 占位符要一个不少
                if sorted(re.findall(r"\{\d+\}", chunk[index])) == sorted(re.findall(r"\{\d+\}", english)):
                    done[chunk[index]] = english
        print(f"  这一批翻好 {len(done)} / {len(chunk)} 条")
        return done

    chunks = [texts[i:i + batch] for i in range(0, len(texts), batch)]
    out: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        for done in pool.map(one, chunks):
            out.update(done)
    return out


# ---------------- 生成 ----------------

def build(table: dict[str, str]) -> None:
    exact = {zh: en for zh, en in table.items() if not re.search(r"\{\d+\}", zh)}
    patterns = [[zh, en] for zh, en in table.items() if re.search(r"\{\d+\}", zh)]
    body = json.dumps({"exact": exact, "patterns": patterns}, ensure_ascii=False, indent=0)
    OUTPUT.write_text(
        "// 英文对照表：由 tools/build_i18n.py 生成（从界面代码里提取中文字符串，请模型翻译），不要手改；\n"
        "// 要改某一条的译法，改 tools/i18n_overrides.json 再重新生成。\n"
        f"export const EN = {body};\n",
        encoding="utf-8",
    )
    print(f"写好 {OUTPUT.relative_to(ROOT)}：整句 {len(exact)} 条，模板 {len(patterns)} 条")


def main() -> None:
    parser = argparse.ArgumentParser(description="生成英文界面的对照表")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不翻译、不生成")
    args = parser.parse_args()
    texts = extract()
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    overrides = json.loads(OVERRIDES.read_text(encoding="utf-8")) if OVERRIDES.exists() else {}
    todo = [text for text in texts if text not in cache and text not in overrides]
    print(f"界面上的中文：{len(texts)} 条；已经有译文的 {len(texts) - len(todo)} 条；要翻译 {len(todo)} 条")
    if args.dry_run:
        for text in todo[:40]:
            print("  ", text)
        return
    if todo:
        cache.update(translate(todo))
        CACHE.write_text(json.dumps(dict(sorted(cache.items())), ensure_ascii=False, indent=1), encoding="utf-8")
    # 代码里还在用的句子（按缓存）+ 手写的（覆盖缓存；也可以是代码里拼出来、提取不到的整句模板）
    table = {text: cache[text] for text in texts if text in cache}
    table.update(overrides)
    build(table)


if __name__ == "__main__":
    main()
