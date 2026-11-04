"""命题智能体的 Web 服务：命题接口 + 界面静态文件。"""

from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from typing import Literal
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from mingti.answer_key import condense, model_call
from mingti.bank import BankQuestion, BankStore, Paper
from mingti.config import BLUEPRINT_JSON, CURRICULA, CURRICULUM_CSV, RUNTIME_DIR, WEB_DIR, load_settings
from mingti.curriculum_tree import load_tree
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

settings = load_settings()
models = ModelRegistry(settings)
store = QuestionStore(RUNTIME_DIR / "questions.sqlite3")
profiles = ProfileStore(RUNTIME_DIR / "profile.json")
bank = BankStore(RUNTIME_DIR / "bank.sqlite3", backfill_from=store.all_records)
manager = JobManager(settings, CURRICULUM_CSV, RUNTIME_DIR, store, models, bank=bank)
importer = ImportManager(models, bank, lambda edition: _curriculum(edition))

app = FastAPI(title="命题智能体")


@app.middleware("http")
async def revalidate_assets(request, call_next):
    """界面文件每次都向服务端确认是否有更新，改完代码刷新就能看到，不会用到旧的缓存。"""
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
def health() -> dict:
    return {
        "ready": manager.ready,
        "default_model": models.default_id,
        "prompt_revision": PROMPT_REVISION,
        "ui_version": _ui_version(),
    }


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


@app.post("/api/jobs/{job_id}/stop")
def stop_job(job_id: str) -> dict:
    if not manager.stop(job_id):
        raise HTTPException(404, "任务不存在")
    return {"stopping": True}


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
    return {"stopping": True}


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
    return {"items": bank.items(), "counts": bank.counts()}


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
    importer.stop(_import_or_404(import_id).id)
    return {"stopping": True}


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
    paper_id: str, answers: bool = True, auto: bool = False, part: Literal["all", "exam", "answers"] = "all"
) -> str:
    return print_html(_paper_or_404(paper_id), answers=answers, auto_print=auto, part=part)


@app.get("/api/papers/{paper_id}/export")
def export_paper(paper_id: str, request: Request, format: Literal["docx", "pdf"] = "docx", answers: bool = True) -> Response:
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
    return _download(content, f"{name}.{format}", f"paper.{format}", media)


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
