"""命题记录：每次命题（成功或失败）都存进 SQLite，供"题目记录"页和实验统计使用。

同一个对话里可以连着出好几道题，session_id 记的就是属于哪个对话；以前的记录各自算一个对话。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path


def curriculum_label(result: dict | None) -> str:
    """出好的题实际考的知识点路径（没选考点时由模型挑、再对应到教材目录）。"""
    curriculum = (result or {}).get("curriculum") or {}
    parts = [curriculum.get(key) for key in ("volume", "chapter", "section", "name")]
    return " › ".join(dict.fromkeys(part for part in parts if part))


class QuestionStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as db:
            db.execute(
                """
                create table if not exists questions (
                    id text primary key,
                    created_at text not null,
                    status text not null,
                    question_type text,
                    knowledge text,
                    target_difficulty integer,
                    seconds real,
                    calls integer,
                    tokens integer,
                    request_json text not null,
                    result_json text,
                    calls_json text,
                    error text,
                    previews_json text
                )
                """
            )
            columns = {row[1] for row in db.execute("pragma table_info(questions)")}
            if "previews_json" not in columns:
                db.execute("alter table questions add column previews_json text")
            if "extra_json" not in columns:
                db.execute("alter table questions add column extra_json text")
            if "session_id" not in columns:
                db.execute("alter table questions add column session_id text")
            db.execute("update questions set session_id = id where session_id is null")
            db.execute("create index if not exists questions_session on questions (session_id, created_at)")
            # 以前没选考点的记录在列表里显示"未指定考点"；按出好的题实际考的知识点补上
            for record_id, result in db.execute(
                "select id, result_json from questions where coalesce(knowledge, '') = '' and result_json is not null"
            ).fetchall():
                label = curriculum_label(json.loads(result))
                if label:
                    db.execute("update questions set knowledge = ? where id = ?", (label, record_id))

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database_path)
        db.row_factory = sqlite3.Row
        return db

    def save(self, record: dict) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                """
                insert or replace into questions
                (id, created_at, status, question_type, knowledge, target_difficulty,
                 seconds, calls, tokens, request_json, result_json, calls_json, error, previews_json,
                 extra_json, session_id)
                values
                (:id, :created_at, :status, :question_type, :knowledge, :target_difficulty,
                 :seconds, :call_count, :tokens, :request_json, :result_json, :calls_json, :error,
                 :previews_json, :extra_json, :session_id)
                """,
                {
                    "id": record["id"],
                    "session_id": record.get("session_id") or record["id"],
                    "created_at": record["created_at"],
                    "status": record["status"],
                    "question_type": record.get("question_type"),
                    "knowledge": record.get("knowledge"),
                    "target_difficulty": record.get("target_difficulty"),
                    "seconds": record.get("seconds"),
                    "call_count": record.get("call_count"),
                    "tokens": record.get("tokens"),
                    "request_json": json.dumps(record["request"], ensure_ascii=False),
                    "result_json": json.dumps(record["result"], ensure_ascii=False)
                    if record.get("result")
                    else None,
                    "calls_json": json.dumps(record.get("call_log", []), ensure_ascii=False),
                    "error": record.get("error"),
                    "previews_json": json.dumps(record.get("previews", []), ensure_ascii=False),
                    "extra_json": json.dumps(record.get("extra", {}), ensure_ascii=False),
                },
            )

    def list(self, limit: int = 200) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                """
                select id, session_id, created_at, status, question_type, knowledge, target_difficulty,
                       seconds, calls, tokens, error
                from questions order by created_at desc limit ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def sessions(self, limit: int = 100) -> list[dict]:
        """对话列表，最近有动静的在前；标题用第一道题考的知识点。"""
        with self._connect() as db:
            rows = db.execute(
                """
                select session_id as id,
                       min(created_at) as started_at,
                       max(created_at) as updated_at,
                       count(*) as count,
                       (select knowledge from questions f where f.session_id = q.session_id
                        order by created_at asc limit 1) as knowledge,
                       (select question_type from questions f where f.session_id = q.session_id
                        order by created_at asc limit 1) as question_type,
                       (select status from questions l where l.session_id = q.session_id
                        order by created_at desc limit 1) as last_status
                from questions q
                group by session_id
                order by updated_at desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def session(self, session_id: str) -> list[dict]:
        """一个对话里的全部出题记录，按先后顺序。"""
        with self._connect() as db:
            rows = db.execute(
                "select * from questions where session_id = ? order by created_at asc", (session_id,)
            ).fetchall()
        return [self._record(row) for row in rows]

    def get(self, record_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("select * from questions where id = ?", (record_id,)).fetchone()
        return None if row is None else self._record(row)

    def all_records(self) -> list[dict]:
        """导出用：全部记录，新的在前。"""
        with self._connect() as db:
            rows = db.execute("select * from questions order by created_at desc").fetchall()
        return [self._record(row) for row in rows]

    def recent_briefs(self, limit: int) -> list[dict]:
        """偏好学习用：最近几次出题要求（只取结构化字段所在的 brief），新的在前。"""
        with self._connect() as db:
            rows = db.execute(
                "select request_json from questions order by created_at desc limit ?", (limit,)
            ).fetchall()
        briefs = []
        for row in rows:
            try:
                brief = json.loads(row["request_json"]).get("brief")
            except (ValueError, AttributeError):
                continue
            if isinstance(brief, dict):
                briefs.append(brief)
        return briefs

    @staticmethod
    def _record(row: sqlite3.Row) -> dict:
        record = dict(row)
        record["request"] = json.loads(record.pop("request_json"))
        result = record.pop("result_json")
        record["result"] = json.loads(result) if result else None
        record["call_log"] = json.loads(record.pop("calls_json") or "[]")
        record["previews"] = json.loads(record.pop("previews_json") or "[]")
        record["extra"] = json.loads(record.pop("extra_json") or "{}")
        return record
