"""命题记录：每次命题（成功或失败）都存进 SQLite，供"题目记录"页和实验统计使用。

同一个对话里可以连着出好几道题，session_id 记的就是属于哪个对话；以前的记录各自算一个对话。
对话里除了出题，还有聊天（问候、答疑）和自动组卷，这两种存在 messages 表里，和出题记录按时间排在一起。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


def _when(stamp: str | None) -> datetime:
    """记录里的时间（ISO 格式）→ 可以比较先后的时间（没写时区的按 UTC）；读不出来的排在最前。"""
    try:
        moment = datetime.fromisoformat(str(stamp))
    except ValueError:
        return datetime(2000, 1, 1, tzinfo=timezone.utc)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


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
            # 聊天和自动组卷：kind 是 chat（一问一答）或 paper（一次组卷）；title 用作对话列表里的标题
            db.execute(
                """
                create table if not exists messages (
                    id text primary key,
                    session_id text not null,
                    created_at text not null,
                    kind text not null,
                    title text not null default '',
                    data_json text not null
                )
                """
            )
            db.execute("create index if not exists messages_session on messages (session_id, created_at)")
            # 对话列表里置顶的对话（置顶时间越晚越靠前），以及用户自己改的对话名称
            db.execute("create table if not exists session_flags (session_id text primary key, pinned_at text)")
            if "title" not in {row[1] for row in db.execute("pragma table_info(session_flags)")}:
                db.execute("alter table session_flags add column title text")
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
        """对话列表，最近有动静的在前。标题：对话从出题开始的，用第一道题考的知识点；
        从聊天或组卷开始的，用那一句话或那份试卷的名字（title）。"""
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
                """
            ).fetchall()
            talks = db.execute(
                """
                select session_id as id,
                       min(created_at) as started_at,
                       max(created_at) as updated_at,
                       sum(kind = 'chat') as chats,
                       sum(kind = 'paper') as papers,
                       (select title from messages f where f.session_id = m.session_id
                        order by created_at asc limit 1) as title
                from messages m
                group by session_id
                """
            ).fetchall()
            flags = db.execute("select session_id, pinned_at, title from session_flags").fetchall()
        pinned = {row["session_id"]: row["pinned_at"] for row in flags if row["pinned_at"]}
        renamed = {row["session_id"]: row["title"] for row in flags if row["title"]}
        merged = {row["id"]: {**dict(row), "chats": 0, "papers": 0, "title": ""} for row in rows}
        for talk in talks:
            session = merged.get(talk["id"])
            if session is None:
                merged[talk["id"]] = {
                    "id": talk["id"], "started_at": talk["started_at"], "updated_at": talk["updated_at"], "count": 0,
                    "knowledge": "", "question_type": None, "last_status": None,
                    "chats": talk["chats"], "papers": talk["papers"], "title": talk["title"],
                }
                continue
            session.update(chats=talk["chats"], papers=talk["papers"])
            if _when(talk["started_at"]) < _when(session["started_at"]):
                session["title"] = talk["title"]
                session["started_at"] = talk["started_at"]
            if _when(talk["updated_at"]) > _when(session["updated_at"]):
                session["updated_at"] = talk["updated_at"]
        for session in merged.values():
            session["pinned_at"] = pinned.get(session["id"])
            session["custom_title"] = renamed.get(session["id"]) or ""
        # 置顶的在最前（后置顶的在前），其余按最近有动静的在前
        top = sorted((s for s in merged.values() if s["pinned_at"]), key=lambda s: _when(s["pinned_at"]), reverse=True)
        rest = sorted((s for s in merged.values() if not s["pinned_at"]), key=lambda s: _when(s["updated_at"]), reverse=True)
        return (top + rest)[:limit]

    def pin(self, session_id: str, pinned: bool) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "insert into session_flags (session_id, pinned_at) values (?, ?) "
                "on conflict(session_id) do update set pinned_at = excluded.pinned_at",
                (session_id, datetime.now(timezone.utc).isoformat() if pinned else None),
            )

    def rename(self, session_id: str, title: str) -> None:
        """用户给对话起的名字；空字符串表示恢复成自动的名字（第一道题的考点、第一句话或试卷名）。"""
        with self._lock, self._connect() as db:
            db.execute(
                "insert into session_flags (session_id, title) values (?, ?) "
                "on conflict(session_id) do update set title = excluded.title",
                (session_id, title.strip()[:40] or None),
            )

    def delete_session(self, session_id: str) -> int:
        """删掉一个对话：它的出题记录、聊天、组卷和摘要。题库里的题和试卷是另存的，不受影响。"""
        with self._lock, self._connect() as db:
            removed = db.execute("delete from questions where session_id = ?", (session_id,)).rowcount
            removed += db.execute("delete from messages where session_id = ?", (session_id,)).rowcount
            db.execute("delete from session_flags where session_id = ?", (session_id,))
        return removed

    def session(self, session_id: str) -> list[dict]:
        """一个对话里的全部内容，按先后顺序：出题记录（kind = "question"），以及聊天、组卷（kind = "chat"/"paper"）。"""
        with self._connect() as db:
            rows = db.execute(
                "select * from questions where session_id = ? order by created_at asc", (session_id,)
            ).fetchall()
            talks = db.execute(
                "select * from messages where session_id = ? order by created_at asc", (session_id,)
            ).fetchall()
        entries = [{**self._record(row), "kind": "question"} for row in rows]
        entries += [self._message(row) for row in talks]
        return sorted(entries, key=lambda entry: _when(entry["created_at"]))

    # ---------- 聊天、组卷 ----------

    def save_message(self, message: dict) -> dict:
        """message：{id, session_id, created_at, kind, title, ...其余内容}；同一个 id 再存一次就是更新。"""
        body = {key: value for key, value in message.items() if key not in ("id", "session_id", "created_at", "kind", "title")}
        with self._lock, self._connect() as db:
            db.execute(
                """
                insert or replace into messages (id, session_id, created_at, kind, title, data_json)
                values (?, ?, ?, ?, ?, ?)
                """,
                (
                    message["id"],
                    message["session_id"],
                    message["created_at"],
                    message["kind"],
                    str(message.get("title") or "")[:80],
                    json.dumps(body, ensure_ascii=False),
                ),
            )
        return message

    def get_message(self, message_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("select * from messages where id = ?", (message_id,)).fetchone()
        return None if row is None else self._message(row)

    def recent_messages(self, session_id: str, limit: int = 12) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "select * from messages where session_id = ? order by created_at desc limit ?", (session_id, limit)
            ).fetchall()
        return [self._message(row) for row in reversed(rows)]

    @staticmethod
    def _message(row: sqlite3.Row) -> dict:
        return {
            **json.loads(row["data_json"] or "{}"),
            "id": row["id"],
            "session_id": row["session_id"],
            "created_at": row["created_at"],
            "kind": row["kind"],
            "title": row["title"],
        }

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
