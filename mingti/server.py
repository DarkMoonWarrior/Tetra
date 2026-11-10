"""命题智能体的 Web 服务：命题接口 + 界面静态文件。"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from mingti import context, desktop_os, organize
from mingti.answer_key import condense, model_call
from mingti.assemble import AssemblyManager
from mingti.assistant import Assistant, bank_summary
from mingti.attachments import AttachmentError, Uploads
from mingti.bank import BankQuestion, BankStore, FolderError, Paper
from mingti.config import BLUEPRINT_JSON, CURRICULA, CURRICULUM_CSV, RUNTIME_DIR, WEB_DIR, load_settings
from mingti.curriculum_tree import load_tree
from mingti.datafolder import merge_legacy_data
from mingti.export import ExportError, merge_pdfs, print_html, print_to_pdf, to_docx
from mingti.figures import illustrate
from mingti.importer import ImportError_, ImportManager
from mingti.jobs import BATCH_MAX, JobManager
from mingti.deepseek import DeepSeekClientError
from mingti.model_config import ApiStyle, ServiceIn, ThinkingStyle, discover_models, test_model
from mingti.model_config import public as public_service
from mingti.presets import public_presets
from mingti.models import ModelRegistry
from mingti.profile import (
    PROPOSAL_WINDOW,
    RESET_PHRASE,
    Profile,
    ProfileStore,
    ProfileUpdate,
    proposals,
)
from mingti.question_authoring import PROMPT_REVISION, QuestionDesignBrief
from mingti.store import QuestionStore
from mingti.team import role_of
from mingti.tracking import JobRecorder

merge_legacy_data()  # 以前从源码运行时的数据（项目里的 data/runtime）并进网页版、桌面版共用的数据文件夹，只并一次
settings = load_settings()
models = ModelRegistry(settings)
store = QuestionStore(RUNTIME_DIR / "questions.sqlite3")
profiles = ProfileStore(RUNTIME_DIR / "profile.json")
bank = BankStore(RUNTIME_DIR / "bank.sqlite3", backfill_from=store.all_records)
manager = JobManager(settings, CURRICULUM_CSV, RUNTIME_DIR, store, models, bank=bank)
importer = ImportManager(models, bank, lambda edition: _curriculum(edition), store=store)
uploads = Uploads()
assistant = Assistant(models, store, manager.model_runs, uploads=uploads, bank=bank)
assemblies = AssemblyManager(manager, bank, store, models, lambda edition: _curriculum(edition), lambda: profiles.load())

app = FastAPI(title="命题智能体")

# 桌面程序（mingti/desktop.py）：窗口关了以后，看还有没有开着的页面、有没有题在出，决定什么时候退出
DESKTOP = os.environ.get("TETRA_DESKTOP") == "1"
# 开着的页面：页面编号（每次请求带在 X-Tetra-Page 里）→ 最近一次来问的时间；页面关掉时发 /api/page-closed 去掉
PAGES: dict[str, float] = {}
RESTART_REQUESTED = False  # 设置里点了"重新打开 Tetra"（换数据文件夹以后）


@app.middleware("http")
async def revalidate_assets(request, call_next):
    """界面文件每次都向服务端确认是否有更新，改完代码刷新就能看到，不会用到旧的缓存。"""
    page = request.headers.get("x-tetra-page")
    if page and request.url.path.startswith("/api/") and request.url.path != "/api/page-closed":
        PAGES[page[:60]] = time.monotonic()
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


# 界面上不写具体厂商：以后可以接别家模型
NO_MODEL = "还没有可用的模型：请在『个人设置 → 模型服务』里接入一个模型服务"


class JobRequest(BaseModel):
    brief: QuestionDesignBrief
    meta: dict = Field(default_factory=dict)
    config: dict = Field(default_factory=dict)


@lru_cache
def _curriculum(edition: str = "pep_a") -> list[dict]:
    return load_tree(CURRICULA.get(edition, CURRICULA["pep_a"])[1])


def _ui_version() -> str:
    """界面文件最后一次修改的时间；页面打开后它变了，说明界面更新过，提示用户刷新。"""
    stamps = [p.stat().st_mtime for p in WEB_DIR.rglob("*") if p.is_file() and "vendor" not in p.parts]
    return str(int(max(stamps, default=0)))


@app.get("/api/health")
def health(request: Request) -> dict:
    return {
        "ready": manager.ready,
        "default_model": models.default_id,
        "prompt_revision": PROMPT_REVISION,
        "ui_version": _ui_version(),
        # 桌面程序起的服务：它自己的窗口里（地址带 ?window=desktop）设置里多一页"桌面版"；浏览器里打开的照常是网页版
        "desktop": DESKTOP,
        "address": str(request.base_url).rstrip("/"),
        "data_dir": str(RUNTIME_DIR),
    }


@app.post("/api/page-closed")
async def page_closed(request: Request) -> dict:
    """页面关掉时（pagehide）用 sendBeacon 发来自己的编号：桌面版据此判断还有没有开着的页面。"""
    page = (await request.body()).decode("utf-8", errors="ignore").strip()[:60]
    PAGES.pop(page, None)
    return {"ok": True}


@app.get("/api/models")
def model_list() -> dict:
    return {"default": models.default_id, "models": models.public()}


# ---------- 模型服务：在界面里接入、修改、删除；Key 只存在这台电脑上，返回给页面时只露头尾几位 ----------


class ServiceProbe(BaseModel):
    """获取模型列表、测试连接：用表单里填的地址和 Key；Key 留空时用已保存的那个服务的 Key。"""

    service_id: str | None = Field(default=None, max_length=40)
    base_url: str = Field(min_length=8, max_length=300)
    api_key: str = Field(default="", max_length=400)
    api: ApiStyle = "openai"
    preset: str = Field(default="", max_length=30)
    model: str = Field(default="", max_length=120)
    thinking: ThinkingStyle = "none"


def _services_view() -> dict:
    return {
        "services": [public_service(service) for service in models.store.services()],
        "models": models.public(),
        "default": models.default_id,
    }


def _probe_key(payload: ServiceProbe) -> str:
    if payload.api_key.strip():
        return payload.api_key.strip()
    service = models.store.get(payload.service_id) if payload.service_id else None
    if service is None or not service.api_key:
        raise HTTPException(400, "请先填写 API Key")
    return service.api_key


@app.get("/api/model-services")
def model_services() -> dict:
    return _services_view()


@app.get("/api/model-services/presets")
def model_service_presets() -> dict:
    """常用服务的模板：接口地址、请求格式、推荐型号都写好了，选一个填上 API Key 就能用。"""
    return {"presets": public_presets()}


@app.post("/api/model-services")
def add_model_service(payload: ServiceIn) -> dict:
    try:
        models.store.add(payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    models.reload()
    return _services_view()


@app.put("/api/model-services/{service_id}")
def update_model_service(service_id: str, payload: ServiceIn) -> dict:
    if models.store.update(service_id, payload) is None:
        raise HTTPException(404, "这个模型服务不存在（可能已经删掉了）")
    models.reload()
    return _services_view()


@app.delete("/api/model-services/{service_id}")
def delete_model_service(service_id: str) -> dict:
    if not models.store.delete(service_id):
        raise HTTPException(404, "这个模型服务不存在（可能已经删掉了）")
    models.reload()
    return _services_view()


@app.post("/api/model-services/discover")
def discover_service_models(payload: ServiceProbe) -> dict:
    try:
        return {"models": discover_models(payload.base_url, _probe_key(payload), api=payload.api, preset=payload.preset)}
    except DeepSeekClientError as exc:
        raise HTTPException(502, f"没取到模型列表：{exc}。可以直接手动填写型号") from exc


@app.post("/api/model-services/test")
def test_service_model(payload: ServiceProbe) -> dict:
    if not payload.model.strip():
        raise HTTPException(400, "请先填写要测试的型号")
    try:
        seconds = test_model(
            payload.base_url, _probe_key(payload), payload.model.strip(), payload.thinking, api=payload.api
        )
    except (DeepSeekClientError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "seconds": seconds}


@app.get("/api/curriculum")
def curriculum(edition: Literal["pep_a", "pep_b"] | None = None) -> list[dict]:
    """知识点目录：默认按个人设置里选的教材版本。"""
    return _curriculum(edition or profiles.load().textbook)


@app.get("/api/blueprints")
def blueprints() -> list[dict]:
    if not BLUEPRINT_JSON.exists():
        return []
    return json.loads(BLUEPRINT_JSON.read_text(encoding="utf-8"))


@app.post("/api/jobs")
def create_job(payload: JobRequest) -> dict:
    if not manager.ready:
        raise HTTPException(503, NO_MODEL)
    job = manager.submit(payload.brief, payload.meta, payload.config)
    return {"id": job.id, "session_id": job.session_id, "config": job.config}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在（服务重启后进行中的任务会丢失）")
    return job.snapshot()


def _settle(task, seconds: float = 3.0) -> str:
    """点了停止：正在进行的请求已经掐断，等后台线程收完尾（一般零点几秒），界面接着读到的就是"已停下"。"""
    done = getattr(task, "done", None)
    if done is not None:
        done.wait(seconds)
    else:
        deadline = time.monotonic() + seconds
        while task.finished_at is None and time.monotonic() < deadline:
            time.sleep(0.05)
    return task.status


@app.post("/api/jobs/{job_id}/stop")
def stop_job(job_id: str) -> dict:
    if not manager.stop(job_id):
        raise HTTPException(404, "任务不存在")
    return {"stopped": True, "status": _settle(manager.get(job_id))}


# ---------- 批量出题：同一份要求一次出好几道 ----------


class BatchRequest(JobRequest):
    count: int = Field(ge=2, le=BATCH_MAX)


@app.post("/api/batches")
def create_batch(payload: BatchRequest) -> dict:
    if not manager.ready:
        raise HTTPException(503, NO_MODEL)
    batch = manager.submit_batch(payload.brief, payload.meta, payload.config, payload.count)
    return {"id": batch.id, "session_id": batch.session_id, "count": batch.count, "config": batch.config}


@app.get("/api/batches/{batch_id}")
def batch_status(batch_id: str) -> dict:
    batch = manager.get_batch(batch_id)
    if batch is None:
        raise HTTPException(404, "任务不存在（服务重启后进行中的任务会丢失）")
    return manager.batch_snapshot(batch)


@app.post("/api/batches/{batch_id}/stop")
def stop_batch(batch_id: str) -> dict:
    if not manager.stop_batch(batch_id):
        raise HTTPException(404, "任务不存在")
    return {"stopped": True, "status": _settle(manager.get_batch(batch_id))}


# ---------- 对话助手：先判断是聊天、出题还是组卷；聊天直接回答 ----------


class AssistantRequest(BaseModel):
    text: str = Field(default="", max_length=4000)
    attachments: list[str] = Field(default_factory=list, max_length=10)  # 附件编号（/api/uploads 返回的）
    language: Literal["zh", "en"] = "zh"  # 界面语言：en 时回答用英文
    session_id: str = Field(default="", max_length=40)
    model: str | None = Field(default=None, max_length=160)
    thinking: Literal["low", "medium", "high", "max"] = "medium"  # 输入框右下角显示的那一档
    touched: bool = False  # 用户刚在输入框下方调过题型、难度这些出题选项
    current: dict = Field(default_factory=dict)  # 输入框下方现在的出题选项


def _session_id(raw: str) -> str:
    return raw if re.fullmatch(r"[A-Za-z0-9_-]{4,40}", raw or "") else f"c_{uuid.uuid4().hex[:10]}"


@app.post("/api/assistant")
def ask_assistant(payload: AssistantRequest) -> dict:
    if not models.options:
        raise HTTPException(503, NO_MODEL)
    if not payload.text.strip() and not payload.attachments:
        raise HTTPException(400, "写一句话，或者附上图片、文件再发")
    profile = profiles.load()
    session_id = _session_id(payload.session_id)
    try:
        answer = assistant.respond(
            text=payload.text.strip(),
            attachments=payload.attachments,
            language=payload.language,
            session_id=session_id,
            model_id=payload.model,
            thinking=payload.thinking,
            touched=payload.touched,
            current=payload.current,
            # 用户是老师：年级是他任教的年级，不是他自己在读的年级
            profile={
                "name": profile.display_name,
                "teaching_grade": profile.grade,
                "textbook": CURRICULA.get(profile.textbook, CURRICULA["pep_a"])[0],
            },
            bank={
                **bank_summary(bank.items()),
                "papers": bank.counts()["papers"],
                "folders": [folder["name"] for folder in bank.folders()][:60],
            },
        )
    except FolderError as exc:
        raise HTTPException(400, str(exc)) from exc
    except AttachmentError as exc:
        raise HTTPException(400, str(exc)) from exc
    except DeepSeekClientError as exc:
        raise HTTPException(502, str(exc)) from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(502, f"模型的回答没读出来（{exc}），再发一次试试") from exc
    return {**answer, "context": context.stats(store.session(session_id))}


# ---------- 聊天里的附件：先传上来，发消息时带编号 ----------


class UploadRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    data: str = Field(min_length=10, max_length=36_000_000)  # data URL（25 MB 的文件 base64 以后约 34 MB）
    thumb: str = Field(default="", max_length=150_000)  # 界面生成的缩略图（data URL），存进聊天记录


@app.post("/api/uploads")
def upload_attachment(payload: UploadRequest) -> dict:
    try:
        return uploads.add(payload.name, payload.data, payload.thumb)
    except AttachmentError as exc:
        raise HTTPException(400, str(exc)) from exc


class ChatImportRequest(BaseModel):
    session_id: str = Field(default="", max_length=40)
    # 这条消息的附件；空的表示要存的题在前文里（之前发的图片、文字，或者助手解过的题）
    uploads: list[str] = Field(default_factory=list, max_length=10)
    text: str = Field(default="", max_length=4000)
    model: str | None = Field(default=None, max_length=160)
    thinking: Literal["low", "medium", "high", "max"] = "medium"
    edition: Literal["pep_a", "pep_b"] | None = None
    folder: str | None = Field(default=None, max_length=30)  # 用户说了存到哪个文件夹（文件夹名）


CONVERSATION_TURNS = 6  # 从对话里存题时带上最近几轮聊天
CONVERSATION_CHARS = 16000


def _conversation(session_id: str) -> tuple[list[dict], list[str]]:
    """从对话里存题要用的前文：最近几轮聊天（你说的话 + 助手的回答），以及这几轮里附过的图片、PDF 的编号（新的在后）。"""
    chats = [entry for entry in store.session(session_id) if entry.get("kind") == "chat"][-CONVERSATION_TURNS:]
    lines: list[dict] = []
    upload_ids: list[str] = []
    for entry in chats:
        files = [item for item in entry.get("attachments") or [] if item.get("kind") in ("image", "pdf")]
        upload_ids += [item["id"] for item in files if item.get("id")]
        names = "、".join(item.get("name", "") for item in entry.get("attachments") or [])
        lines.append({"role": "user", "content": f"{entry.get('text') or ''}{f'（附件：{names}）' if names else ''}"})
        lines.append({"role": "assistant", "content": entry.get("reply") or ""})
    # 太长就从最早的往后丢，最近的一问一答总是留着
    while len(lines) > 2 and sum(len(line["content"]) for line in lines) > CONVERSATION_CHARS:
        lines = lines[2:]
    return lines, upload_ids


@app.post("/api/chat-imports")
def start_chat_import(payload: ChatImportRequest) -> dict:
    """把题存进题库：附件里的题逐页识别；没带附件就从前文里把讨论过的题和解答整理出来。
    没有解析的补写解析，做完存成对话里的一张核对卡片，用户确认后才存。"""
    if not models.options:
        raise HTTPException(503, NO_MODEL)
    session_id = _session_id(payload.session_id)
    edition = payload.edition or profiles.load().textbook
    chat = {
        "session_id": session_id,
        "message_id": f"m_{uuid.uuid4().hex[:12]}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "text": payload.text.strip(),
        "attachments": [],
        "thinking": payload.thinking,
        "folder": (payload.folder or "").strip() or None,
    }
    try:
        if payload.uploads:
            files = uploads.files(payload.uploads)
            if not files:
                raise AttachmentError("存进题库要用图片或 PDF（文本文件里的题请直接复制到输入框）")
            vision = assistant.vision_route(payload.model)
            chat.update(attachments=assistant.attachment_cards(payload.uploads), solver_model=payload.model or vision.id)
            job = importer.start(files, vision.id, edition, chat=chat)
        else:
            lines, upload_ids = _conversation(session_id)
            if not lines:
                raise ImportError_("对话里还没有可以存的题：把题目的图片发过来，或者直接把题目打出来")
            # 前文里附过的图片还在（4 小时内、服务没重启过）：带上原图，题干照原图录、配图从原图上截
            files = []
            for upload_id in reversed(upload_ids):
                try:
                    files = uploads.files([upload_id]) + files
                except AttachmentError:
                    continue
                if len(files) >= 4:
                    break
            route = assistant.vision_route(payload.model) if files else models.route(payload.model)
            chat["solver_model"] = payload.model or route.id
            job = importer.start_from_chat(lines, payload.text.strip(), files, route.id, edition, chat=chat)
    except (AttachmentError, ImportError_) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {**importer.snapshot(job), "session_id": session_id}


class ImportDecision(BaseModel):
    indexes: list[int] = Field(default_factory=list, max_length=400)  # 要存的题（核对卡片上勾选的）
    decline: bool = False  # 不存了
    folder_id: str | None = Field(default=None, max_length=40)  # 存进哪个文件夹（空：未归类）
    folder_name: str | None = Field(default=None, max_length=30)  # 或者新建一个这个名字的文件夹再存进去


@app.post("/api/messages/{message_id}/import")
def decide_chat_import(message_id: str, payload: ImportDecision) -> dict:
    """核对卡片上点了"存入题库"（或"不存"）：按卡片上存的题目存，服务重启过也能存。"""
    message = store.get_message(message_id)
    if message is None or message.get("kind") != "import":
        raise HTTPException(404, "这张核对卡片不存在（可能对话已经删掉了）")
    if message.get("status") == "saved":
        raise HTTPException(409, "这些题已经存进题库了")
    questions = message.get("questions") or []
    saved = []
    folder = None
    if not payload.decline:
        try:
            if payload.folder_id:
                folder = bank.folder(payload.folder_id)
                if folder is None:
                    raise FolderError("选的文件夹不存在（可能刚被删掉了），换一个再存")
            elif (payload.folder_name or "").strip():
                folder = bank.create_folder(payload.folder_name, None, reuse=True)
        except FolderError as exc:
            raise HTTPException(400, str(exc)) from exc
        for index in payload.indexes:
            if 0 <= index < len(questions) and bank.add_imported(
                questions[index], source_id=f"import:{message.get('import_id')}:{index}",
                folder_id=folder["id"] if folder else None,
            ):
                saved.append(index)
        if payload.indexes and not saved:
            raise HTTPException(409, "没存进去：这些题可能已经在题库里了")
    message.update(status="declined" if payload.decline else "saved", saved=len(saved), saved_indexes=saved,
                   saved_folder={"id": folder["id"], "name": folder["name"]} if folder else None)
    store.save_message(message)
    return {"message": message, "saved": len(saved), "counts": bank.counts()}


@app.post("/api/messages/{message_id}/undo")
def undo_organize(message_id: str) -> dict:
    """撤销一次整理题库（对话里助手建的文件夹、挪的题，全部恢复原样）。"""
    message = store.get_message(message_id)
    if message is None or message.get("kind") != "organize":
        raise HTTPException(404, "这次整理的记录不存在（可能对话已经删掉了）")
    if message.get("status") == "undone":
        raise HTTPException(409, "这次整理已经撤销过了")
    organize.undo(bank, message.get("undo") or [])
    message.update(status="undone")
    store.save_message(message)
    return {"message": message, "folders": bank.folders()}


# ---------- 上下文：这个对话带给模型的前文有多长；可以把前面的内容压缩成摘要 ----------


class CompressRequest(BaseModel):
    model: str | None = Field(default=None, max_length=160)
    auto: bool = False  # 自动压缩：只在上下文超额时才压缩，没超额就什么都不做


@app.get("/api/sessions/{session_id}/context")
def session_context(session_id: str) -> dict:
    return context.stats(store.session(session_id))


@app.post("/api/sessions/{session_id}/compress")
def compress_session(session_id: str, payload: CompressRequest) -> dict:
    if not models.options:
        raise HTTPException(503, NO_MODEL)
    try:
        summary = assistant.compress(session_id, payload.model, auto=payload.auto)
    except context.NothingToCompress as exc:
        raise HTTPException(400, str(exc)) from exc
    except DeepSeekClientError as exc:
        raise HTTPException(502, str(exc)) from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(502, f"摘要没写成（{exc}），再试一次") from exc
    return {"summary": summary, "context": context.stats(store.session(session_id))}


# ---------- 对话列表：置顶、删除 ----------


class PinRequest(BaseModel):
    pinned: bool = True


def _session_busy(session_id: str) -> bool:
    """这个对话里还有题在出（单道、一批或者组卷）。"""
    with manager._lock:
        jobs = list(manager._jobs.values())
        batches = list(manager._batches.values())
    if any(job.session_id == session_id and job.finished_at is None for job in jobs):
        return True
    if any(batch.session_id == session_id and batch.finished_at is None for batch in batches):
        return True
    return assemblies.busy(session_id)


@app.post("/api/sessions/{session_id}/pin")
def pin_session(session_id: str, payload: PinRequest) -> dict:
    store.pin(session_id, payload.pinned)
    return {"pinned": payload.pinned}


class RenameRequest(BaseModel):
    title: str = Field(default="", max_length=40)  # 空字符串：恢复成自动的名字


@app.post("/api/sessions/{session_id}/rename")
def rename_session(session_id: str, payload: RenameRequest) -> dict:
    store.rename(session_id, payload.title)
    return {"title": payload.title.strip()}


def _session_bank_ids(session_id: str) -> tuple[list[str], set[str]]:
    """这个对话出的题在题库里的编号，以及其中已经组进试卷的（这些删对话时也不动，免得试卷缺题）。"""
    ids = bank.ids_for_sources(store.question_ids(session_id))
    used = bank.used_in_papers()
    return ids, {item_id for item_id in ids if item_id in used}


@app.get("/api/sessions/{session_id}/bank")
def session_bank(session_id: str) -> dict:
    """删除对话前问一句要不要连题库里的题一起删：这个对话出的题有几道在题库里、几道组进了试卷。"""
    ids, in_papers = _session_bank_ids(session_id)
    return {"count": len(ids), "in_papers": len(in_papers)}


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str, bank_items: bool = False) -> dict:
    """bank_items=true：这个对话出的题也从题库删掉（组进试卷的留着）。"""
    if _session_busy(session_id):
        raise HTTPException(409, "这个对话里还有题在出，等它出完或先停止，再删除")
    removed = 0
    if bank_items:
        ids, in_papers = _session_bank_ids(session_id)
        removed = bank.delete([item_id for item_id in ids if item_id not in in_papers])
    return {"deleted": store.delete_session(session_id), "bank_deleted": removed, "counts": bank.counts()}


# ---------- 自动组卷：按一句话的要求定方案、从题库挑题，不够的现场出题，组成试卷 ----------


class AssemblyRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    session_id: str = Field(default="", max_length=40)
    config: dict = Field(default_factory=dict)
    meta: dict = Field(default_factory=dict)
    # 题目从哪来：auto 按老师的说法判断（定方案时一起定）；original 全部现场出新题；
    # bank_first 先从题库挑、不够的现场出；bank_only 只用题库里的题
    source: Literal["auto", "original", "bank_first", "bank_only"] = "auto"


@app.post("/api/assemblies")
def create_assembly(payload: AssemblyRequest) -> dict:
    if not manager.ready:
        raise HTTPException(503, NO_MODEL)
    meta = {
        "edition": payload.meta.get("edition") if payload.meta.get("edition") in CURRICULA else profiles.load().textbook,
        "detail": payload.meta.get("detail") or profiles.load().defaults.solution_detail,
        "source": payload.source,
    }
    assembly = assemblies.start(payload.text, _session_id(payload.session_id), payload.config, meta)
    return assemblies.snapshot(assembly)


def _assembly_or_404(assembly_id: str):
    assembly = assemblies.get(assembly_id)
    if assembly is None:
        raise HTTPException(404, "任务不存在（服务重启后进行中的任务会丢失）")
    return assembly


@app.get("/api/assemblies/{assembly_id}")
def assembly_status(assembly_id: str) -> dict:
    return assemblies.snapshot(_assembly_or_404(assembly_id))


@app.post("/api/assemblies/{assembly_id}/stop")
def stop_assembly(assembly_id: str) -> dict:
    assembly = _assembly_or_404(assembly_id)
    assemblies.stop(assembly.id)
    return {"stopped": True, "status": _settle(assembly)}


@app.get("/api/questions")
def questions() -> list[dict]:
    return store.list()


@app.get("/api/sessions")
def sessions() -> list[dict]:
    return store.sessions()


@app.get("/api/sessions/{session_id}")
def session(session_id: str) -> list[dict]:
    records = store.session(session_id)
    if not records:
        raise HTTPException(404, "这个对话不存在（可能已经删掉了）")
    return records


@app.get("/api/questions/{record_id}")
def question(record_id: str) -> dict:
    record = store.get(record_id)
    if record is None:
        raise HTTPException(404, "记录不存在")
    return record


# ---------- 个人设置 ----------


class ProposalReview(BaseModel):
    action: Literal["accept", "reject"]


class ResetRequest(BaseModel):
    confirmation: str = Field(max_length=40)


def _profile_view(profile: Profile) -> dict:
    return {
        "profile": profile.model_dump(mode="json"),
        "proposals": proposals(profile, store.recent_briefs(PROPOSAL_WINDOW)),
        "reset_phrase": RESET_PHRASE,
    }


@app.get("/api/profile")
def get_profile() -> dict:
    return _profile_view(profiles.load())


@app.patch("/api/profile")
def update_profile(changes: ProfileUpdate) -> dict:
    if changes.agent is not None:
        known = {option.id for option in models.options}
        changes.agent.model = changes.agent.model if changes.agent.model in known else None
        changes.agent.check_model = changes.agent.check_model if changes.agent.check_model in known else None
    if changes.display_name is not None:
        changes.display_name = changes.display_name.strip()
    return _profile_view(profiles.update(changes))


@app.post("/api/profile/proposals/{proposal_id}")
def review_proposal(proposal_id: str, payload: ProposalReview) -> dict:
    current = profiles.load()
    found = next(
        (p for p in proposals(current, store.recent_briefs(PROPOSAL_WINDOW)) if p["id"] == proposal_id),
        None,
    )
    if found is None:
        raise HTTPException(409, "这条建议已经不成立了（可能刚改过设置），列表已刷新")
    return _profile_view(profiles.review(found, payload.action))


@app.post("/api/profile/reset")
def reset_profile(payload: ResetRequest) -> dict:
    if payload.confirmation.strip() != RESET_PHRASE:
        raise HTTPException(400, f"请输入“{RESET_PHRASE}”确认")
    return _profile_view(profiles.reset())


# ---------- 题库与试卷 ----------


class AddToBank(BaseModel):
    record_id: str = Field(min_length=1, max_length=40)


class DeleteItems(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=500)


def _download(content: bytes, name: str, ascii_name: str, media_type: str) -> Response:
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"},
    )


@app.get("/api/counts")
def counts() -> dict:
    return bank.counts()


@app.get("/api/bank")
def bank_items() -> dict:
    return {"items": bank.items(), "folders": bank.folders(), "counts": bank.counts()}


# ---------- 题库的文件夹：老师自己建的分类，可以一层套一层；删文件夹不删题 ----------


class FolderIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    parent_id: str | None = Field(default=None, max_length=40)


class FolderPatch(BaseModel):
    name: str | None = Field(default=None, max_length=60)
    move: bool = False  # 为真时按 parent_id 挪（parent_id 为空 = 挪到最外层）
    parent_id: str | None = Field(default=None, max_length=40)


class MoveItems(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=2000)
    folder_id: str | None = Field(default=None, max_length=40)  # 空：移出文件夹（未归类）


@app.get("/api/bank/folders")
def bank_folders() -> dict:
    return {"folders": bank.folders()}


@app.post("/api/bank/folders")
def create_bank_folder(payload: FolderIn) -> dict:
    try:
        folder = bank.create_folder(payload.name, payload.parent_id)
    except FolderError as exc:
        raise HTTPException(409 if "已经有" in str(exc) else 400, str(exc)) from exc
    return {"folder": folder, "folders": bank.folders()}


@app.patch("/api/bank/folders/{folder_id}")
def update_bank_folder(folder_id: str, payload: FolderPatch) -> dict:
    try:
        if payload.name is not None:
            bank.rename_folder(folder_id, payload.name)
        if payload.move:
            bank.move_folder(folder_id, payload.parent_id)
    except FolderError as exc:
        raise HTTPException(409 if "已经有" in str(exc) else 400, str(exc)) from exc
    return {"folders": bank.folders()}


@app.delete("/api/bank/folders/{folder_id}")
def delete_bank_folder(folder_id: str) -> dict:
    """删文件夹不删题：里面的题和子文件夹挪到上一级。"""
    try:
        info = bank.delete_folder(folder_id)
    except FolderError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"moved_items": len(info["items"]), "moved_folders": len(info["children"]), "folders": bank.folders()}


@app.post("/api/bank/move")
def move_bank_items(payload: MoveItems) -> dict:
    try:
        moved = bank.move_items(payload.ids, payload.folder_id)
    except FolderError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"moved": len(moved), "folders": bank.folders()}


@app.get("/api/bank/{item_id}")
def bank_item(item_id: str) -> dict:
    item = bank.get(item_id)
    if item is None:
        raise HTTPException(404, "题库里没有这道题（可能已经删掉了）")
    return item


@app.post("/api/bank")
def add_to_bank(payload: AddToBank) -> dict:
    record = store.get(payload.record_id)
    item = bank.add_from_record(record) if record else None
    if item is None:
        raise HTTPException(404, "这条出题记录没有可以收进题库的题")
    return item


@app.put("/api/bank/{item_id}")
def update_bank_item(item_id: str, question: BankQuestion) -> dict:
    item = bank.update(item_id, question)
    if item is None:
        raise HTTPException(404, "题库里没有这道题（可能已经删掉了）")
    return item


@app.post("/api/bank/delete")
def delete_bank_items(payload: DeleteItems) -> dict:
    return {"deleted": bank.delete(payload.ids), "counts": bank.counts()}


class TidyRequest(BaseModel):
    detail: Literal["concise", "standard"] = "concise"


class FigureRequest(BaseModel):
    action: Literal["draw", "remove"] = "draw"
    request: str = Field(default="", max_length=200)


def _bank_item(item_id: str) -> dict:
    item = next((i for i in bank.items() if i["id"] == item_id), None)
    if item is None:
        raise HTTPException(404, "题库里没有这道题（可能已经删掉了）")
    return item


def _main_route():
    main = profiles.load().agent.model
    return models.route(main if main in {o.id for o in models.options} else models.default_id)


@app.post("/api/bank/{item_id}/figure")
def bank_figure(item_id: str, payload: FigureRequest) -> dict:
    """给题库里的题配图、按老师的意见重画，或者去掉配图。"""
    item = _bank_item(item_id)
    fields = {key: item[key] for key in BankQuestion.model_fields}
    if payload.action == "remove":
        fields.update(figure=None, figure_svg="")
        return {"item": bank.update(item_id, BankQuestion(**fields)), "problems": []}
    if not models.options:
        raise HTTPException(503, NO_MODEL)
    draft = {
        "question_type": item["question_type"],
        "stem_latex": item["stem"],
        "options": item["options"],
        "answer_value": item["answer"],
        "solution_steps": item["full_solution_steps"] or item["solution_steps"],
        "curriculum": {"name": item["knowledge"].split(" › ")[-1]} if item["knowledge"] else None,
    }
    recorder = JobRecorder(manager.model_runs, lambda *_: None, lane="A", role_of=role_of)
    drawn = illustrate(draft, call=model_call(_main_route(), recorder), request=payload.request.strip(), required=True)
    if drawn is None or drawn["spec"] is None:
        reason = f"（{drawn['problems'][0]}）" if drawn and drawn["problems"] else ""
        raise HTTPException(502, f"这次没画成{reason}，可以写一句要求（比如画成什么样的图）再试")
    fields.update(figure=drawn["spec"], figure_svg=drawn["svg"])
    return {"item": bank.update(item_id, BankQuestion(**fields)), "problems": drawn["problems"]}


@app.post("/api/bank/{item_id}/tidy")
def tidy_bank_item(item_id: str, payload: TidyRequest) -> dict:
    """把题库里一道题的解析整理成标准答案式；总是从完整推导出发，可以反复整理。"""
    item = _bank_item(item_id)
    if not models.options:
        raise HTTPException(503, NO_MODEL)
    full_method = item["full_solution_method"] or item["solution_method"]
    full_steps = item["full_solution_steps"] or item["solution_steps"]
    draft = {
        "question_type": item["question_type"],
        "stem_latex": item["stem"],
        "options": item["options"],
        "answer_value": item["answer"],
        "final_answer": item["answer"],
        "solution_method": full_method,
        "solution_steps": full_steps,
    }
    recorder = JobRecorder(manager.model_runs, lambda *_: None, lane="A", role_of=role_of)
    tidied = condense(draft, detail=payload.detail, call=model_call(_main_route(), recorder))
    if tidied is draft:
        raise HTTPException(502, "这次没整理成，题目没有改动。可以再试一次")
    fields = {key: item[key] for key in BankQuestion.model_fields}
    fields.update(
        solution_method=tidied["solution_method"],
        solution_steps=tidied["solution_steps"],
        full_solution_method=full_method,
        full_solution_steps=full_steps,
    )
    return bank.update(item_id, BankQuestion(**fields))


# ---------- 导入题目：上传 PDF 或图片，模型识别出题目，老师核对后收进题库 ----------


class ImportRequest(BaseModel):
    files: list[dict] = Field(min_length=1, max_length=20)  # [{name, data: dataURL}]
    model: str | None = Field(default=None, max_length=160)
    edition: Literal["pep_a", "pep_b"] | None = None


class ImportSave(BaseModel):
    questions: list[dict] = Field(min_length=1, max_length=400)


@app.get("/api/imports/models")
def import_models() -> dict:
    """可选的模型，标出哪些能看图（识别题目要能看图的模型）。"""
    return {"models": importer.vision_models()}


@app.post("/api/imports")
def start_import(payload: ImportRequest) -> dict:
    try:
        job = importer.start(payload.files, payload.model, payload.edition or profiles.load().textbook)
    except ImportError_ as exc:
        raise HTTPException(400, str(exc)) from exc
    return importer.snapshot(job)


def _import_or_404(import_id: str):
    job = importer.get(import_id)
    if job is None:
        raise HTTPException(404, "这次导入不存在（服务重启后要重新上传）")
    return job


@app.get("/api/imports/{import_id}")
def import_status(import_id: str) -> dict:
    return importer.snapshot(_import_or_404(import_id))


@app.post("/api/imports/{import_id}/stop")
def stop_import(import_id: str) -> dict:
    job = _import_or_404(import_id)
    importer.stop(job.id)
    return {"stopped": True, "status": _settle(job)}


@app.post("/api/imports/{import_id}/save")
def save_import(import_id: str, payload: ImportSave) -> dict:
    saved = importer.save(_import_or_404(import_id), payload.questions)
    return {"saved": saved, "counts": bank.counts()}


@app.get("/api/papers")
def papers() -> dict:
    return {"papers": bank.papers(), "counts": bank.counts()}


@app.post("/api/papers")
def create_paper(paper: Paper) -> dict:
    return bank.create_paper(paper)


def _paper_or_404(paper_id: str) -> dict:
    paper = bank.paper(paper_id)
    if paper is None:
        raise HTTPException(404, "试卷不存在（可能已经删掉了）")
    return paper


@app.get("/api/papers/{paper_id}")
def get_paper(paper_id: str) -> dict:
    return _paper_or_404(paper_id)


@app.put("/api/papers/{paper_id}")
def update_paper(paper_id: str, paper: Paper) -> dict:
    updated = bank.update_paper(paper_id, paper)
    if updated is None:
        raise HTTPException(404, "试卷不存在（可能已经删掉了）")
    return updated


@app.delete("/api/papers/{paper_id}")
def delete_paper(paper_id: str) -> dict:
    if not bank.delete_paper(paper_id):
        raise HTTPException(404, "试卷不存在")
    return {"deleted": True, "counts": bank.counts()}


@app.get("/api/papers/{paper_id}/print", response_class=HTMLResponse)
def print_paper(
    paper_id: str, answers: bool = True, auto: bool = False, part: Literal["all", "exam", "answers"] = "all",
    dark: bool = False,
) -> str:
    return print_html(_paper_or_404(paper_id), answers=answers, auto_print=auto, part=part, dark=dark)


def _paper_file(paper_id: str, request: Request, format: str, answers: bool) -> tuple[bytes, str, str]:
    """生成一份试卷文件：(内容, 文件名, 类型)。"""
    paper = _paper_or_404(paper_id)
    name = f"{paper['title']}{'（含答案）' if answers else ''}"
    try:
        if format == "docx":
            content = to_docx(paper, answers=answers)
            media = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        else:
            url = f"{str(request.base_url).rstrip('/')}/api/papers/{paper_id}/print?answers={str(answers).lower()}"
            if paper.get("style") == "standard":
                # 标准试卷：试题和答案分开印，各自从第 1 页编页码（"数学试题　第1页（共4页）"），再合成一个 PDF
                parts = [print_to_pdf(f"{url}&part=exam")]
                if answers and any(item.get("question") for item in paper["items"]):
                    parts.append(print_to_pdf(f"{url}&part=answers"))
                content = merge_pdfs(parts)
            else:
                content = print_to_pdf(url)
            media = "application/pdf"
    except ExportError as exc:
        raise HTTPException(503, str(exc)) from exc
    return content, f"{name}.{format}", media


@app.get("/api/papers/{paper_id}/export")
def export_paper(paper_id: str, request: Request, format: Literal["docx", "pdf"] = "docx", answers: bool = True) -> Response:
    content, name, media = _paper_file(paper_id, request, format, answers)
    return _download(content, name, f"paper.{format}", media)


class SaveRequest(BaseModel):
    format: Literal["docx", "pdf"] = "docx"
    answers: bool = True
    ask: bool | None = None  # None：按桌面版设置（每次问 / 存到固定文件夹）；True：这次弹另存为


@app.post("/api/papers/{paper_id}/save")
def save_paper(paper_id: str, payload: SaveRequest, request: Request) -> dict:
    """桌面版导出：直接存到设置里的文件夹（默认"文档\\Tetra"），或者弹系统的另存为对话框。"""
    _desktop_only()
    content, name, _media = _paper_file(paper_id, request, payload.format, payload.answers)
    prefs = desktop_os.load_prefs()
    folder = desktop_os.export_dir(prefs)
    ask = prefs["export_ask"] if payload.ask is None else payload.ask
    if ask:
        chosen = desktop_os.pick_save_file("保存试卷", folder if folder.exists() else desktop_os.documents(), name)
        if not chosen:
            return {"cancelled": True}
        target = Path(chosen)
    else:
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(400, f"保存的文件夹建不了：{folder}。可以在「个人设置 → 桌面版」里换一个") from exc
        target = desktop_os.unique_path(folder, name)
    try:
        target.write_bytes(content)
    except OSError as exc:
        raise HTTPException(400, f"没存进去（{exc.strerror or exc}）：文件可能正开着，关掉以后再试") from exc
    return {"path": str(target), "name": target.name, "folder": str(target.parent)}


# ---------- 桌面版的设置：开机自启、快捷方式、导出文件夹、数据文件夹 ----------


def _desktop_only() -> None:
    if not DESKTOP:
        raise HTTPException(400, "只有桌面版里能用")


def _desktop_view() -> dict:
    prefs = desktop_os.load_prefs()
    return {
        "frozen": desktop_os.exe() is not None,
        "installed": desktop_os.installed(),
        "app_dir": str(Path(desktop_os.exe()).parent) if desktop_os.exe() else "",
        "autostart": desktop_os.autostart_enabled(),
        "autostart_minimized": bool(prefs["autostart_minimized"]),
        "shortcuts": desktop_os.shortcuts(),
        "export_dir": str(desktop_os.export_dir(prefs)),
        "export_dir_default": not prefs["export_dir"],
        "export_ask": bool(prefs["export_ask"]),
        "data_dir": str(RUNTIME_DIR),
        "data_dir_next": prefs["data_dir_next"],
        "logs_dir": str(desktop_os.home() / "logs"),
    }


class DesktopPatch(BaseModel):
    autostart: bool | None = None
    autostart_minimized: bool | None = None
    desktop_shortcut: bool | None = None
    start_menu_shortcut: bool | None = None
    export_dir: str | None = Field(default=None, max_length=500)  # 空字符串：恢复成默认的"文档\Tetra"
    export_ask: bool | None = None


@app.get("/api/desktop")
def desktop_settings() -> dict:
    _desktop_only()
    return _desktop_view()


@app.patch("/api/desktop")
def update_desktop_settings(payload: DesktopPatch) -> dict:
    _desktop_only()
    try:
        if payload.autostart is not None:
            desktop_os.set_autostart(payload.autostart)
        if payload.desktop_shortcut is not None:
            desktop_os.set_shortcut("desktop", payload.desktop_shortcut)
        if payload.start_menu_shortcut is not None:
            desktop_os.set_shortcut("start_menu", payload.start_menu_shortcut)
    except (RuntimeError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc
    changes = {key: value for key, value in payload.model_dump().items()
               if key in ("autostart_minimized", "export_ask") and value is not None}
    if payload.export_dir is not None:
        changes["export_dir"] = payload.export_dir.strip()
    if changes:
        desktop_os.save_prefs(changes)
    return _desktop_view()


class PickRequest(BaseModel):
    purpose: Literal["export", "data"]


@app.post("/api/desktop/pick-folder")
def pick_desktop_folder(payload: PickRequest) -> dict:
    """弹系统的选文件夹对话框（在 Tetra 窗口前面）；点了取消返回 path = null。"""
    _desktop_only()
    if payload.purpose == "export":
        initial, title = desktop_os.export_dir(), "导出的 Word、PDF 存到哪个文件夹"
    else:
        initial, title = RUNTIME_DIR.parent, "把 Tetra 的数据放到哪个文件夹"
    return {"path": desktop_os.pick_folder(title, initial if initial.exists() else None)}


class OpenRequest(BaseModel):
    target: Literal["file", "folder", "export", "data", "logs", "app"]
    path: str | None = Field(default=None, max_length=1000)  # target 为 file / folder 时：刚导出的那个文件


@app.post("/api/desktop/open")
def open_desktop_path(payload: OpenRequest) -> dict:
    """在资源管理器里打开：刚导出的文件（或它所在的文件夹并选中它）、导出文件夹、数据文件夹、日志、程序所在的文件夹。"""
    _desktop_only()
    places = {
        "export": desktop_os.export_dir(), "data": RUNTIME_DIR, "logs": desktop_os.home() / "logs",
        "app": Path(desktop_os.exe()).parent if desktop_os.exe() else None,
    }
    if payload.target in ("file", "folder"):
        path = Path(payload.path or "")
        if not path.is_file():
            raise HTTPException(404, "文件不在了（可能已经移走或删掉了）")
        if payload.target == "file":
            desktop_os.open_path(path)
        else:
            desktop_os.reveal(path)
        return {"opened": str(path)}
    place = places[payload.target]
    if place is None:
        raise HTTPException(400, "从源码运行时没有程序文件夹")
    Path(place).mkdir(parents=True, exist_ok=True)
    desktop_os.open_path(Path(place))
    return {"opened": str(place)}


class DataDirRequest(BaseModel):
    path: str = Field(default="", max_length=500)  # 空字符串：取消还没搬的那次更换


@app.post("/api/desktop/data-dir")
def change_data_dir(payload: DataDirRequest) -> dict:
    """换数据文件夹：先记下来，下次打开 Tetra 时把数据复制过去（服务开着时数据库在用，不能当场搬）。"""
    _desktop_only()
    if not payload.path.strip():
        desktop_os.save_prefs({"data_dir_next": ""})
        return _desktop_view()
    try:
        target = desktop_os.check_data_target(payload.path.strip())
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc
    desktop_os.save_prefs({"data_dir_next": str(target)})
    return _desktop_view()


@app.post("/api/desktop/restart")
def restart_desktop() -> dict:
    """重新打开 Tetra（换了数据文件夹以后马上生效）：还有题在出时不能重启。"""
    global RESTART_REQUESTED
    _desktop_only()
    if _anything_running():
        raise HTTPException(409, "还有题在出，等出完再重新打开")
    RESTART_REQUESTED = True
    return {"restarting": True}


def _anything_running() -> bool:
    with manager._lock:
        tasks = [*manager._jobs.values(), *manager._batches.values()]
    with assemblies._lock:
        tasks += list(assemblies._assemblies.values())
    with importer._lock:
        tasks += list(importer._jobs.values())
    return any(task.finished_at is None for task in tasks)


@app.get("/api/profile/export")
def export_profile() -> Response:
    payload = {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "note": "命题智能体导出的个人设置和全部出题记录；不含 API Key。",
        "profile": profiles.export(),
        "questions": store.all_records(),
    }
    filename = f"mingti-export-{datetime.now():%Y%m%d-%H%M}.json"
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
