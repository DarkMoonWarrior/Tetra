"""网页版（从源码运行，tetra 命令）和桌面版共用一个数据文件夹：config.RUNTIME_DIR，默认 %LOCALAPPDATA%\\Tetra\\data。

以前从源码运行时，数据在项目的 data/runtime 里，和桌面版是分开的。用新版本启动服务时，merge_legacy_data()
把那里的数据并进共用的文件夹，只并一次：
- 题库、试卷、题库文件夹、出过的题、对话、模型调用记录：一条条并进去。编号都是随机生成的，两边不会撞；已经有的不动。
- 模型服务：按编号并进去；编号一样的，或者接口地址和 Key 都一样的，算同一个，不重复加。
- 个人设置：共用文件夹里还没有就搬过去；两边都有时，用改过次数多的那份（version 大）。
并完在旧文件夹里留一个说明文件，以后不再并（不然在共用文件夹里删掉的题，下次启动又会被并回来）。
旧文件夹原样留着当备份。合并都走 SQLite，桌面版这时开着也没关系。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from mingti import config

log = logging.getLogger("mingti.datafolder")
MARKER = "已合并到新的数据文件夹.txt"
TABLES = {
    "questions.sqlite3": ("questions", "messages", "session_flags"),
    "bank.sqlite3": ("bank_items", "papers", "bank_folders", "meta"),
    "model-runs.sqlite3": ("model_runs",),
}


def merge_legacy_data(target: Path | None = None, legacy: Path | None = None) -> dict[str, int] | None:
    """把旧文件夹（默认 data/runtime）里的数据并进 target（默认 config.RUNTIME_DIR）。
    并了返回每一类并进来几条；不用并（没有旧数据、已经并过）返回 None。设了 TETRA_HOME 时不并（测试、自己指定了数据位置）。
    没并成只写日志，不拦着服务启动，下次启动再试。"""
    if target is None and legacy is None and (config.CUSTOM_HOME or config.FROZEN):
        return None
    target = Path(target or config.RUNTIME_DIR)
    legacy = Path(legacy or config.LEGACY_RUNTIME_DIR)
    if not legacy.is_dir() or (legacy / MARKER).exists() or not any((legacy / name).exists() for name in TABLES):
        return None
    if legacy.resolve() == target.resolve():
        return None
    try:
        _prepare(target)
        counts: dict[str, int] = {}
        for name, tables in TABLES.items():
            if (legacy / name).exists():
                counts.update(_merge_rows(target / name, legacy / name, tables))
        counts["model_services"] = _merge_models(target / "models.json", legacy / "models.json")
        counts["profile"] = int(_merge_profile(target / "profile.json", legacy / "profile.json"))
    except (OSError, sqlite3.Error):
        log.exception("以前的数据（%s）没能并进 %s，下次启动再试", legacy, target)
        return None
    (legacy / MARKER).write_text(
        "这个文件夹是以前从源码运行（tetra 命令）时的数据。\n"
        f"{time.strftime('%Y-%m-%d %H:%M')} 已经并进了新的数据文件夹：\n{target}\n"
        "现在网页版和桌面版都用那里的数据。这个文件夹留着当备份，确认没问题以后可以删掉。\n",
        encoding="utf-8",
    )
    message = f"以前的数据已经并进 {target}，网页版和桌面版现在用同一份数据（原来的 {legacy} 留着当备份）"
    log.info("%s：%s", message, counts)
    print(message, flush=True)  # 从源码运行时终端里也能看到
    return counts


def _prepare(target: Path) -> None:
    """先让共用文件夹里的表都建好、是现在的样子（老版本建的表可能少几列）。"""
    from mingti.bank import BankStore
    from mingti.model_operations.registry import ModelOperationsRegistry
    from mingti.store import QuestionStore

    target.mkdir(parents=True, exist_ok=True)
    QuestionStore(target / "questions.sqlite3")
    BankStore(target / "bank.sqlite3")
    ModelOperationsRegistry(target / "model-runs.sqlite3")


def _merge_rows(target: Path, source: Path, tables: tuple[str, ...]) -> dict[str, int]:
    """把 source 里这几张表的行并进 target 的同名表；主键（或唯一的列）已经有的跳过。两边共有的列才搬。"""
    counts: dict[str, int] = {}
    with closing(sqlite3.connect(target, timeout=30)) as db:
        db.execute("attach database ? as legacy", (str(source),))
        for table in tables:
            old = [row[1] for row in db.execute(f'pragma legacy.table_info("{table}")')]
            new = {row[1] for row in db.execute(f'pragma main.table_info("{table}")')}
            columns = ", ".join(f'"{name}"' for name in old if name in new)
            if not columns:
                continue
            cursor = db.execute(f'insert or ignore into main."{table}" ({columns}) select {columns} from legacy."{table}"')
            counts[table] = max(cursor.rowcount, 0)
        db.commit()
    return counts


def _merge_models(target: Path, source: Path) -> int:
    """模型服务：按编号并进去；编号一样、或者接口地址和 Key 都一样的算同一个。返回加了几个。"""
    old = _read_json(source)
    if not old:
        return 0
    new = _read_json(target) or {}
    services = list(new.get("services") or [])
    ids = {item.get("id") for item in services}
    endpoints = {(item.get("base_url"), item.get("api_key")) for item in services}
    added = [
        item for item in old.get("services") or []
        if item.get("id") not in ids and (item.get("base_url"), item.get("api_key")) not in endpoints
    ]
    migrated = bool(new.get("migrated_env") or old.get("migrated_env"))
    if added or migrated != bool(new.get("migrated_env")):
        _replace(target, json.dumps({"services": services + added, "migrated_env": migrated}, ensure_ascii=False, indent=2))
    return len(added)


def _merge_profile(target: Path, source: Path) -> bool:
    """个人设置：共用文件夹里还没有就搬过去；两边都有时用改过次数多的那份。"""
    old = _read_json(source)
    if not old:
        return False
    new = _read_json(target)
    if new and int(new.get("version") or 0) >= int(old.get("version") or 0):
        return False
    _replace(target, source.read_text(encoding="utf-8"))
    return True


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _replace(path: Path, text: str) -> None:
    """先写临时文件再换过去：另一个服务这时来读，读到的要么是旧的、要么是新的，不会是写了一半的。"""
    temp = path.with_name(path.name + ".merge-tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)
