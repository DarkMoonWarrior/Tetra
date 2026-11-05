"""题库与试卷。

出好的题自动收进题库（第一次启动时，也会把以前出过的题补进来）。题库里存的是一份可以编辑的题目，
和出题记录分开：在题库里修改、删除，不会动原来的出题记录。
试卷只记题库里题目的编号和分值，所以题库里改了题，试卷也跟着变；题从题库删掉后，试卷里会跳过它。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mingti.question_authoring.schemas import QuestionType

logger = logging.getLogger("mingti.bank")

TYPE_ORDER = ("single_choice", "multiple_choice", "fill_blank", "open_response")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Option(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=4)
    text: str = Field(max_length=4000)


class BankQuestion(BaseModel):
    """题库里一道题可以编辑的内容。公式用 $…$ 写在文字里。"""

    model_config = ConfigDict(extra="forbid")

    question_type: QuestionType
    difficulty: int = Field(default=3, ge=1, le=5)
    grade: str = Field(default="", max_length=10)
    knowledge: str = Field(default="", max_length=300)
    stem: str = Field(min_length=1, max_length=20000)
    options: list[Option] = Field(default_factory=list, max_length=8)
    answer: str = Field(default="", max_length=4000)
    solution_method: str = Field(default="", max_length=8000)
    solution_steps: list[str] = Field(default_factory=list, max_length=60)
    authoring_intent: str = Field(default="", max_length=8000)
    assessed_methods: list[str] = Field(default_factory=list, max_length=30)
    common_mistakes: list[str] = Field(default_factory=list, max_length=30)
    # 整理成标准答案之前，解题员写的完整推导（审题、复核都是按它核对的）
    full_solution_method: str = Field(default="", max_length=8000)
    full_solution_steps: list[str] = Field(default_factory=list, max_length=60)
    # 配图：配图员写的图纸和按图纸画出来的 SVG（没有配图时为空）
    figure: dict | None = None
    figure_svg: str = Field(default="", max_length=300000)
    # 返工到上限时交付的题：复核还没解决的意见。老师改过这道题以后就清掉
    solution_issue: str = Field(default="", max_length=1000)


class PaperItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bank_id: str = Field(min_length=1, max_length=40)
    score: float = Field(default=5, ge=0, le=100)


class Paper(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="数学练习", min_length=1, max_length=60)
    subtitle: str = Field(default="", max_length=120)
    minutes: int | None = Field(default=None, ge=1, le=300)
    show_answers: bool = True
    # 版式：练习卷（原来的排法），或标准试卷（照高考数学卷：绝密★启用前、注意事项、新高考的大题说明）
    style: Literal["practice", "standard"] = "practice"
    exam_name: str = Field(default="", max_length=60)  # 标准试卷的考试名称，空着用"20XX年普通高等学校招生全国统一考试"
    tag: str = Field(default="", max_length=30)  # 标准试卷右上角的标记，比如"模拟卷（一）"
    items: list[PaperItem] = Field(default_factory=list, max_length=80)


def question_from_record(record: dict) -> BankQuestion | None:
    """出题记录 → 题库里的题。没出成的记录返回 None。"""
    draft = record.get("result") or {}
    if record.get("status") != "succeeded" or not draft:
        return None
    request = record.get("request") or {}
    brief = request.get("brief") or {}
    meta = request.get("meta") or {}
    curriculum = draft.get("curriculum") or {}
    path = [curriculum.get(key) for key in ("volume", "chapter", "section", "name")]
    knowledge = (
        " › ".join(dict.fromkeys(part for part in path if part))
        or meta.get("knowledge_label")
        or brief.get("knowledge_point_query")
        or ""
    )
    question_type = draft.get("question_type") or brief.get("question_type") or "single_choice"
    answer = draft.get("answer_value") or ""
    if question_type in ("fill_blank", "open_response") and draft.get("final_answer"):
        answer = draft["final_answer"]
    try:
        return BankQuestion(
            question_type=question_type,
            difficulty=brief.get("target_difficulty") or draft.get("estimated_difficulty") or 3,
            grade=brief.get("grade") or "",
            knowledge=knowledge[:300],
            stem=draft.get("stem_latex") or draft.get("stem_plain") or "",
            options=[Option(key=o["key"], text=o["text"]) for o in draft.get("options") or []],
            answer=answer,
            solution_method=draft.get("solution_method") or "",
            solution_steps=list(draft.get("solution_steps") or []),
            authoring_intent=draft.get("authoring_intent") or "",
            assessed_methods=list(draft.get("assessed_methods") or []),
            common_mistakes=list(draft.get("common_mistakes") or []),
            full_solution_method=draft.get("full_solution_method") or "",
            full_solution_steps=list(draft.get("full_solution_steps") or []),
            figure=draft.get("figure"),
            figure_svg=draft.get("figure_svg") or "",
            solution_issue=draft.get("solution_issue") or "",
        )
    except ValidationError as exc:
        logger.warning("记录 %s 没能收进题库：%s", record.get("id"), exc)
        return None


class BankStore:
    def __init__(self, database_path: Path, backfill_from: Callable[[], list[dict]] | None = None) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as db:
            db.executescript(
                """
                create table if not exists bank_items (
                    id text primary key,
                    source_id text unique,
                    created_at text not null,
                    updated_at text not null,
                    edited integer not null default 0,
                    question_type text not null,
                    difficulty integer not null,
                    knowledge text not null default '',
                    data_json text not null
                );
                create table if not exists papers (
                    id text primary key,
                    created_at text not null,
                    updated_at text not null,
                    data_json text not null
                );
                create table if not exists meta (key text primary key, value text);
                """
            )
        if backfill_from is not None:
            self._backfill(backfill_from)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database_path)
        db.row_factory = sqlite3.Row
        return db

    def _backfill(self, records: Callable[[], list[dict]]) -> None:
        """只在第一次启动时把以前出好的题补进来；之后删掉的题不会再被补回。"""
        with self._lock, self._connect() as db:
            if db.execute("select 1 from meta where key = 'backfilled'").fetchone():
                return
            added = sum(1 for record in reversed(records()) if self._insert(db, record))
            db.execute("insert into meta (key, value) values ('backfilled', ?)", (_now(),))
        logger.info("题库：补进以前出好的 %d 道题", added)

    @staticmethod
    def _insert(db: sqlite3.Connection, record: dict) -> str | None:
        question = question_from_record(record)
        if question is None:
            return None
        item_id = f"b_{uuid.uuid4().hex[:10]}"
        created = record.get("created_at") or _now()
        cursor = db.execute(
            """
            insert or ignore into bank_items
            (id, source_id, created_at, updated_at, edited, question_type, difficulty, knowledge, data_json)
            values (?, ?, ?, ?, 0, ?, ?, ?, ?)
            """,
            (
                item_id,
                record.get("id"),
                created,
                created,
                question.question_type,
                question.difficulty,
                question.knowledge,
                question.model_dump_json(),
            ),
        )
        return item_id if cursor.rowcount else None

    @staticmethod
    def _item(row: sqlite3.Row) -> dict:
        # 按当前的字段读一遍，旧版本存进来的题缺的字段补上默认值
        return {
            "id": row["id"],
            "source_id": row["source_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "edited": bool(row["edited"]),
            "imported": str(row["source_id"] or "").startswith("import:"),
            **BankQuestion.model_validate_json(row["data_json"]).model_dump(),
        }

    # ---------- 题库 ----------

    def add_from_record(self, record: dict) -> dict | None:
        with self._lock, self._connect() as db:
            self._insert(db, record)
            row = db.execute("select * from bank_items where source_id = ?", (record.get("id"),)).fetchone()
        return None if row is None else self._item(row)

    def add_imported(self, item: dict, *, source_id: str) -> str | None:
        """导入（从 PDF、图片识别出来）的题收进题库；同一个来源编号不重复收。"""
        # 从原卷上截下来的配图；太大放不下就不要了，题目照收
        figure_svg = str(item.get("figure_svg") or "")
        if len(figure_svg) > 300000:
            figure_svg = ""
        try:
            question = BankQuestion(
                question_type=item["question_type"],
                difficulty=int(item.get("difficulty") or 3),
                knowledge=str(item.get("knowledge") or "")[:300],
                stem=str(item["stem"])[:20000],
                options=[Option(key=str(o["key"])[:4], text=str(o["text"])[:4000]) for o in item.get("options") or []][:8],
                answer=str(item.get("answer") or "")[:4000],
                solution_method=str(item.get("solution_method") or "")[:8000],
                solution_steps=[str(step) for step in item.get("solution_steps") or []][:60],
                figure_svg=figure_svg,
            )
        except (ValidationError, KeyError, TypeError, ValueError) as exc:
            logger.warning("导入的题没能收进题库：%s", exc)
            return None
        item_id = f"b_{uuid.uuid4().hex[:10]}"
        now = _now()
        with self._lock, self._connect() as db:
            cursor = db.execute(
                """
                insert or ignore into bank_items
                (id, source_id, created_at, updated_at, edited, question_type, difficulty, knowledge, data_json)
                values (?, ?, ?, ?, 0, ?, ?, ?, ?)
                """,
                (item_id, source_id, now, now, question.question_type, question.difficulty, question.knowledge,
                 question.model_dump_json()),
            )
        return item_id if cursor.rowcount else None

    def items(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("select * from bank_items order by created_at desc").fetchall()
        return [self._item(row) for row in rows]

    def get(self, item_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("select * from bank_items where id = ?", (item_id,)).fetchone()
        return None if row is None else self._item(row)

    def update(self, item_id: str, question: BankQuestion) -> dict | None:
        with self._lock, self._connect() as db:
            cursor = db.execute(
                """
                update bank_items
                set updated_at = ?, edited = 1, question_type = ?, difficulty = ?, knowledge = ?, data_json = ?
                where id = ?
                """,
                (
                    _now(),
                    question.question_type,
                    question.difficulty,
                    question.knowledge,
                    question.model_dump_json(),
                    item_id,
                ),
            )
        return self.get(item_id) if cursor.rowcount else None

    def ids_for_sources(self, source_ids: list[str]) -> list[str]:
        """由这些出题记录收进题库的题（删除对话时可以一起删）。"""
        if not source_ids:
            return []
        with self._connect() as db:
            marks = ",".join("?" for _ in source_ids)
            return [row[0] for row in db.execute(f"select id from bank_items where source_id in ({marks})", source_ids)]

    def delete(self, item_ids: list[str]) -> int:
        if not item_ids:
            return 0
        with self._lock, self._connect() as db:
            marks = ",".join("?" for _ in item_ids)
            return db.execute(f"delete from bank_items where id in ({marks})", item_ids).rowcount

    def counts(self) -> dict:
        with self._connect() as db:
            bank = db.execute("select count(*) from bank_items").fetchone()[0]
            papers = db.execute("select count(*) from papers").fetchone()[0]
        return {"bank": bank, "papers": papers}

    # ---------- 试卷 ----------

    def _paper(self, row: sqlite3.Row, *, resolve: bool) -> dict:
        data = json.loads(row["data_json"])
        paper = {"id": row["id"], "created_at": row["created_at"], "updated_at": row["updated_at"], **data}
        if resolve:
            with self._connect() as db:
                for item in paper["items"]:
                    found = db.execute("select * from bank_items where id = ?", (item["bank_id"],)).fetchone()
                    item["question"] = None if found is None else self._item(found)
        return paper

    def papers(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("select * from papers order by updated_at desc").fetchall()
            alive = {row[0] for row in db.execute("select id from bank_items")}
        listed = []
        for row in rows:
            paper = self._paper(row, resolve=False)
            kept = [item for item in paper["items"] if item["bank_id"] in alive]
            listed.append(
                {
                    "id": paper["id"],
                    "title": paper["title"],
                    "updated_at": paper["updated_at"],
                    "count": len(kept),
                    "total": sum(item["score"] for item in kept),
                }
            )
        return listed

    def used_in_papers(self) -> set[str]:
        """已经组进某份试卷的题（自动组卷时优先挑没用过的）。"""
        with self._connect() as db:
            rows = db.execute("select data_json from papers").fetchall()
        used: set[str] = set()
        for row in rows:
            for item in json.loads(row["data_json"]).get("items", []):
                used.add(item.get("bank_id"))
        return used

    def paper(self, paper_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("select * from papers where id = ?", (paper_id,)).fetchone()
        return None if row is None else self._paper(row, resolve=True)

    def create_paper(self, paper: Paper) -> dict:
        paper_id = f"p_{uuid.uuid4().hex[:10]}"
        now = _now()
        with self._lock, self._connect() as db:
            db.execute(
                "insert into papers (id, created_at, updated_at, data_json) values (?, ?, ?, ?)",
                (paper_id, now, now, paper.model_dump_json()),
            )
        return self.paper(paper_id)

    def update_paper(self, paper_id: str, paper: Paper) -> dict | None:
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "update papers set updated_at = ?, data_json = ? where id = ?",
                (_now(), paper.model_dump_json(), paper_id),
            )
        return self.paper(paper_id) if cursor.rowcount else None

    def delete_paper(self, paper_id: str) -> bool:
        with self._lock, self._connect() as db:
            return db.execute("delete from papers where id = ?", (paper_id,)).rowcount > 0
