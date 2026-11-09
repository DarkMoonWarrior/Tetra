"""题库与试卷。

出好的题自动收进题库（第一次启动时，也会把以前出过的题补进来）。题库里存的是一份可以编辑的题目，
和出题记录分开：在题库里修改、删除，不会动原来的出题记录。
试卷只记题库里题目的编号和分值，所以题库里改了题，试卷也跟着变；题从题库删掉后，试卷里会跳过它。

文件夹：老师自己建的分类，可以一层套一层。一道题最多放在一个文件夹里（bank_items.folder_id，空 = 未归类）。
删文件夹不删题：里面的题和子文件夹挪到它的上一级。同一层里文件夹不重名，助手按名字找文件夹时不会对错。
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
FOLDER_NAME_MAX = 30
FOLDER_DEPTH_MAX = 5  # 文件夹最多套几层


class FolderError(ValueError):
    """文件夹操作不成立（重名、找不到、挪进自己里面……）：消息直接显示给用户。"""


def folder_name(raw: object) -> str:
    """文件夹名：去掉首尾空白，中间的连续空白并成一个；不能是空的，最多 FOLDER_NAME_MAX 个字。"""
    name = " ".join(str(raw or "").split())
    if not name:
        raise FolderError("文件夹名不能是空的")
    return name[:FOLDER_NAME_MAX]


def _same_name(a: str, b: str) -> bool:
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


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
                create table if not exists bank_folders (
                    id text primary key,
                    name text not null,
                    parent_id text,
                    created_at text not null,
                    updated_at text not null
                );
                """
            )
            # 旧的题库没有"所在文件夹"这一列：补上（旧题都是未归类）
            columns = {row[1] for row in db.execute("pragma table_info(bank_items)")}
            if "folder_id" not in columns:
                db.execute("alter table bank_items add column folder_id text")
            db.execute("create index if not exists bank_items_folder on bank_items (folder_id)")
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
            "folder_id": row["folder_id"],
            **BankQuestion.model_validate_json(row["data_json"]).model_dump(),
        }

    # ---------- 题库 ----------

    def add_from_record(self, record: dict) -> dict | None:
        with self._lock, self._connect() as db:
            self._insert(db, record)
            row = db.execute("select * from bank_items where source_id = ?", (record.get("id"),)).fetchone()
        return None if row is None else self._item(row)

    def add_imported(self, item: dict, *, source_id: str, folder_id: str | None = None) -> str | None:
        """导入（从 PDF、图片识别出来）的题收进题库；同一个来源编号不重复收。folder_id：直接放进这个文件夹。"""
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
            if folder_id and not db.execute("select 1 from bank_folders where id = ?", (folder_id,)).fetchone():
                folder_id = None  # 文件夹刚被删掉：照收，放在未归类
            cursor = db.execute(
                """
                insert or ignore into bank_items
                (id, source_id, created_at, updated_at, edited, question_type, difficulty, knowledge, data_json, folder_id)
                values (?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                """,
                (item_id, source_id, now, now, question.question_type, question.difficulty, question.knowledge,
                 question.model_dump_json(), folder_id or None),
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

    # ---------- 文件夹 ----------

    @staticmethod
    def _tree(db: sqlite3.Connection) -> dict[str, dict]:
        return {row["id"]: dict(row) for row in db.execute("select * from bank_folders order by created_at, rowid")}

    @staticmethod
    def _depth(tree: dict[str, dict], folder_id: str | None) -> int:
        """这个文件夹在第几层（最外层是 1；None 表示最外面，是 0）。"""
        depth, node, seen = 0, folder_id, set()
        while node in tree and node not in seen:
            seen.add(node)
            depth += 1
            node = tree[node]["parent_id"]
        return depth

    @staticmethod
    def _height(tree: dict[str, dict], folder_id: str) -> int:
        """这个文件夹连同下面的子文件夹一共几层（没有子文件夹是 1）。"""
        kids = [folder["id"] for folder in tree.values() if folder["parent_id"] == folder_id]
        return 1 + max((BankStore._height(tree, kid) for kid in kids), default=0)

    @staticmethod
    def _sibling_named(tree: dict[str, dict], parent_id: str | None, name: str, skip: str | None = None) -> dict | None:
        return next(
            (f for f in tree.values() if f["parent_id"] == parent_id and f["id"] != skip and _same_name(f["name"], name)),
            None,
        )

    def folders(self) -> list[dict]:
        """全部文件夹，按建立的先后：{id, name, parent_id, created_at, count（直接装着几道题）, total（连同子文件夹里的）}。"""
        with self._connect() as db:
            tree = self._tree(db)
            counts = dict(db.execute(
                "select folder_id, count(*) from bank_items where folder_id is not null group by folder_id"
            ).fetchall())
        children: dict[str | None, list[str]] = {}
        for folder in tree.values():
            parent = folder["parent_id"] if folder["parent_id"] in tree else None
            children.setdefault(parent, []).append(folder["id"])
        totals: dict[str, int] = {}

        def total(folder_id: str, seen: frozenset = frozenset()) -> int:
            if folder_id not in totals:
                kids = [kid for kid in children.get(folder_id, []) if kid not in seen]
                totals[folder_id] = counts.get(folder_id, 0) + sum(total(kid, seen | {folder_id}) for kid in kids)
            return totals[folder_id]

        return [
            {
                "id": folder["id"],
                "name": folder["name"],
                "parent_id": folder["parent_id"] if folder["parent_id"] in tree else None,
                "created_at": folder["created_at"],
                "count": counts.get(folder["id"], 0),
                "total": total(folder["id"]),
            }
            for folder in tree.values()
        ]

    def folder(self, folder_id: str) -> dict | None:
        return next((folder for folder in self.folders() if folder["id"] == folder_id), None)

    def create_folder(self, name: str, parent_id: str | None = None, *, reuse: bool = False) -> dict:
        """新建文件夹。同一层已经有同名的：reuse 为真就直接用那个（返回值带 existed），否则报错。"""
        name = folder_name(name)
        with self._lock, self._connect() as db:
            tree = self._tree(db)
            if parent_id is not None and parent_id not in tree:
                raise FolderError("上级文件夹不存在（可能刚被删掉了）")
            same = self._sibling_named(tree, parent_id, name)
            if same is not None:
                if reuse:
                    return {**same, "existed": True}
                raise FolderError(f"这里已经有叫「{same['name']}」的文件夹了")
            if self._depth(tree, parent_id) >= FOLDER_DEPTH_MAX:
                raise FolderError(f"文件夹最多套 {FOLDER_DEPTH_MAX} 层")
            folder_id = f"f_{uuid.uuid4().hex[:8]}"
            now = _now()
            db.execute(
                "insert into bank_folders (id, name, parent_id, created_at, updated_at) values (?, ?, ?, ?, ?)",
                (folder_id, name, parent_id, now, now),
            )
        return {"id": folder_id, "name": name, "parent_id": parent_id, "created_at": now, "updated_at": now}

    def rename_folder(self, folder_id: str, name: str) -> tuple[dict, str]:
        """改名；返回（改好的文件夹，原来的名字）。"""
        name = folder_name(name)
        with self._lock, self._connect() as db:
            tree = self._tree(db)
            folder = tree.get(folder_id)
            if folder is None:
                raise FolderError("文件夹不存在（可能已经删掉了）")
            same = self._sibling_named(tree, folder["parent_id"], name, skip=folder_id)
            if same is not None:
                raise FolderError(f"这里已经有叫「{same['name']}」的文件夹了")
            db.execute("update bank_folders set name = ?, updated_at = ? where id = ?", (name, _now(), folder_id))
        return {**folder, "name": name}, folder["name"]

    def move_folder(self, folder_id: str, parent_id: str | None) -> str | None:
        """把文件夹挪到 parent_id 里（None：挪到最外层）；返回原来的上级。"""
        with self._lock, self._connect() as db:
            tree = self._tree(db)
            folder = tree.get(folder_id)
            if folder is None:
                raise FolderError("文件夹不存在（可能已经删掉了）")
            if parent_id is not None:
                if parent_id not in tree:
                    raise FolderError("要挪进去的文件夹不存在（可能刚被删掉了）")
                node, seen = parent_id, set()
                while node in tree and node not in seen:
                    if node == folder_id:
                        raise FolderError("不能把文件夹挪进它自己或者它的子文件夹里")
                    seen.add(node)
                    node = tree[node]["parent_id"]
                if self._depth(tree, parent_id) + self._height(tree, folder_id) > FOLDER_DEPTH_MAX:
                    raise FolderError(f"挪过去会超过 {FOLDER_DEPTH_MAX} 层：文件夹最多套 {FOLDER_DEPTH_MAX} 层")
            same = self._sibling_named(tree, parent_id, folder["name"], skip=folder_id)
            if same is not None:
                raise FolderError(f"那里已经有叫「{same['name']}」的文件夹了")
            db.execute("update bank_folders set parent_id = ?, updated_at = ? where id = ?", (parent_id, _now(), folder_id))
        return folder["parent_id"]

    def delete_folder(self, folder_id: str) -> dict:
        """删掉文件夹：里面的题和子文件夹挪到它的上一级（子文件夹和那里的重名就在名字后面加"（2）"）。
        返回撤销要用的信息 {folder, items, children, renamed}。"""
        with self._lock, self._connect() as db:
            tree = self._tree(db)
            folder = tree.get(folder_id)
            if folder is None:
                raise FolderError("文件夹不存在（可能已经删掉了）")
            parent = folder["parent_id"] if folder["parent_id"] in tree else None
            items = [row[0] for row in db.execute("select id from bank_items where folder_id = ?", (folder_id,))]
            children = [f for f in tree.values() if f["parent_id"] == folder_id]
            siblings = [f["name"] for f in tree.values() if f["parent_id"] == parent and f["id"] != folder_id]
            renamed: dict[str, str] = {}
            now = _now()
            for child in children:
                name, n = child["name"], 2
                while any(_same_name(other, name) for other in siblings):
                    name = f"{child['name'][:FOLDER_NAME_MAX - 4]}（{n}）"
                    n += 1
                if name != child["name"]:
                    renamed[child["id"]] = child["name"]
                siblings.append(name)
                db.execute(
                    "update bank_folders set parent_id = ?, name = ?, updated_at = ? where id = ?",
                    (parent, name, now, child["id"]),
                )
            db.execute("update bank_items set folder_id = ? where folder_id = ?", (parent, folder_id))
            db.execute("delete from bank_folders where id = ?", (folder_id,))
        return {"folder": folder, "items": items, "children": [child["id"] for child in children], "renamed": renamed}

    def restore_folder(self, info: dict) -> None:
        """撤销删除：按 delete_folder 返回的信息把文件夹建回来，原来的题和子文件夹放回去。"""
        folder = info["folder"]
        with self._lock, self._connect() as db:
            tree = self._tree(db)
            parent = folder.get("parent_id") if folder.get("parent_id") in tree else None
            if folder["id"] not in tree:
                name = folder["name"]
                if self._sibling_named(tree, parent, name) is not None:
                    name = f"{name[:FOLDER_NAME_MAX - 4]}（恢复）"
                db.execute(
                    "insert into bank_folders (id, name, parent_id, created_at, updated_at) values (?, ?, ?, ?, ?)",
                    (folder["id"], name, parent, folder.get("created_at") or _now(), _now()),
                )
            for child_id in info.get("children") or []:
                if child_id in tree:
                    old = (info.get("renamed") or {}).get(child_id) or tree[child_id]["name"]
                    db.execute("update bank_folders set parent_id = ?, name = ? where id = ?", (folder["id"], old, child_id))
            ids = list(info.get("items") or [])
            for start in range(0, len(ids), 500):
                part = ids[start:start + 500]
                marks = ",".join("?" for _ in part)
                db.execute(f"update bank_items set folder_id = ? where id in ({marks})", [folder["id"], *part])

    def move_items(self, item_ids: list[str], folder_id: str | None) -> dict[str, str | None]:
        """把这些题放进文件夹（None：移出文件夹，变成未归类）；返回真的挪动了的题原来在哪个文件夹 {题号: 原文件夹}。"""
        item_ids = list(dict.fromkeys(item_ids))
        if not item_ids:
            return {}
        previous: dict[str, str | None] = {}
        with self._lock, self._connect() as db:
            if folder_id is not None and not db.execute("select 1 from bank_folders where id = ?", (folder_id,)).fetchone():
                raise FolderError("文件夹不存在（可能刚被删掉了）")
            for start in range(0, len(item_ids), 500):
                part = item_ids[start:start + 500]
                marks = ",".join("?" for _ in part)
                rows = db.execute(f"select id, folder_id from bank_items where id in ({marks})", part).fetchall()
                moving = [row["id"] for row in rows if row["folder_id"] != folder_id]
                previous.update({row["id"]: row["folder_id"] for row in rows if row["folder_id"] != folder_id})
                if moving:
                    marks = ",".join("?" for _ in moving)
                    db.execute(f"update bank_items set folder_id = ? where id in ({marks})", [folder_id, *moving])
        return previous

    def put_back(self, places: dict[str, str | None]) -> None:
        """撤销挪动：每道题放回原来的文件夹（那个文件夹已经不在了就放到未归类）。"""
        with self._lock, self._connect() as db:
            alive = set(self._tree(db))
            for item_id, folder_id in places.items():
                db.execute(
                    "update bank_items set folder_id = ? where id = ?",
                    (folder_id if folder_id in alive else None, item_id),
                )

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
