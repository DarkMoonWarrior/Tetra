"""整理题库：老师在对话里说一句（"把导数的题都放进一个叫导数专题的文件夹""按章节帮我分好"），助手把题分进文件夹。

- 一次模型调用：看现有的文件夹、题库里每道题的简要信息（题型、难度、知识点、题干开头、所在文件夹）和这个对话的前文，
  写出要做的操作：新建、改名、挪动、删除文件夹，把题放进（或移出）文件夹。要挪哪些题可以列编号，也可以给条件（filter），
  条件由程序按题库去筛，题多的时候也不用把编号一个个写出来。
- 程序逐条检查、执行（文件夹不存在、重名、套得太深的跳过并说明），每一步都记下怎么撤销。
- 结果存成对话里的一条记录（kind = "organize"）：助手的回答、做了哪些改动、撤销用的信息；界面上可以一键撤销。
- 只能整理，不能删题、改题：删文件夹时里面的题挪到上一级。
"""

from __future__ import annotations

import re
import time
import uuid
from datetime import datetime, timezone

from mingti.bank import FolderError

STAGE = "bank-organize"
TYPES = {"single_choice": "单选", "multiple_choice": "多选", "fill_blank": "填空", "open_response": "解答"}
OPS = ("create_folder", "rename_folder", "move_folder", "delete_folder", "move_items")
MAX_OPERATIONS = 60

INSTRUCTIONS = """你在帮高中数学老师整理题库：把题分进文件夹。
folders 是现有的文件夹，每行：编号｜名字｜上级文件夹的编号（最外层写“-”）｜直接装着几道题。
items 是题库里的题，每行：编号｜题型｜难度（1 基础～5 压轴）｜知识点（册›章›节›考点）｜题干开头｜所在文件夹的编号（“-”是未归类）｜来源（出题 = 本应用出的，导入 = 从试卷、图片导入的）｜收进题库的日期。
this_chat 是这个对话里出的、存进题库的题的编号：用户说“刚才出的题”“这几道题”“这个对话里的题”时多半指它们。summary 和 history 是这个对话的前文。
按用户这条消息（message）的要求写出要做的操作 operations，会按顺序执行：
- create_folder：新建文件夹。name 是文件夹名（简短，不超过 15 个字）；parent 写上级文件夹的编号、前面新建的文件夹的 ref，放在最外层写 null；ref 给新文件夹起个代号（比如 "new1"），后面的操作可以用它指代这个文件夹。同一层已经有同名的文件夹就不要再建，直接用现有的编号。
- rename_folder：folder 写文件夹编号，name 写新名字。
- move_folder：把文件夹挪到别的文件夹里：folder、parent（null 表示挪到最外层）。
- delete_folder：删掉文件夹 folder。里面的题和子文件夹会挪到它的上一级，题不会被删。只在用户明确要删文件夹时用。
- move_items：把题放进文件夹 folder（编号或 ref；null 表示移出文件夹，变成未归类）。要挪哪些题：items 列出题的编号；也可以写 filter 按条件选，几个条件要同时满足：
  knowledge 知识点里包含的字（比如 "导数"，可以写成数组表示其中任意一个），type 题型（single_choice、multiple_choice、fill_blank、open_response），difficulty_min、difficulty_max，text 题干里包含的字（也可以写数组），source（generated 本应用出的 / imported 导入的），in_folder 现在所在的文件夹编号（"none" 表示未归类），this_chat（true 表示这个对话里的题）。
  按知识点、题型、难度这类整批分时用 filter；挑具体某几道（比如按题干内容判断）时用 items 列编号。每道题只能在一个文件夹里，放进新文件夹就离开原来的文件夹。
只能整理文件夹和题的归属，不能删题、改题目内容。用户要的超出这些（删题、改题、出题），operations 留空，在 reply 里说明删题、改题可以在“题库”页面里操作。
要求说得笼统时按最常见的理解做：按章节分就按知识点路径里的“章”建文件夹；按题型分就建单选题、多选题、填空题、解答题；按难度分就建基础、中档、较难这类文件夹。用户只说“整理一下”又没说按什么，就按章节分。
reply：一两句话告诉用户你做了什么（不用逐道列出），用 reply_language 指定的语言；没有可以做的就说明原因。
只输出 JSON：{"reply": "...", "operations": [{"op": "create_folder", "ref": "new1", "name": "导数专题", "parent": null}, {"op": "move_items", "folder": "new1", "filter": {"knowledge": "导数"}}]}"""

SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "operations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": list(OPS)},
                    "ref": {"type": ["string", "null"]},
                    "name": {"type": ["string", "null"]},
                    "folder": {"type": ["string", "null"]},
                    "parent": {"type": ["string", "null"]},
                    "items": {"type": "array", "items": {"type": "string"}},
                    "filter": {"type": ["object", "null"]},
                },
                "required": ["op"],
            },
        },
    },
    "required": ["reply", "operations"],
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _flat(text: object) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _key(text: object) -> str:
    return re.sub(r"\s+", "", str(text or "")).casefold()


def chat_items(store, bank_items: list[dict], session_id: str) -> list[str]:
    """这个对话里出的题、从附件存进题库的题，在题库里的编号。"""
    by_source = {item.get("source_id"): item["id"] for item in bank_items if item.get("source_id")}
    found = [by_source[record_id] for record_id in store.question_ids(session_id) if record_id in by_source]
    for entry in store.session(session_id):
        if entry.get("kind") == "import":
            for index in entry.get("saved_indexes") or []:
                source = f"import:{entry.get('import_id')}:{index}"
                if source in by_source:
                    found.append(by_source[source])
    return list(dict.fromkeys(found))


def listing(items: list[dict], folders: list[dict]) -> dict:
    """给模型看的题库：每个文件夹一行、每道题一行。题多时题干截得更短，太多就不带题干。"""
    stem_size = 60 if len(items) <= 300 else 30 if len(items) <= 900 else 0
    rows = []
    for item in items:
        path = " › ".join(part.strip() for part in str(item.get("knowledge") or "").split("›") if part.strip())
        stem = _flat(item.get("stem"))
        stem = stem[:stem_size] + ("…" if len(stem) > stem_size else "") if stem_size else ""
        rows.append("｜".join([
            item["id"],
            TYPES.get(item.get("question_type"), "其他"),
            str(item.get("difficulty") or ""),
            path[-70:] or "没写知识点",
            stem,
            item.get("folder_id") or "-",
            "导入" if item.get("imported") else "出题",
            str(item.get("created_at") or "")[:10],
        ]))
    lines = [f"{f['id']}｜{f['name']}｜{f.get('parent_id') or '-'}｜{f['count']}" for f in folders]
    return {"folders": lines, "items": rows}


def _matches(item: dict, rule: dict, this_chat: set[str]) -> bool:
    def words(value) -> list[str]:
        values = value if isinstance(value, list) else [value]
        return [_key(v) for v in values if isinstance(v, str) and _key(v)]

    knowledge = words(rule.get("knowledge"))
    if knowledge and not any(word in _key(item.get("knowledge")) for word in knowledge):
        return False
    text = words(rule.get("text"))
    if text and not any(word in _key(item.get("stem")) for word in text):
        return False
    if rule.get("type") in TYPES and item.get("question_type") != rule["type"]:
        return False
    try:
        if rule.get("difficulty_min") is not None and int(item.get("difficulty") or 0) < int(rule["difficulty_min"]):
            return False
        if rule.get("difficulty_max") is not None and int(item.get("difficulty") or 0) > int(rule["difficulty_max"]):
            return False
    except (TypeError, ValueError):
        pass
    if rule.get("source") == "imported" and not item.get("imported"):
        return False
    if rule.get("source") == "generated" and item.get("imported"):
        return False
    where = rule.get("in_folder")
    if isinstance(where, str) and where:
        if where in ("none", "-", "未归类"):
            if item.get("folder_id"):
                return False
        elif item.get("folder_id") != where:
            return False
    if rule.get("this_chat") is True and item["id"] not in this_chat:
        return False
    return True


def _meaningful(rule: object) -> bool:
    return isinstance(rule, dict) and any(value not in (None, "", [], False) for value in rule.values())


class Plan:
    """按顺序执行模型写的操作，记下改动（给界面显示）和撤销的办法。"""

    def __init__(self, bank, items: list[dict], this_chat: list[str]) -> None:
        self.bank = bank
        self.items = {item["id"]: dict(item) for item in items}
        self.this_chat = set(this_chat)
        self.folders = {folder["id"]: dict(folder) for folder in bank.folders()}
        self.refs: dict[str, str] = {}
        self.changes: list[dict] = []
        self.problems: list[str] = []
        self.undo: list[dict] = []
        self.focus: str | None = None

    def _name(self, folder_id: str | None) -> str:
        return self.folders[folder_id]["name"] if folder_id in self.folders else ""

    def _folder(self, raw: object, *, allow_root: bool = False) -> tuple[bool, str | None]:
        """文件夹编号、新建时的 ref、或者（模型写成了）名字 → (找到了没有, 编号)；allow_root 时 null 表示最外层。"""
        if raw in (None, "", "null", "-") or (isinstance(raw, str) and raw.strip() in ("最外层", "未归类", "none")):
            return allow_root, None
        text = str(raw).strip()
        if text in self.refs:
            return True, self.refs[text]
        if text in self.folders:
            return True, text
        named = [fid for fid, folder in self.folders.items() if _key(folder["name"]) == _key(text)]
        if len(named) == 1:
            return True, named[0]
        return False, None

    def run(self, operations: list) -> None:
        for raw in (operations or [])[:MAX_OPERATIONS]:
            if not isinstance(raw, dict) or raw.get("op") not in OPS:
                continue
            try:
                getattr(self, f"_{raw['op']}")(raw)
            except FolderError as exc:
                self.problems.append(str(exc))

    def _create_folder(self, raw: dict) -> None:
        found, parent = self._folder(raw.get("parent"), allow_root=True)
        if not found:
            raise FolderError(f"没找到要放新文件夹的上级「{raw.get('parent')}」，「{raw.get('name') or ''}」没有建")
        folder = self.bank.create_folder(raw.get("name") or "", parent, reuse=True)
        if raw.get("ref"):
            self.refs[str(raw["ref"]).strip()] = folder["id"]
        self.focus = folder["id"]
        if folder.get("existed"):
            return
        self.folders[folder["id"]] = {**folder, "count": 0}
        self.changes.append({"kind": "create", "name": folder["name"], "parent": self._name(parent)})
        self.undo.append({"op": "remove_created", "folder": folder["id"]})

    def _rename_folder(self, raw: dict) -> None:
        found, folder_id = self._folder(raw.get("folder"))
        if not found or folder_id is None:
            raise FolderError(f"没找到要改名的文件夹「{raw.get('folder')}」")
        folder, old = self.bank.rename_folder(folder_id, raw.get("name") or "")
        if folder["name"] == old:
            return
        self.folders[folder_id]["name"] = folder["name"]
        self.focus = folder_id
        self.changes.append({"kind": "rename", "old": old, "name": folder["name"]})
        self.undo.append({"op": "rename", "folder": folder_id, "name": old})

    def _move_folder(self, raw: dict) -> None:
        found, folder_id = self._folder(raw.get("folder"))
        if not found or folder_id is None:
            raise FolderError(f"没找到要挪的文件夹「{raw.get('folder')}」")
        found, parent = self._folder(raw.get("parent"), allow_root=True)
        if not found:
            raise FolderError(f"没找到要挪进去的文件夹「{raw.get('parent')}」")
        before = self.bank.move_folder(folder_id, parent)
        if before == parent:
            return
        self.folders[folder_id]["parent_id"] = parent
        self.focus = folder_id
        self.changes.append({"kind": "move_folder", "name": self._name(folder_id), "to": self._name(parent)})
        self.undo.append({"op": "move_folder", "folder": folder_id, "parent": before})

    def _delete_folder(self, raw: dict) -> None:
        found, folder_id = self._folder(raw.get("folder"))
        if not found or folder_id is None:
            raise FolderError(f"没找到要删的文件夹「{raw.get('folder')}」")
        name, parent = self._name(folder_id), self.folders[folder_id].get("parent_id")
        info = self.bank.delete_folder(folder_id)
        for item_id in info["items"]:
            if item_id in self.items:
                self.items[item_id]["folder_id"] = parent
        for child in info["children"]:
            if child in self.folders:
                self.folders[child]["parent_id"] = parent
        self.folders.pop(folder_id, None)
        if self.focus == folder_id:
            self.focus = parent
        self.changes.append({"kind": "delete", "name": name, "count": len(info["items"]), "to": self._name(parent)})
        self.undo.append({"op": "restore", "info": info})

    def _move_items(self, raw: dict) -> None:
        found, folder_id = self._folder(raw.get("folder"), allow_root=True)
        if not found:
            raise FolderError(f"没找到文件夹「{raw.get('folder')}」，这一步的题没有挪")
        picked = [item_id for item_id in raw.get("items") or [] if isinstance(item_id, str) and item_id in self.items]
        rule = raw.get("filter")
        if _meaningful(rule):
            picked += [item_id for item_id, item in self.items.items() if _matches(item, rule, self.this_chat)]
        picked = list(dict.fromkeys(picked))
        if not picked:
            target = f"「{self._name(folder_id)}」" if folder_id else "未归类"
            self.problems.append(f"没有找到要放进{target}的题")
            return
        previous = self.bank.move_items(picked, folder_id)
        for item_id in previous:
            self.items[item_id]["folder_id"] = folder_id
        if folder_id:
            self.focus = folder_id
        if not previous:
            return  # 这些题本来就在这个文件夹里
        self.changes.append({"kind": "move_items", "count": len(previous), "to": self._name(folder_id)})
        self.undo.append({"op": "put_back", "places": previous})


def undo(bank, steps: list[dict]) -> None:
    """按记下的办法倒着撤销一次整理。"""
    for step in reversed(steps or []):
        try:
            if step["op"] == "put_back":
                bank.put_back(step["places"])
            elif step["op"] == "remove_created":
                if bank.folder(step["folder"]) is not None:
                    bank.delete_folder(step["folder"])
            elif step["op"] == "rename":
                bank.rename_folder(step["folder"], step["name"])
            elif step["op"] == "move_folder":
                bank.move_folder(step["folder"], step["parent"])
            elif step["op"] == "restore":
                bank.restore_folder(step["info"])
        except FolderError:
            continue  # 这一步已经撤不了（比如文件夹后来被手动删了），其余照常撤


def run(*, call, bank, store, text: str, session_id: str, past: dict, language: str, model: str,
        thinking: str, started: float, usage: list) -> dict:
    """整理一次题库并存成对话里的一条记录；call(payload) → 模型返回的 JSON。"""
    items = bank.items()
    folders = bank.folders()
    this_chat = chat_items(store, items, session_id)
    payload = {
        "summary": past.get("summary"),
        "history": past.get("lines") or [],
        **listing(items, folders),
        "this_chat": this_chat,
        "reply_language": "English" if language == "en" else "中文",
        "message": text,
    }
    result = call(payload)
    plan = Plan(bank, items, this_chat)
    plan.run(result.get("operations") if isinstance(result.get("operations"), list) else [])
    reply = str(result.get("reply") or "").strip()
    if not plan.changes and not reply:
        reply = "这次没有需要改动的地方。"
    message = {
        "id": f"m_{uuid.uuid4().hex[:12]}",
        "session_id": session_id,
        "created_at": _now(),
        "kind": "organize",
        "title": _flat(text)[:40],
        "text": text,
        "reply": reply,
        "changes": plan.changes,
        "problems": plan.problems[:10],
        "undo": plan.undo,
        "focus": plan.focus,
        "status": "done" if plan.changes else "nothing",
        "model": model,
        "thinking": thinking,
        "seconds": round(time.time() - started, 1),
        "calls": len(usage),
        "tokens": sum(u.get("input_tokens", 0) + u.get("output_tokens", 0) for u in usage),
    }
    store.save_message(message)
    return message


def describe(change: dict) -> str:
    """一条改动的说明（上下文、摘要里用；界面上另有自己的写法）。"""
    kind = change.get("kind")
    if kind == "create":
        return f"新建文件夹「{change.get('name')}」" + (f"（在「{change['parent']}」里）" if change.get("parent") else "")
    if kind == "rename":
        return f"文件夹「{change.get('old')}」改名为「{change.get('name')}」"
    if kind == "move_folder":
        return f"文件夹「{change.get('name')}」挪到" + (f"「{change['to']}」里" if change.get("to") else "最外层")
    if kind == "delete":
        return f"删掉文件夹「{change.get('name')}」，里面的 {change.get('count', 0)} 道题挪到" + (f"「{change['to']}」" if change.get("to") else "未归类")
    if kind == "move_items":
        return f"{change.get('count', 0)} 道题放进" + (f"「{change['to']}」" if change.get("to") else "未归类")
    return ""
