"""自动组卷：老师说一句要求（比如"从题库里挑题，组一套高一期末考试的试卷"），助手按下面几步组好一份试卷：

1. 定组卷方案（命题双向细目表）：请模型按要求、今天的日期（第几学期）和教材目录，定考查范围、题型结构，
   以及每道题考哪个知识点、多难、几分。没指定结构时照新高考数学卷：单选 8、多选 3、填空 3、解答 5，满分 150。
2. 按方案从题库里挑题：题型相同、难度最多差一档，知识点先找同一个考点，再放宽到同一节、同一章；
   同一个考点在一份卷子里不重复。题干说"如图"却没有图、没有答案的题不用。
3. 题库里没有合适的，就现场出题：走完整的出题流程（命题、审题、解题、复核），出好的题照常收进题库。
   一道没出成，降一档难度再出一次；还不成，就放宽条件从题库里找一道同题型的顶上。
4. 组成一份试卷（考试、测验用标准试卷版式），存进"试卷"，在对话里可以直接导出 Word、PDF。

组卷在后台线程里跑，界面轮询 snapshot() 看进度；组好（或停下、失败）后整个过程存成对话里的一条记录（store.messages，kind = "paper"）。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from mingti import context
from mingti.assistant import CHAT_THINKING
from mingti.bank import Paper, PaperItem
from mingti.config import CURRICULA
from mingti.question_authoring import PROMPT_REVISION, DeepSeekQuestionAuthoringProvider, QuestionDesignBrief
from mingti.tracking import AbortScope, JobRecorder

logger = logging.getLogger("mingti.assemble")

TYPE_ORDER = ("single_choice", "multiple_choice", "fill_blank", "open_response")
TYPES = {"single_choice": "单选题", "multiple_choice": "多选题", "fill_blank": "填空题", "open_response": "解答题"}
SECTION = {"single_choice": "单项选择题", "multiple_choice": "多项选择题", "fill_blank": "填空题", "open_response": "解答题"}
LEVELS = {1: "基础", 2: "较易", 3: "中档", 4: "较难", 5: "压轴"}
DEFAULT_SCORE = {"single_choice": 5, "multiple_choice": 6, "fill_blank": 5, "open_response": 12}
GRADES = ("高一", "高二", "高三")
MAX_SLOTS = 30
PLAN_STAGE = "paper-plan"
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{4,40}")
# 题干里说到了图，题库里这道题却没有配图：组进卷子学生没法做，不用
FIGURE_WORDS = re.compile(r"如图|下图|右图|左图|图中|图示|见图")

PLAN_INSTRUCTIONS = """你是高中数学教研组的命题老师。老师用一句话提了组卷要求（request），请你定一份组卷方案（命题双向细目表）。之后会按方案先从题库里挑题，题库里没有的再现场出题。summary 和 history 是这个对话的前文，老师在前面说过的范围、题量、侧重点也要照顾到。
1. 考查范围：按年级、学期和考试类型定。today 是今天的日期，semester 是现在所在的学期（9 月到次年 1 月是第一学期，2 月到 7 月是第二学期）。比如人教 A 版高一第一学期期末考必修第一册，第二学期期末考必修第二册；期中考试只考本学期前半段的内容；高三复习和模拟考试可以考全部内容。老师说了范围的照老师说的。知识点只能从 outline（教材目录）里选，topic 原样照抄目录里的知识点名称。
2. 题型结构：老师没指定时，期中、期末、月考和模拟考试用新高考数学卷的结构：单选题 8 道（每道 5 分）、多选题 3 道（每道 6 分）、填空题 3 道（每道 5 分）、解答题 5 道（依次 13、15、15、17、17 分），满分 150 分，考试时间 120 分钟。单元测验、周测可以少一些（比如单选 6、多选 2、填空 2、解答 3，100 分左右）。老师指定了题量、题型、分值或时间的，照老师说的定。
3. 难度：每道题 1～5（1 基础、2 较易、3 中档、4 较难、5 压轴）。期中、期末以基础和中档为主（容易、中档、难大约 6:3:1）；单元测验更容易一些；高考模拟按高考的难度。同一种题型内由易到难排：单选、多选、填空前几道 1～2，后几道 3～4；解答题前两道 2～3，最后两道 4～5。
4. 知识点：尽量覆盖范围内的每一章，重点章（比如函数、三角函数、导数）多出几道；同一个知识点在整份卷子里只出现一次。解答题选适合出成大题的知识点。
5. 题库（bank）里已经有的题：在满足上面要求的前提下，优先选题库里已经有对应题型、难度的知识点，这样能直接用现成的题，少现场出题。
6. title：试卷名，比如“2026—2027学年第一学期高一期末考试”；exam_name：印在试卷最上面的考试名称（不要写“数学”两个字），一般和 title 一样；tag：右上角的小标记，比如“高一期末”，可以留空；style：考试、测验用 standard（标准试卷版式），练习、作业用 practice；purpose：用途，比如“期末考试”；grade：高一、高二或高三；minutes：考试时间（分钟）；scope：一句话写考查范围；summary：一两句话说明命题思路（范围、结构、难度安排）。
slots 按卷面顺序列出每一道题：type（single_choice、multiple_choice、fill_blank、open_response）、difficulty、topic、score。最多 30 道。
只输出 JSON。"""

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "exam_name": {"type": "string"},
        "tag": {"type": "string"},
        "style": {"type": "string", "enum": ["standard", "practice"]},
        "purpose": {"type": "string"},
        "grade": {"type": "string", "enum": list(GRADES)},
        "minutes": {"type": "integer"},
        "scope": {"type": "string"},
        "summary": {"type": "string"},
        "slots": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": list(TYPE_ORDER)},
                    "difficulty": {"type": "integer", "minimum": 1, "maximum": 5},
                    "topic": {"type": "string"},
                    "score": {"type": "number"},
                },
                "required": ["type", "difficulty", "topic", "score"],
            },
        },
    },
    "required": ["title", "exam_name", "style", "purpose", "grade", "minutes", "scope", "summary", "slots"],
}


class AssemblyError(RuntimeError):
    pass


def _now_iso(moment: float | None = None) -> str:
    return datetime.fromtimestamp(moment or time.time(), timezone.utc).isoformat()


def _key(text: str) -> str:
    return re.sub(r"[\s，。、：；（）()·《》“”\"'-]", "", str(text or "")).lower()


def _similar(a: str, b: str) -> float:
    """两个名称有多像：按相邻两个字的组合算重合度（0～1）。"""
    a, b = _key(a), _key(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    pairs = lambda s: Counter(s[i:i + 2] for i in range(len(s) - 1)) if len(s) > 1 else Counter([s])  # noqa: E731
    pa, pb = pairs(a), pairs(b)
    return 2 * sum((pa & pb).values()) / max(1, sum(pa.values()) + sum(pb.values()))


def usable(item: dict) -> bool:
    """题库里这道题能不能直接组进卷子：有题干、有答案；选择题有选项；说了"如图"就得有图；解答题得有解析；
    标了"待核对"的题要老师看过才用。"""
    stem = str(item.get("stem") or "")
    if not stem.strip() or not str(item.get("answer") or "").strip() or item.get("solution_issue"):
        return False
    if item.get("question_type") in ("single_choice", "multiple_choice") and len(item.get("options") or []) < 2:
        return False
    if FIGURE_WORDS.search(stem) and not item.get("figure_svg"):
        return False
    if item.get("question_type") == "open_response" and not (item.get("solution_steps") or item.get("full_solution_steps")):
        return False
    return True


class Outline:
    """教材目录（按个人设置里的教材版本）：知识点、节、章 → 完整路径；也能写成给模型看的目录文字。"""

    def __init__(self, tree: list[dict]) -> None:
        self.tree = tree
        self.nodes: list[dict] = []  # {level, name, path, node_id}
        self.kp_path: dict[str, list[str]] = {}
        for volume in tree:
            self.nodes.append({"level": "volume", "name": volume["name"], "path": [volume["name"]], "node_id": None,
                               "grade": volume.get("grade", "")})
            for chapter in volume["children"]:
                path = [volume["name"], chapter["name"]]
                self.nodes.append({"level": "chapter", "name": chapter["name"], "path": path, "node_id": None})
                for section in chapter["children"]:
                    spath = [*path, section["name"]]
                    self.nodes.append({"level": "section", "name": section["name"], "path": spath, "node_id": None})
                    for kp in section["children"]:
                        kpath = [*spath, kp["name"]]
                        self.nodes.append({"level": "kp", "name": kp["name"], "path": kpath, "node_id": kp["id"]})
                        self.kp_path.setdefault(kp["name"], kpath)

    def locate(self, topic: str) -> dict | None:
        """方案里写的知识点 → 目录里的位置：先找名字完全一样的（知识点 > 节 > 章 > 册），再找最像的。"""
        topic = str(topic or "").strip()
        if not topic:
            return None
        order = {"kp": 0, "section": 1, "chapter": 2, "volume": 3}
        exact = [node for node in self.nodes if _key(node["name"]) == _key(topic)]
        if exact:
            return min(exact, key=lambda node: order[node["level"]])
        scored = [(_similar(node["name"], topic), -order[node["level"]], node) for node in self.nodes if node["level"] != "volume"]
        score, _, node = max(scored, key=lambda entry: entry[:2], default=(0, 0, None))
        return node if score >= 0.55 else None

    def path_of(self, item: dict) -> list[str]:
        """题库里一道题的知识点路径：按考点名称在这一版教材的目录里找；找不到就用题里存的路径。"""
        parts = [part.strip() for part in str(item.get("knowledge") or "").split(" › ") if part.strip()]
        if parts and parts[-1] in self.kp_path:
            return self.kp_path[parts[-1]]
        return parts

    def text(self) -> str:
        lines = []
        for volume in self.tree:
            lines.append(f"{volume['name']}（{volume.get('grade', '')}）")
            for chapter in volume["children"]:
                sections = "；".join(
                    f"{section['name']}（{'、'.join(kp['name'] for kp in section['children'])}）"
                    for section in chapter["children"]
                )
                lines.append(f"- {chapter['name']}：{sections}")
        return "\n".join(lines)


def semester(today: datetime) -> str:
    if today.month >= 9 or today.month == 1:
        return "第一学期（上学期）"
    if 2 <= today.month <= 7:
        return "第二学期（下学期）"
    return "暑假（下学期刚结束，新学年还没开始）"


class Assembly:
    def __init__(self, assembly_id: str, text: str, session_id: str, config: dict, meta: dict) -> None:
        self.id = assembly_id
        self.text = text
        self.session_id = session_id
        self.config = config
        self.meta = meta
        self.status = "running"
        self.stage = "planning"
        self.plan: dict | None = None
        self.slots: list[dict] = []
        self.paper: dict | None = None
        self.error: str | None = None
        self.job_ids: list[str] = []
        self.used_ids: set[str] = set()  # 按方案从题库挑中的题
        self.plan_usage: list[dict] = []
        self.stop_requested = False
        self.abort_scope = AbortScope()  # 点停止时，定方案那次调用也当场掐断
        self.created_at = time.time()
        self.finished_at: float | None = None
        self.message_id = f"m_{uuid.uuid4().hex[:12]}"
        self.lock = threading.Lock()


class AssemblyManager:
    def __init__(self, jobs, bank, store, models, outline_for, profile_for) -> None:
        """jobs：JobManager（现场出题）；outline_for(edition) → 教材目录树；profile_for() → 个人设置。"""
        self.jobs = jobs
        self.bank = bank
        self.store = store
        self.models = models
        self.outline_for = outline_for
        self.profile_for = profile_for
        self._assemblies: dict[str, Assembly] = {}
        self._lock = threading.Lock()

    # ---------- 对外 ----------

    def start(self, text: str, session_id: str, config: dict, meta: dict) -> Assembly:
        session = session_id if SESSION_ID.fullmatch(session_id or "") else f"c_{uuid.uuid4().hex[:10]}"
        assembly = Assembly(f"as_{uuid.uuid4().hex[:12]}", text.strip(), session, self.jobs.normalize_config(config), meta or {})
        with self._lock:
            self._assemblies[assembly.id] = assembly
        threading.Thread(target=self._run, args=(assembly,), name=f"mingti-assemble-{assembly.id}", daemon=True).start()
        return assembly

    def get(self, assembly_id: str) -> Assembly | None:
        with self._lock:
            return self._assemblies.get(assembly_id)

    def busy(self, session_id: str) -> bool:
        """这个对话里有没有还没组完的卷子。"""
        with self._lock:
            return any(a.session_id == session_id and a.finished_at is None for a in self._assemblies.values())

    def stop(self, assembly_id: str) -> bool:
        assembly = self.get(assembly_id)
        if assembly is None:
            return False
        assembly.stop_requested = True
        assembly.abort_scope.abort()
        for job_id in list(assembly.job_ids):
            self.jobs.stop(job_id)
        return True

    def snapshot(self, assembly: Assembly) -> dict:
        with assembly.lock:
            slots = [dict(slot) for slot in assembly.slots]
            job_ids = list(assembly.job_ids)
            view = {
                "id": assembly.id,
                "message_id": assembly.message_id,
                "session_id": assembly.session_id,
                "text": assembly.text,
                "status": assembly.status,
                "stage": assembly.stage,
                "plan": dict(assembly.plan) if assembly.plan else None,
                "paper": dict(assembly.paper) if assembly.paper else None,
                "error": assembly.error,
                "config": assembly.config,
                "created_at": _now_iso(assembly.created_at),
                "elapsed": round((assembly.finished_at or time.time()) - assembly.created_at, 1),
            }
            plan_usage = list(assembly.plan_usage)
        calls = len(plan_usage)
        tokens = sum(u.get("input_tokens", 0) + u.get("output_tokens", 0) for u in plan_usage)
        for job_id in job_ids:
            job = self.jobs.get(job_id)
            if job is None:
                continue
            overview = job.overview()
            calls += overview["calls"]
            tokens += overview["tokens"]
        for slot in slots:
            job = self.jobs.get(slot["job_id"]) if slot.get("job_id") and slot["state"] == "generating" else None
            if job is not None:
                overview = job.overview()
                slot.update(stage=overview["stage"], role=overview["role"], stem=overview["stem"] or slot.get("stem", ""))
        view["slots"] = slots
        view["totals"] = {
            "picked": sum(1 for slot in slots if slot.get("source") == "bank" and slot.get("bank_id")),
            "generated": sum(1 for slot in slots if slot.get("source") == "generated" and slot.get("bank_id")),
            "failed": sum(1 for slot in slots if slot["state"] == "failed"),
            "calls": calls,
            "tokens": tokens,
        }
        return view

    # ---------- 组卷的几步 ----------

    def _run(self, assembly: Assembly) -> None:
        try:
            profile = self.profile_for()
            edition = assembly.meta.get("edition") or getattr(profile, "textbook", "pep_a")
            outline = Outline(self.outline_for(edition))
            items = [item for item in self.bank.items() if usable(item)]
            self._plan(assembly, outline, items, profile, edition)
            self._set_stage(assembly, "selecting")
            self._select(assembly, outline, items)
            missing = [slot for slot in assembly.slots if not slot.get("bank_id")]
            if missing and not assembly.stop_requested:
                self._set_stage(assembly, "generating")
                self._generate_all(assembly, missing, outline, items)
            self._set_stage(assembly, "assembling")
            self._make_paper(assembly)
            with assembly.lock:
                assembly.status = "cancelled" if assembly.stop_requested else "succeeded"
        except AssemblyError as exc:
            with assembly.lock:
                assembly.error = str(exc)
                assembly.status = "cancelled" if assembly.stop_requested else "failed"
        except Exception as exc:  # noqa: BLE001 - 任何异常都要反馈到界面
            logger.exception("自动组卷 %s 出错", assembly.id)
            with assembly.lock:
                assembly.error = f"内部错误：{exc}"
                assembly.status = "failed"
        finally:
            with assembly.lock:
                assembly.stage = "done"
                assembly.finished_at = time.time()
            try:
                self._save(assembly)
            except Exception:  # noqa: BLE001 - 存记录失败不影响已经组好的试卷
                logger.exception("自动组卷 %s 没能存进对话", assembly.id)

    def _set_stage(self, assembly: Assembly, stage: str) -> None:
        with assembly.lock:
            assembly.stage = stage

    def _plan(self, assembly: Assembly, outline: Outline, items: list[dict], profile, edition: str) -> None:
        """请模型定组卷方案（命题双向细目表），再把每道题的知识点对到教材目录上。"""
        if not self.models.options:
            raise AssemblyError("还没有可用的模型：请在『个人设置 → 模型服务』里接入一个模型服务")
        route = self.models.route(assembly.config.get("model"))
        bank_lines: dict[str, Counter] = {}
        for item in items:
            path = outline.path_of(item)
            if not path:
                continue
            key = f"{path[-1]}（{'·'.join(path[:2])}）"
            bank_lines.setdefault(key, Counter())[(TYPES[item['question_type']], item["difficulty"])] += 1
        bank_view = [
            f"{key}：" + "，".join(f"{kind}难度{level}×{count}" for (kind, level), count in sorted(counter.items()))
            for key, counter in bank_lines.items()
        ]
        today = datetime.now()
        # 这个对话的前文（老师之前可能说过范围、题量），比聊天时带得少一些
        past = context.build(self.store.session(assembly.session_id), limit=6000)
        payload = {
            "request": assembly.text,
            "today": f"{today:%Y-%m-%d}",
            "semester": semester(today),
            "textbook": CURRICULA.get(edition, CURRICULA["pep_a"])[0],
            "teacher_grade": getattr(profile, "grade", "") or "",
            "summary": past["summary"],
            "history": past["lines"],
            "outline": outline.text(),
            "bank": bank_view[:300],
        }

        def listen(event: str, call: dict, updates: dict) -> None:
            if event == "end":
                with assembly.lock:
                    assembly.plan_usage.append(updates)

        recorder = JobRecorder(self.jobs.model_runs, listen, lane="A", role_of=lambda _stage: "planner",
                               should_stop=lambda: assembly.stop_requested, abort_scope=assembly.abort_scope)
        # 定方案只有一次调用：按思考强度，中 = 想一会儿，高 = 正常思考，极高 = 最深的一档
        thinking = CHAT_THINKING.get(assembly.config.get("thinking"), "light")
        try:
            with recorder.track(feature="paper_assembly", provider=route.provider, model=route.model,
                                prompt_version=f"{PROMPT_REVISION}:{PLAN_STAGE}") as run:
                raw = route.client.request(
                    instructions=PLAN_INSTRUCTIONS,
                    input_text=json.dumps(payload, ensure_ascii=False),
                    action=f"定组卷方案（{PLAN_STAGE}）",
                    output_schema=PLAN_SCHEMA,
                    max_tokens=4000,
                    recovery_instruction="只修复 JSON 结构。",
                    thinking=thinking,
                )
                run.capture_response(raw)
            plan = json.loads(DeepSeekQuestionAuthoringProvider._extract_output_text(raw))
        except Exception as exc:  # noqa: BLE001 - 定不出方案就说明原因，让老师换个说法再试
            if assembly.stop_requested:
                raise AssemblyError("已经停下，还没定出组卷方案") from exc
            raise AssemblyError(f"没能定出组卷方案：{exc}") from exc
        self._apply_plan(assembly, plan if isinstance(plan, dict) else {}, outline)

    def _apply_plan(self, assembly: Assembly, plan: dict, outline: Outline) -> None:
        slots = []
        for entry in plan.get("slots") or []:
            if not isinstance(entry, dict) or entry.get("type") not in TYPES:
                continue
            kind = entry["type"]
            try:
                difficulty = max(1, min(5, int(entry.get("difficulty") or 3)))
            except (TypeError, ValueError):
                difficulty = 3
            try:
                score = float(entry.get("score") or DEFAULT_SCORE[kind])
            except (TypeError, ValueError):
                score = DEFAULT_SCORE[kind]
            score = max(0.5, min(100.0, score))
            topic = str(entry.get("topic") or "").strip()[:40]
            node = outline.locate(topic)
            path = node["path"] if node else []
            slots.append({
                "type": kind,
                "difficulty": difficulty,
                "topic": topic or (path[-1] if path else ""),
                "score": int(score) if score == int(score) else round(score, 1),
                "path": path,
                "node_id": node["node_id"] if node else None,
                "state": "planned",
                "source": None,
                "bank_id": None,
                "job_id": None,
                "stem": "",
                "match": None,
                "error": None,
            })
            if len(slots) >= MAX_SLOTS:
                break
        if not slots:
            raise AssemblyError("组卷方案里没有题目。可以把要求说得具体一些再试，比如“高一期末，单选 8 道、填空 4 道、解答 5 道”")
        # 按题型分大题：单选、多选、填空、解答，各自保持方案里的先后
        slots.sort(key=lambda slot: TYPE_ORDER.index(slot["type"]))
        for number, slot in enumerate(slots, start=1):
            slot["no"] = number
        grade = plan.get("grade") if plan.get("grade") in GRADES else None
        try:
            minutes = max(10, min(300, int(plan.get("minutes") or 120)))
        except (TypeError, ValueError):
            minutes = 120
        now = datetime.now()
        title = str(plan.get("title") or "").strip()[:60] or f"自动组卷（{now.month} 月 {now.day} 日）"
        with assembly.lock:
            assembly.plan = {
                "title": title,
                "exam_name": str(plan.get("exam_name") or title).strip()[:60],
                "tag": str(plan.get("tag") or "").strip()[:30],
                "style": "practice" if plan.get("style") == "practice" else "standard",
                "purpose": str(plan.get("purpose") or "考试").strip()[:20],
                "grade": grade,
                "minutes": minutes,
                "scope": str(plan.get("scope") or "").strip()[:120],
                "summary": str(plan.get("summary") or "").strip()[:300],
                "total": sum(slot["score"] for slot in slots),
            }
            assembly.slots = slots

    def _select(self, assembly: Assembly, outline: Outline, items: list[dict]) -> None:
        """按方案从题库挑题：先找同一个考点的题，再放宽到同一节、同一章；难度最多差一档；
        放宽时不用别的题已经占了（或方案里另有一道要考）的考点，免得一份卷子里两道题考同一个点。"""
        paths = {item["id"]: outline.path_of(item) for item in items}
        in_papers = self.bank.used_in_papers()
        with assembly.lock:
            slots = assembly.slots
            planned = Counter(slot["path"][3] for slot in slots if len(slot["path"]) == 4)
        used: set[str] = set()
        taken: Counter = Counter()
        depth = {"kp": 4, "section": 3, "chapter": 2}
        for level in ("kp", "section", "chapter"):
            for slot in slots:
                if slot["bank_id"] or len(slot["path"]) < depth[level]:
                    continue
                want = slot["path"][: depth[level]]
                best, best_rank = None, None
                for item in items:
                    path = paths[item["id"]]
                    if item["id"] in used or item["question_type"] != slot["type"] or path[: depth[level]] != want:
                        continue
                    gap = abs(int(item["difficulty"]) - slot["difficulty"])
                    if gap > 1:
                        continue
                    kp = path[-1] if path else ""
                    own = len(slot["path"]) == 4 and kp == slot["path"][3]
                    if level != "kp" and not own and (taken[kp] or planned[kp]):
                        continue
                    # 难度越接近越好；没进过别的试卷的优先；再按新的优先
                    rank = (gap, item["id"] in in_papers, -_stamp(item.get("created_at") or ""))
                    if best_rank is None or rank < best_rank:
                        best, best_rank = item, rank
                if best is None:
                    continue
                used.add(best["id"])
                kp = (paths[best["id"]] or [""])[-1]
                taken[kp] += 1
                with assembly.lock:
                    slot.update(
                        state="picked", source="bank", bank_id=best["id"], stem=best["stem"][:600], match=level,
                        topic=kp or slot["topic"], difficulty=int(best["difficulty"]), path=paths[best["id"]] or slot["path"],
                    )
        with assembly.lock:
            assembly.used_ids = used  # 现场出题失败、要从题库里顶一道时，不再用这些

    def _generate_all(self, assembly: Assembly, missing: list[dict], outline: Outline, items: list[dict]) -> None:
        """题库里没有合适的，现场出题：同时出的不超过 parallel 道（和批量出题一样，最多 3 道）。"""
        parallel = max(1, min(self.jobs.settings.max_parallel_jobs, 3))
        with assembly.lock:
            for slot in missing:
                slot["state"] = "queued"
        with ThreadPoolExecutor(max_workers=parallel, thread_name_prefix=f"mingti-assemble-{assembly.id}") as pool:
            for future in [pool.submit(self._generate, assembly, slot, items) for slot in missing]:
                future.result()

    def _brief(self, assembly: Assembly, slot: dict, difficulty: int) -> QuestionDesignBrief:
        plan = assembly.plan or {}
        grade = plan.get("grade")
        topic = slot["topic"] or (slot["path"][-1] if slot["path"] else "")
        purpose = plan.get("purpose") or "考试"
        asked = f"，考查「{topic}」" if topic else ""
        text = f"请出一道{grade or '高中'}数学{TYPES[slot['type']]}{asked}，难度 {difficulty}/5（{LEVELS[difficulty]}），用于{purpose}。"
        scope = f"，全卷考查范围：{plan['scope']}" if plan.get("scope") else ""
        notes = [f"这道题是试卷《{plan.get('title')}》第 {slot['no']} 题（{SECTION[slot['type']]}，{slot['score']} 分）{scope}。"]
        # 同一章里已经定下的题：别和它们出成一样的
        chapter = slot["path"][:2]
        with assembly.lock:
            others = [
                re.sub(r"\s+", " ", other["stem"])[:100]
                for other in assembly.slots
                if other is not slot and other.get("stem") and (not chapter or other["path"][:2] == chapter)
            ]
        if others:
            notes.append("试卷里同一章已经有下面这些题，这道题的情境、设问和解法都要和它们不同：")
            budget = 1500 - len(notes[0])
            for stem in others[-6:]:
                line = f"- {stem}"
                if budget - len(line) < 0:
                    break
                notes.append(line)
                budget -= len(line)
        return QuestionDesignBrief(
            request_text=text,
            grade=grade,
            curriculum_node_id=slot["node_id"] if len(slot["path"]) == 4 else None,
            knowledge_point_query=topic or None,
            question_type=slot["type"],
            target_difficulty=difficulty,
            teaching_purpose=purpose[:300],
            additional_constraints="\n".join(notes)[:1800],
        )

    def _generate(self, assembly: Assembly, slot: dict, items: list[dict]) -> None:
        error = ""
        for attempt in range(2):
            if assembly.stop_requested:
                break
            # 第一次没出成：降一档难度再出一次（难题最容易在审核里一轮轮返工）
            difficulty = slot["difficulty"] if attempt == 0 else max(1, slot["difficulty"] - 1)
            meta = {
                **{key: assembly.meta.get(key) for key in ("detail", "edition") if assembly.meta.get(key)},
                "session_id": assembly.session_id,
                "assembly_id": assembly.id,
                "slot_no": slot["no"],
                "knowledge_label": " › ".join(slot["path"]),
                "topic_id": slot["node_id"],
                "source": "assembly",
            }
            try:
                job = self.jobs.submit(self._brief(assembly, slot, difficulty), meta, assembly.config)
            except Exception as exc:  # noqa: BLE001 - 这一道开不了头就记下原因，别的题照常出
                error = str(exc)
                break
            with assembly.lock:
                assembly.job_ids.append(job.id)
                slot.update(state="generating", source="generated", job_id=job.id, error=None)
            while job.finished_at is None:
                if assembly.stop_requested:
                    self.jobs.stop(job.id)
                time.sleep(0.1)
            if job.status == "succeeded":
                item = self.bank.add_from_record(job.record())  # 出题流程自己也会收进题库；同一道题只收一次
                if item is not None:
                    with assembly.lock:
                        slot.update(
                            state="generated", bank_id=item["id"], stem=item["stem"][:600],
                            difficulty=int(item["difficulty"]), error=None, flagged=bool(item.get("solution_issue")),
                        )
                    return
            error = job.error or "没能得到通过全部检查的题"
        # 现场也没出成：放宽条件，从题库里找一道同题型、同一册的题顶上
        with assembly.lock:
            stopped = assembly.stop_requested
            used = assembly.used_ids | {other["bank_id"] for other in assembly.slots if other.get("bank_id")}
        if not stopped:
            spare = self._spare(slot, items, used)
            if spare is not None:
                with assembly.lock:
                    slot.update(state="picked", source="bank", bank_id=spare["id"], stem=spare["stem"][:600],
                                match="spare", difficulty=int(spare["difficulty"]), error=error or None)
                return
        with assembly.lock:
            slot.update(state="failed", error="已经停下" if stopped else error or "没出成")

    @staticmethod
    def _spare(slot: dict, items: list[dict], used: set[str]) -> dict | None:
        volume = slot["path"][:1]
        candidates = [
            item for item in items
            if item["id"] not in used and item["question_type"] == slot["type"]
            and (not volume or str(item.get("knowledge") or "").startswith(volume[0]))
        ]
        return min(candidates, key=lambda item: abs(int(item["difficulty"]) - slot["difficulty"]), default=None)

    def _make_paper(self, assembly: Assembly) -> None:
        with assembly.lock:
            plan = dict(assembly.plan or {})
            chosen = [slot for slot in assembly.slots if slot.get("bank_id")]
        if not chosen:
            raise AssemblyError("已经停下，还没有挑好或出好的题" if assembly.stop_requested else "一道题也没挑到、出成，没能组成试卷")
        paper = self.bank.create_paper(Paper(
            title=plan["title"],
            minutes=plan.get("minutes"),
            show_answers=True,
            style=plan.get("style", "standard"),
            exam_name=plan.get("exam_name", "") if plan.get("style") != "practice" else "",
            tag=plan.get("tag", "") if plan.get("style") != "practice" else "",
            items=[PaperItem(bank_id=slot["bank_id"], score=slot["score"]) for slot in chosen],
        ))
        with assembly.lock:
            assembly.paper = {
                "id": paper["id"],
                "title": paper["title"],
                "count": len(chosen),
                "total": sum(slot["score"] for slot in chosen),
            }

    def _save(self, assembly: Assembly) -> None:
        view = self.snapshot(assembly)
        plan = view.get("plan") or {}
        self.store.save_message({
            "id": assembly.message_id,
            "session_id": assembly.session_id,
            "created_at": view["created_at"],
            "kind": "paper",
            "title": (view.get("paper") or {}).get("title") or plan.get("title") or assembly.text[:40],
            "text": assembly.text,
            "assembly_id": assembly.id,
            "status": view["status"],
            "plan": plan,
            "slots": [{key: value for key, value in slot.items() if key not in ("stage", "role")} for slot in view["slots"]],
            "paper": view.get("paper"),
            "error": view.get("error"),
            "config": view.get("config"),
            "seconds": view["elapsed"],
            "totals": view["totals"],
        })


def _stamp(value: str) -> float:
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0.0
