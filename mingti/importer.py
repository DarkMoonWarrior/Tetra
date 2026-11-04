"""导入题目：老师上传 PDF 或图片，模型逐页识别出题目（题干、选项、答案、解析、考点），老师核对后收进题库。

流程：
1. PDF 每页渲染成图片（PyMuPDF，150 dpi）；图片统一转成 PNG，太大的缩小。最多 40 页。
2. 每页交给一个能看图的模型，按固定的 JSON 格式录题（公式写成 LaTeX）；同时最多识别 3 页。
3. 按页码顺序合并：跨页的题（页首 continued、页尾 truncated）接起来；答案页上跨页的解答也接起来，
   再按题号把答案、解答补给题目。原卷上的给分标记（"……6 分"）去掉，组卷时按新试卷重新给分。
4. 考点让模型从当前教材版本的知识点列表里选，再换成"册 › 章 › 节 › 考点"的路径，题库按它分类。
5. 结果先给老师看，勾选的题才收进题库（bank.add_imported）。

配图直接从原文件上截：电子版 PDF 的图是矢量线条，按线条的位置截（vector_figures）；
照片、扫描件按模型给的位置截（raster_figure）。截不到的题标"原题有配图"，收进题库后可以点"配图"重画。
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from mingti.deepseek import DeepSeekClientError

logger = logging.getLogger("mingti.importer")

MAX_PAGES = 40
MAX_SIDE = 2000  # 图片最长边（像素），再大就缩小
TYPES = {"single_choice", "multiple_choice", "fill_blank", "open_response"}
# 能看图的模型（按型号名判断）；DeepSeek 只有 flash 能看图，pro 不能
VISION = re.compile(r"deepseek-(v4-)?flash|glm-5\.3-flash|glm-4v|vl|vision|gpt-|^o\d|claude-|gemini", re.IGNORECASE)

INSTRUCTIONS = """你是数学试卷录入员。看这一页试卷或讲义的图片，把上面的题目逐道录成 JSON。
要求：
- 录题号、题型、题干、选项；这一页上如果有答案、解析，也一并录。不要自己解题，没有答案就留空。
- 题干、解答都照原文一字不落地录完整：条件、数字、小问、公式一个都不能漏，不要改写、概括或省略。
- 公式一律写成 LaTeX，用 $…$ 包住（单独成行的公式用 $$…$$）。
- 题型 question_type：single_choice 单选、multiple_choice 多选、fill_blank 填空、open_response 解答。
- 填空题的空写成 ______；解答题的小问（1）（2）……各占一行。选择题题干末尾的（　　）不要写。
- 题目有配图的，has_figure 写 true，题干照常录文字（"如图"保留）；figure_box 写配图（连同图上的字母、图号）
  在这张图片上的位置 [左, 上, 右, 下]，用图片宽、高的千分数（0 到 1000）。
- 页首的题是接着上一页没写完的，continued 写 true，只录这一页上的部分；页尾的题被截断、下一页还要接着写的，truncated 写 true。
- page_kind 写这一页是什么：questions 试题页、answers 参考答案或解析页、mixed 题目和答案都有、other 封面目录等。
- 参考答案、解析页上的内容一律放进 answers，questions 留空，不要把解答当成新题：
  number 写题号；answer 写最终答案，解答题按（1）（2）……写出每一问的结论（证明题写"证明见解析"）；
  solution_steps 按原文逐行写完整的解答过程，一行都不能省；"……6 分"这类给分标记不用写。
  页尾的解答没写完、下一页接着写的，answer 只写这一页上已经得出结论的小问，没写完的那一问不写。
  页首没有题号、接着上一页没写完的解答，number 写空串，continued 写 true，answer 写这一段里得出的结论（写明是第几问）。
- knowledge：从下面的考点列表里选最贴近的一个，原样写名字；实在对不上写空串。
- difficulty：1 到 5（1 基础、3 中档、5 压轴），按题目本身估计。
- 这一页没有题目（封面、目录、空白页）时，questions 和 answers 都写空数组。
只输出 JSON，格式：
{"page_kind":"questions","questions":[{"number":"1","question_type":"single_choice","stem":"…","options":[{"key":"A","text":"…"}],
"answer":"","solution_steps":[],"knowledge":"","difficulty":3,"has_figure":false,"figure_box":[],"continued":false,"truncated":false}],
"answers":[{"number":"1","answer":"B","solution_steps":["…"],"continued":false}]}"""


class ImportError_(Exception):
    """导入失败，消息直接显示给用户。"""


def is_vision(model_name: str) -> bool:
    return bool(VISION.search(model_name or ""))


def _data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


QUESTION_START = re.compile(r"^\s*(\d{1,2})\s*[．.、]")
# 图号："图 1""（图2）""第 18 题图""（第 18 题图）"
CAPTION = re.compile(r"^[（(]?\s*(?:第\s*(\d{1,2})\s*题\s*)?图\s*[0-9①②③④⑤⑥甲乙丙丁]*\s*[)）]?$")
# 题干里提到配图的说法；模型没标 has_figure、图号也没写题号时，靠它判断这张图是不是这道题的
FIGURE_HINT = re.compile(r"如图|下图|右图|左图|图\s*[0-9①②③④]|题图")
MAX_FIGURE = 250_000  # 一张配图（base64 后）最多这么大，题库里放得下


def _box_union(a: tuple, b: tuple) -> tuple:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _box_near(a: tuple, b: tuple, gap: float) -> bool:
    return a[0] - gap <= b[2] and b[0] - gap <= a[2] and a[1] - gap <= b[3] and b[1] - gap <= a[3]


def _figure_svg(data: bytes, width_pt: float, height_pt: float, mime: str = "image/png") -> str:
    """截下来的配图包成 SVG（题库里的配图都存成 SVG）；显示大小和原卷上一样，最宽 420 像素。"""
    width = min(420.0, width_pt * 96 / 72)
    height = height_pt * width / width_pt
    url = f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" '
        f'viewBox="0 0 {width:.0f} {height:.0f}"><image href="{url}" width="{width:.0f}" height="{height:.0f}"/></svg>'
    )


def _clip_figure(page, clip, zoom: float) -> str:
    """把页面上的一块截成灰度图（试卷的图都是黑白的）；太大就改存 JPEG。"""
    import pymupdf

    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip, colorspace=pymupdf.csGRAY, alpha=False)
    data, mime = pixmap.tobytes("png"), "image/png"
    if len(data) * 4 / 3 > MAX_FIGURE:
        data, mime = pixmap.tobytes("jpeg", jpg_quality=80), "image/jpeg"
    if len(data) * 4 / 3 > MAX_FIGURE:
        return ""
    return _figure_svg(data, clip.width, clip.height, mime)


def vector_figures(page) -> dict[str, dict]:
    """电子版 PDF 上的配图是矢量线条（也可能是插进去的图片）：把挨在一起的线条聚成一张图，
    连同图上的点名（A、B、P…）和图号一起截成高清图片，再对到题目：
    图号写了题号（"第 18 题图"）就按它，否则按阅读顺序归到图前面最近的那道题。
    返回 {题号: {"svg": 配图, "named": 图号里写明了题号}}；页首、前面没有题号的图题号记为 ""（接着上一页的题）。"""
    import pymupdf

    width, height = page.rect.width, page.rect.height
    boxes = []
    for drawing in page.get_drawings():
        r = drawing["rect"]
        box = (max(r.x0, 0), max(r.y0, 0), min(r.x1, width), min(r.y1, height))
        if box[2] >= box[0] and box[3] >= box[1] and (box[2] - box[0] > 0.5 or box[3] - box[1] > 0.5):
            boxes.append(box)
    for info in page.get_image_info():
        r = info["bbox"]
        box = (max(r[0], 0), max(r[1], 0), min(r[2], width), min(r[3], height))
        # 铺满整页的是扫描件的底图，不是配图
        if box[2] > box[0] and box[3] > box[1] and (box[2] - box[0]) * (box[3] - box[1]) < 0.5 * width * height:
            boxes.append(box)
    clusters: list[tuple] = []
    for box in boxes:
        for other in [c for c in clusters if _box_near(c, box, 8)]:
            clusters.remove(other)
            box = _box_union(box, other)
        clusters.append(box)
    merged = True
    while merged:  # 合并后变大的块可能又挨上别的块
        merged = False
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                if _box_near(clusters[i], clusters[j], 8):
                    clusters[i] = _box_union(clusters[i], clusters.pop(j))
                    merged = True
                    break
            if merged:
                break
    # 横线空、分数线、下划线这种又扁又短的不是图；小的图（数轴等）交给模型给的位置去截
    clusters = [c for c in clusters if c[2] - c[0] >= 30 and c[3] - c[1] >= 30]
    if not clusters:
        return {}

    lines = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            text = "".join(span["text"] for span in line["spans"]).strip()
            if text:
                lines.append((tuple(line["bbox"]), text))
    # 题号：一行开头的"17．"，而且靠着这一栏的左边（排除公式中间断开的碎片）
    starts = [(box, match.group(1)) for box, text in lines if (match := QUESTION_START.match(text))]
    # 双栏排版：右半边有两个以上题号、而且左右对齐
    right = sorted(box[0] for box, _ in starts if box[0] > width * 0.45)
    two_columns = len(right) >= 2 and right[1] - right[0] < 15 and any(box[0] < width * 0.45 for box, _ in starts)

    def column(x: float) -> int:
        return 1 if two_columns and x > width * 0.45 else 0

    margins = {}
    for box, _ in lines:
        margins[column(box[0])] = min(margins.get(column(box[0]), box[0]), box[0])
    starts = [(column(box[0]), box[1], number) for box, number in starts if box[0] <= margins[column(box[0])] + 25]
    starts.sort()

    found: dict[str, dict] = {}
    for cluster in clusters:
        area, named = cluster, None
        for box, text in lines:
            caption = CAPTION.match(text)
            if caption:
                # 图号在图的正下方（或紧挨着上方），和图左右有重叠
                overlaps = box[0] < cluster[2] and box[2] > cluster[0]
                if overlaps and cluster[1] - 12 <= box[1] <= cluster[3] + 40:
                    area = _box_union(area, box)
                    named = caption.group(1) or named
                continue
            # 图上的标注：短、不带题干的标点，中心落在图的范围里（留一点边）
            center = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
            if len(text) <= 4 and not re.search(r"[，。；：、？！]", text) and (
                cluster[0] - 10 <= center[0] <= cluster[2] + 10 and cluster[1] - 10 <= center[1] <= cluster[3] + 10
            ):
                area = _box_union(area, box)
        if named:
            number = named
        else:
            here = (column(cluster[0]), cluster[1] + 20)
            before = [number for col, y, number in starts if (col, y) <= here]
            number = before[-1] if before else ""
        slot = found.setdefault(number, {"area": area, "named": False})
        slot["area"] = _box_union(slot["area"], area)
        slot["named"] = slot["named"] or bool(named)
    out = {}
    for number, slot in found.items():
        a = slot["area"]
        clip = pymupdf.Rect(a[0] - 4, a[1] - 4, a[2] + 4, a[3] + 4) & page.rect
        svg = _clip_figure(page, clip, 3)
        if svg:
            out[number] = {"svg": svg, "named": slot["named"]}
    return out


def raster_figure(png: bytes, box) -> str:
    """照片、扫描件上的图没有线条可找：按模型给的位置（图片宽、高的千分数）从整页图片里截。"""
    import pymupdf

    try:
        x0, y0, x1, y1 = [max(0.0, min(1000.0, float(v))) for v in box]
    except (TypeError, ValueError):
        return ""
    # 太小、太大（差不多整页）的位置都不可信
    if x1 - x0 < 30 or y1 - y0 < 30 or (x1 - x0) * (y1 - y0) > 600_000:
        return ""
    document = pymupdf.open(stream=png, filetype="png")
    try:
        page = document[0]
        zoom = pymupdf.Pixmap(png).width / page.rect.width  # 按原图的像素截，不缩小
        w, h = page.rect.width, page.rect.height
        pad = 15  # 模型给的框常常偏紧，四边各放宽 1.5%
        clip = pymupdf.Rect((x0 - pad) * w / 1000, (y0 - pad) * h / 1000, (x1 + pad) * w / 1000, (y1 + pad) * h / 1000) & page.rect
        return _clip_figure(page, clip, zoom)
    finally:
        document.close()


def render_pages(files: list[dict]) -> list[dict]:
    """上传的文件 → 每页一张 PNG：[{file, page, png, figures}]。files：[{name, data(dataURL 或 base64)}]。
    figures 是电子版 PDF 上能直接截下来的配图 {题号: SVG}。"""
    import pymupdf

    pages: list[dict] = []
    for item in files:
        name = str(item.get("name") or "文件")
        raw = str(item.get("data") or "")
        try:
            content = base64.b64decode(raw.split(",", 1)[1] if raw.startswith("data:") else raw)
        except (ValueError, IndexError) as exc:
            raise ImportError_(f"{name} 没读出来（文件内容不完整）") from exc
        kind = "pdf" if name.lower().endswith(".pdf") or content[:4] == b"%PDF" else (name.rsplit(".", 1)[-1].lower() or "png")
        try:
            document = pymupdf.open(stream=content, filetype=kind)
        except Exception as exc:  # noqa: BLE001 - 打不开的文件说清楚是哪个
            raise ImportError_(f"{name} 打不开：只支持 PDF 和常见图片（PNG、JPG）") from exc
        for index, page in enumerate(document):
            if len(pages) >= MAX_PAGES:
                break
            if kind == "pdf":
                scale = 150 / 72
            else:
                side = max(page.rect.width, page.rect.height) or 1
                scale = min(1.0, MAX_SIDE / side)
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            try:
                figures = vector_figures(page) if kind == "pdf" else {}
            except Exception:  # noqa: BLE001 - 截图失败不影响识别题目
                logger.exception("%s 第 %d 页配图没截成", name, index + 1)
                figures = {}
            pages.append({"file": name, "page": index + 1, "png": pixmap.tobytes("png"), "figures": figures})
        document.close()
        if len(pages) >= MAX_PAGES:
            break
    if not pages:
        raise ImportError_("没有可识别的页面")
    return pages


class ImportJob:
    def __init__(self, job_id: str, pages: list[dict], model: str) -> None:
        self.id = job_id
        self.pages = pages
        self.model = model
        self.results: dict[int, dict] = {}  # 第几页（从 0 数）→ 模型录出来的 JSON
        self.errors: dict[int, str] = {}
        self.status = "running"
        self.stop_requested = False
        self.saved = 0
        self.created_at = time.time()
        self.finished_at: float | None = None
        self.lock = threading.Lock()


class ImportManager:
    def __init__(self, models, bank, curriculum_for) -> None:
        """curriculum_for(edition) → 这一版的知识点目录（树）。"""
        self.models = models
        self.bank = bank
        self.curriculum_for = curriculum_for
        self._jobs: dict[str, ImportJob] = {}
        self._lock = threading.Lock()

    # ---- 开始、查看、停止 ----

    def vision_models(self) -> list[dict]:
        return [{**option.public(), "vision": is_vision(option.model)} for option in self.models.options]

    def start(self, files: list[dict], model_id: str | None, edition: str) -> ImportJob:
        if not files:
            raise ImportError_("先选要导入的 PDF 或图片")
        options = self.models.options
        if not options:
            raise ImportError_("还没有可用的模型")
        option = next((o for o in options if o.id == model_id), None) or next(
            (o for o in options if is_vision(o.model)), options[0]
        )
        pages = render_pages(files)
        job = ImportJob(f"im_{uuid.uuid4().hex[:10]}", pages, option.id)
        with self._lock:
            self._jobs[job.id] = job
        threading.Thread(target=self._run, args=(job, edition), name=f"mingti-import-{job.id}", daemon=True).start()
        return job

    def get(self, job_id: str) -> ImportJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def stop(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None:
            return False
        job.stop_requested = True
        return True

    # ---- 识别 ----

    def _kp_index(self, edition: str) -> dict[str, str]:
        """考点名称 → "册 › 章 › 节 › 考点"。"""
        index: dict[str, str] = {}
        for volume in self.curriculum_for(edition):
            for chapter in volume["children"]:
                for section in chapter["children"]:
                    for kp in section["children"]:
                        index.setdefault(kp["name"], " › ".join([volume["name"], chapter["name"], section["name"], kp["name"]]))
        return index

    def _run(self, job: ImportJob, edition: str) -> None:
        kp_index = self._kp_index(edition)
        job.kp_index = kp_index
        instructions = f"{INSTRUCTIONS}\n考点列表：{'、'.join(kp_index)}"
        route = self.models.route(job.model)

        def one(number: int) -> None:
            if job.stop_requested:
                return
            page = job.pages[number]
            try:
                raw = route.client.request(
                    instructions=instructions,
                    input_text=f"这是《{page['file']}》第 {page['page']} 页（共识别 {len(job.pages)} 页中的第 {number + 1} 页）。",
                    action="识别题目",
                    image_data_url=_data_url(page["png"]),
                    max_tokens=8000,
                )
                payload = json.loads(raw["output"][0]["content"][0]["text"])
                payload = payload if isinstance(payload, dict) else {}
                _attach_figures(payload, page)
                with job.lock:
                    job.results[number] = payload
            except (DeepSeekClientError, ValueError, KeyError, IndexError, TypeError) as exc:
                with job.lock:
                    job.errors[number] = str(exc)[:300]

        try:
            with ThreadPoolExecutor(max_workers=3, thread_name_prefix=f"import-{job.id}") as pool:
                list(pool.map(one, range(len(job.pages))))
        except Exception:  # noqa: BLE001 - 识别出错要反馈到界面
            logger.exception("导入 %s 出错", job.id)
        finally:
            with job.lock:
                job.status = "cancelled" if job.stop_requested else "done"
                job.finished_at = time.time()

    # ---- 合并成题目列表 ----

    def questions(self, job: ImportJob) -> list[dict]:
        with job.lock:
            results = dict(job.results)
        kp_index = getattr(job, "kp_index", None) or {}
        merged: list[dict] = []
        answers: list[dict] = []
        for number in sorted(results):
            page = job.pages[number]
            payload = results[number]
            answer_page = payload.get("page_kind") == "answers"
            on_page: list[dict] = []  # 这一页上的答案、解答，按原文的顺序
            first = True
            for raw in payload.get("questions") or []:
                if not isinstance(raw, dict):
                    continue
                # 答案页上被当成"题目"录进来的（多半是解答过程）：按题号当答案用，不算新题
                if answer_page:
                    on_page.append({
                        "number": raw.get("number"),
                        "answer": raw.get("answer"),
                        "solution_steps": raw.get("solution_steps") or str(raw.get("stem") or "").split("\n"),
                        "continued": raw.get("continued"),
                    })
                    continue
                item = _clean(raw, page)
                last = merged[-1] if merged else None
                # 页首接着上一页的题：模型标了 continued，或者上一页标了截断、这道又没有新题号；
                # 紧挨着的两条题号一样（模型有时把题干和小问拆成两条），也是同一道题
                joins = first and (raw.get("continued") or (last and last["truncated"] and item["number"] in ("", last["number"])))
                if last and (joins or (item["number"] and item["number"] == last["number"])):
                    _append(last, item)
                    last["truncated"] = bool(raw.get("truncated"))
                elif item["stem"]:
                    item["truncated"] = bool(raw.get("truncated"))
                    merged.append(item)
                first = False
            on_page += [a for a in payload.get("answers") or [] if isinstance(a, dict)]
            for k, raw in enumerate(on_page):
                entry = {
                    "number": _number(raw.get("number")),
                    "answer": _tidy_answer(raw.get("answer")),
                    "solution_steps": _steps(raw.get("solution_steps")),
                }
                last = answers[-1] if answers else None
                # 跨页的解答：页首没有题号的一段（或者模型标了 continued）接到上一页最后一道的解答后面
                if last and (not entry["number"] or entry["number"] == last["number"] or (k == 0 and raw.get("continued"))):
                    last["answer"] = _continue_answer(last["answer"], last["solution_steps"], entry["answer"], entry["solution_steps"])
                    last["solution_steps"] += entry["solution_steps"]
                elif entry["number"] and (entry["answer"] or entry["solution_steps"]):
                    answers.append(entry)
        # 按题号把答案、解答补给题目：给第一道同题号、还缺这一项的题（一次传了两份卷子时题号会重复）
        for entry in answers:
            target = next((
                q for q in merged if q["number"] == entry["number"]
                and ((entry["answer"] and not q["answer"]) or (entry["solution_steps"] and not q["solution_steps"]))
            ), None)
            if target:
                target["answer"] = target["answer"] or entry["answer"]
                target["solution_steps"] = target["solution_steps"] or entry["solution_steps"]
        for index, item in enumerate(merged):
            item["index"] = index
            item["solution_steps"] = _strip_lead(item["solution_steps"])[:60]
            item["answer"] = _tidy_answer(item["answer"])
            # 解答题的答案页一般只有解答过程：有解答就把答案写成"见解析"
            if not item["answer"] and item["solution_steps"] and item["question_type"] == "open_response":
                item["answer"] = "见解析"
            item["knowledge"] = kp_index.get(item.pop("kp_name", ""), "")
            item["warnings"] = _warnings(item)
            item.pop("truncated", None)
        return merged

    def snapshot(self, job: ImportJob) -> dict:
        with job.lock:
            done = len(job.results) + len(job.errors)
            errors = [f"第 {n + 1} 页：{text}" for n, text in sorted(job.errors.items())]
            status, saved = job.status, job.saved
        return {
            "id": job.id,
            "status": status,
            "model": job.model,
            "pages": len(job.pages),
            "pages_done": done,
            "errors": errors,
            "questions": self.questions(job),
            "saved": saved,
        }

    def save(self, job: ImportJob, picked: list[dict]) -> int:
        """把老师勾选（可能改过）的题收进题库。"""
        count = 0
        for item in picked:
            if self.bank.add_imported(item, source_id=f"import:{job.id}:{item.get('index', count)}"):
                count += 1
        with job.lock:
            job.saved += count
        return count


# 原卷上的给分标记："……6 分""…………8分""\cdots\cdots 11 分"
SCORE_MARK = re.compile(r"\s*(?:[.…·．⋯]|\$?\\[cl]?dots\$?)+\s*\d+(?:\.\d+)?\s*分(?!钟)")


def _number(value) -> str:
    """题号只留数字（"第17题""17．"→"17"）。"""
    text = str(value or "").strip()
    match = re.search(r"\d+", text)
    return match.group(0) if match else text


SECTION_TITLE = re.compile(r"^[一二三四五六七八九十]+\s*[、．.]\s*(?:单项|多项|单|多)?(?:选择|填空|解答)题")


def _steps(value) -> list[str]:
    if isinstance(value, str):
        value = value.split("\n")
    steps = [SCORE_MARK.sub("", str(step)).strip() for step in value or []]
    return [step for step in steps if step and not SECTION_TITLE.match(step)]


def _strip_lead(steps: list[str]) -> list[str]:
    """解答开头的"17．解：""【详解】"去掉，试卷、题卡上会自己加。"""
    if steps:
        first = re.sub(r"^\s*(?:\d{1,2}\s*[．.、]\s*)?(?:【\s*(?:详解|解析|解答|答案)\s*】|(?:解析|解答|解)\s*[：:])\s*", "", steps[0]).strip()
        steps = ([first] if first else []) + steps[1:]
    return steps


# 答案里的小问标记"（2）"；解答里一行开头的"（2）"（前面可能带着"17．解："）
PART = re.compile(r"（\s*(\d)\s*）")
PART_LINE = re.compile(r"^\s*(?:\d{1,2}\s*[．.]\s*)?(?:解\s*[：:]\s*)?（\s*(\d)\s*）")


def _tidy_answer(value) -> str:
    """小问标记统一写成"（2）"：模型有时写成"第（2）问：""（第 2 问）""（2）问：""（2）答："。"""
    answer = str(value or "").strip()
    answer = re.sub(r"[（(]?\s*第\s*[（(]?\s*(\d)\s*[)）]?\s*问\s*[)）]?\s*[：:]?\s*", r"（\1）", answer)
    # 开头或分号后面的半角"(2)"也换成全角（公式里的 f(2) 不动）
    answer = re.sub(r"(^|[；;。]\s*)\((\d)\)", r"\1（\2）", answer)
    answer = re.sub(r"（\s*(\d)\s*）\s*(?:[问答]\s*)?[)）：:]\s*", r"（\1）", answer)
    return answer


def _answer_parts(answer: str) -> list[tuple[int | None, str]]:
    """"（1）证明见解析；（2）最小值为 …" → [(1, "（1）证明见解析"), (2, "（2）最小值为 …")]；没有小问标记的整段记为 None。"""
    marks = list(PART.finditer(answer))
    if not marks:
        return [(None, answer.strip())] if answer.strip() else []
    parts = []
    head = answer[: marks[0].start()].strip(" ；;，,。．")
    if head:
        parts.append((None, head))
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(answer)
        parts.append((int(mark.group(1)), answer[mark.start():end].strip(" ；;，,")))
    return parts


def _continue_answer(before: str, before_steps: list[str], after: str, after_steps: list[str]) -> str:
    """一道解答跨了页，把两页给的结论合起来。
    没有小问的题：下一页给了结论就以它为准，没给就用前一页的。
    有小问的题：前一页上没写完的那一问（解答里最后开始的那一问，下一页又不是从新的一问开始），
    模型给的结论靠不住（它看不到后面，常把最后一行当成结论），以下一页给的为准；
    解答里有、却没有结论的小问写"见解析"。"""
    before_parts, after_parts = _answer_parts(before), _answer_parts(after)
    if not any(n is not None for n, _ in before_parts + after_parts):
        return after.strip() or before.strip()
    current = None  # 前一页上正写到一半的那一问
    for step in before_steps:
        if match := PART_LINE.match(step):
            current = int(match.group(1))
    if after_steps and PART_LINE.match(after_steps[0]):
        current = None  # 下一页从新的一问开始：前一页上的都写完了
    kept = {n: part for n, part in before_parts if n is not None and (current is None or n < current)}
    for n, part in after_parts:
        if n is None:
            if current is None:
                continue
            n, part = current, f"（{current}）{part}"
        if n not in kept or (current is not None and n >= current):
            kept[n] = part
    for step in before_steps + after_steps:
        if (match := PART_LINE.match(step)) and int(match.group(1)) not in kept:
            n = int(match.group(1))
            kept[n] = f"（{n}）见解析"
    if all(part == f"（{n}）见解析" for n, part in kept.items()):
        return "见解析"
    return "；".join(kept[n] for n in sorted(kept))


def _attach_figures(payload: dict, page: dict) -> None:
    """把截下来的配图放到题目上（raw["figure_svg"]）。电子版 PDF 用按线条截的图；
    照片、扫描件，或者线条里没找到的，按模型给的位置截。答案页上的图不要。"""
    if payload.get("page_kind") == "answers":
        return
    figures = page.get("figures") or {}
    for raw in payload.get("questions") or []:
        if not isinstance(raw, dict):
            continue
        key = _number(raw.get("number"))
        found = figures.get(key) or (figures.get("") if raw.get("continued") else None)
        if found and (found["named"] or raw.get("has_figure") or FIGURE_HINT.search(str(raw.get("stem") or ""))):
            raw["figure_svg"] = found["svg"]
            raw["has_figure"] = True
        elif raw.get("has_figure") and raw.get("figure_box"):
            try:
                svg = raster_figure(page["png"], raw.get("figure_box"))
            except Exception:  # noqa: BLE001 - 截不到就提示老师重画
                logger.exception("按位置截配图失败")
                svg = ""
            if svg:
                raw["figure_svg"] = svg
                raw["figure_cropped"] = True


def _clean(raw: dict, page: dict) -> dict:
    kind = raw.get("question_type") if raw.get("question_type") in TYPES else "open_response"
    options = [
        {"key": str(o.get("key") or "").strip()[:4] or chr(65 + i), "text": str(o.get("text") or "").strip()}
        for i, o in enumerate(raw.get("options") or []) if isinstance(o, dict)
    ][:8]
    if kind in ("single_choice", "multiple_choice") and not options:
        kind = "fill_blank" if "___" in str(raw.get("stem") or "") else "open_response"
    try:
        difficulty = min(5, max(1, int(raw.get("difficulty") or 3)))
    except (TypeError, ValueError):
        difficulty = 3
    # 原卷上的分值（"（17 分）"）不是题目内容，组卷时会按新试卷重新标
    stem = re.sub(r"^\s*[（(]\s*\d+(\.\d+)?\s*分\s*[)）]\s*", "", str(raw.get("stem") or "")).strip()
    return {
        "number": _number(raw.get("number")),
        "question_type": kind,
        "stem": stem,
        "options": options,
        "answer": _tidy_answer(raw.get("answer")),
        "solution_steps": _steps(raw.get("solution_steps")),
        "kp_name": str(raw.get("knowledge") or "").strip(),
        "difficulty": difficulty,
        "has_figure": bool(raw.get("has_figure")),
        "figure_svg": str(raw.get("figure_svg") or ""),
        "figure_cropped": bool(raw.get("figure_cropped")),
        "source": f"{page['file']} 第 {page['page']} 页",
    }


def _append(target: dict, extra: dict) -> None:
    if extra["stem"]:
        target["stem"] = f"{target['stem']}\n{extra['stem']}".strip()
    target["options"] += extra["options"]
    target["answer"] = target["answer"] or extra["answer"]
    target["solution_steps"] += extra["solution_steps"]
    target["has_figure"] = target["has_figure"] or extra["has_figure"]
    if not target["figure_svg"] and extra["figure_svg"]:
        target["figure_svg"], target["figure_cropped"] = extra["figure_svg"], extra["figure_cropped"]


def _warnings(item: dict) -> list[str]:
    notes = []
    if item["has_figure"] and not item["figure_svg"]:
        notes.append("原题有配图，没截下来；收进题库后可以点“配图”重画")
    elif item["figure_cropped"]:
        notes.append("配图是按识别出的位置截的，看看截得全不全")
    if not item["answer"] and not item["solution_steps"]:
        notes.append("没识别到答案")
    if not item["knowledge"]:
        notes.append("没对上考点")
    return notes
