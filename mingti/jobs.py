"""命题任务：在后台线程里跑一次多智能体命题，界面通过轮询读取进度。"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from mingti.answer_key import STYLES, condense, model_call
from mingti.config import CURRICULA, Settings
from mingti.figures import FIGURE_RULE, illustrate, needs_figure
from mingti.curriculum import CsvCurriculumCatalog
from mingti.math_verifier import MathVerifier
from mingti.model_operations import ModelOperationsRegistry
from mingti.models import ModelRegistry
from mingti.question_authoring import QuestionAuthoring, QuestionAuthoringError, QuestionDesignBrief
from mingti.question_authoring.providers import THINK_LEVELS
from mingti.store import QuestionStore, curriculum_label
from mingti.team import LANE_IDS, ROLES, TeamProvider, role_of
from mingti.tracking import JobRecorder

logger = logging.getLogger("mingti.jobs")


class Job:
    """一次命题任务；同时实现 team.TeamHooks，接收各命题组的进展。"""

    def __init__(self, job_id: str, brief: QuestionDesignBrief, meta: dict, config: dict, model_runs) -> None:
        self.id = job_id
        # 属于哪个对话：界面带过来的编号；没有（或格式不对）就自己算一个新对话
        session = str((meta or {}).get("session_id") or "")
        self.session_id = session if re.fullmatch(r"[A-Za-z0-9_-]{4,40}", session) else job_id
        self.brief = brief
        self.meta = meta
        self.config = config
        self.model_runs = model_runs
        self.status = "queued"
        self.created_at = time.time()
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.calls: list[dict] = []
        self.events: list[dict] = []
        self.lanes: dict[str, dict] = {
            lane: {"id": lane, "status": "waiting", "stage": None, "note": "", "previews": []}
            for lane in LANE_IDS[: config["lanes"]]
        }
        self.winner: str | None = None
        self.result: dict | None = None
        self.error: str | None = None
        self.stop_requested = False
        # 兜底上限：难题偶尔会一轮轮返工下去。超过就停下并说明，不让人干等十分钟最后还是失败
        self.max_calls = 70 + 30 * (config["lanes"] - 1)
        # 极高档每次都想得更久，时间上限放宽到 20 分钟
        self.max_seconds = 1200 if config.get("thinking") == "max" else 480
        self.over_budget = False
        self._halt = threading.Event()
        self.lock = threading.Lock()

    # ---- team.TeamHooks ----

    def recorder_for(self, lane: str, *, after_win: bool = False) -> JobRecorder:
        # 定出题目之后其余方案已经叫停；整理解析这一步只在用户点了停止时才不做
        return JobRecorder(
            self.model_runs,
            self._on_call,
            lane=lane,
            role_of=role_of,
            should_stop=(lambda: self.stop_requested) if after_win else self.should_stop,
        )

    def should_stop(self) -> bool:
        return self._halt.is_set()

    def halt(self) -> None:
        self._halt.set()

    def lane_status(self, lane: str, status: str, note: str = "") -> None:
        with self.lock:
            info = self.lanes[lane]
            # 胜出的组不会再被改成"已停止"；任务结束后落后的组也不再更新
            if info["status"] == "won" or self.finished_at:
                return
            info["status"] = status
            info["note"] = note
            if status == "won":
                self.winner = lane
        several = len(self.lanes) > 1
        if status == "won":
            self.event(lane, "check", "pass", f"采用方案 {lane}，其余方案停下" if several else "全部检查通过")
        elif status == "failed":
            self.event(lane, "check", "reject", f"方案 {lane} 没出成：{note}" if several else f"没出成：{note}")

    def lane_progress(self, lane: str, stage: str) -> None:
        with self.lock:
            self.lanes[lane]["stage"] = stage

    def lane_statement(self, lane: str, preview: dict) -> None:
        with self.lock:
            previews = self.lanes[lane]["previews"]
            previews.append({"at": self._offset(time.time()), "statement": preview})
            version = len(previews)
        self.event(lane, "composer", "note", "题干定稿" if version == 1 else f"题干改到第 {version} 版")

    def event(self, lane: str, role: str, kind: str, text: str, details: list[str] | None = None) -> None:
        with self.lock:
            if self.finished_at:
                return
            self.events.append(
                {
                    "at": self._offset(time.time()),
                    "lane": lane,
                    "role": role,
                    "kind": kind,
                    "text": text,
                    "details": list(details or []),
                }
            )

    # ---- 内部 ----

    def _on_call(self, event: str, call: dict, updates: dict) -> None:
        exceeded = False
        with self.lock:
            if event == "start":
                if not self.finished_at:
                    self.calls.append(call)
                elapsed = time.time() - (self.started_at or time.time())
                if self.winner is None and not self.over_budget and (
                    len(self.calls) > self.max_calls or elapsed > self.max_seconds
                ):
                    self.over_budget = exceeded = True
            else:
                call.update(updates)
        if exceeded:
            self.halt()
            self.event(
                self.winner or "A", "check", "reject",
                f"已经调用模型 {len(self.calls)} 次、用时 {round(elapsed / 60, 1)} 分钟还没出成，先停下",
            )

    def _offset(self, moment: float) -> float:
        return round(moment - (self.started_at or moment), 2)

    def _call_view(self, call: dict, now: float) -> dict:
        running = "ok" not in call
        return {
            "stage": call["stage"],
            "lane": call["lane"],
            "role": call["role"],
            "model": call["model"],
            "at": self._offset(call["started"]),
            "seconds": round(now - call["started"], 1) if running else call["seconds"],
            "status": "running" if running else ("ok" if call["ok"] else "failed"),
            "input_tokens": call.get("input_tokens", 0),
            "output_tokens": call.get("output_tokens", 0),
        }

    def snapshot(self) -> dict:
        now = time.time()
        with self.lock:
            calls = [self._call_view(call, now) for call in self.calls]
            end = self.finished_at or now
            lanes = []
            for info in self.lanes.values():
                lane_calls = [c for c in calls if c["lane"] == info["id"]]
                lanes.append(
                    {
                        "id": info["id"],
                        "status": info["status"],
                        "stage": info["stage"],
                        "note": info["note"],
                        "previews": list(info["previews"]),
                        "calls": len(lane_calls),
                        "tokens": sum(c["input_tokens"] + c["output_tokens"] for c in lane_calls),
                    }
                )
            return {
                "id": self.id,
                "session_id": self.session_id,
                "status": self.status,
                "brief": self.brief.model_dump(mode="json"),
                "meta": self.meta,
                "config": self.config,
                "elapsed": round(end - self.started_at, 1) if self.started_at else 0,
                "lanes": lanes,
                "winner": self.winner,
                "events": list(self.events),
                "calls": calls,
                "totals": {
                    "calls": len(calls),
                    "input_tokens": sum(c["input_tokens"] for c in calls),
                    "output_tokens": sum(c["output_tokens"] for c in calls),
                },
                "result": self.result,
                "error": self.error,
            }

    def latest_stem(self) -> str:
        """这道题目前的题干：出好了用定稿；还在出就用最近一次审过的题干；都还没有就是空串。"""
        with self.lock:
            if self.result:
                return self.result.get("stem_latex") or self.result.get("stem_plain") or ""
            previews = [preview for info in self.lanes.values() for preview in info["previews"]]
        if not previews:
            return ""
        statement = max(previews, key=lambda preview: preview["at"])["statement"]
        return statement.get("stem_latex") or statement.get("stem_plain") or ""

    def overview(self) -> dict:
        """批量出题时列表里的一行：状态、正在做哪一步、题干开头、没出成的原因（不带完整的调用记录）。"""
        stem = self.latest_stem()
        now = time.time()
        with self.lock:
            running = [call for call in self.calls if "ok" not in call]
            current = running[-1] if running and self.status == "running" else None
            end = self.finished_at or now
            return {
                "job_id": self.id,
                "status": self.status,
                "elapsed": round(end - self.started_at, 1) if self.started_at else 0,
                "calls": len(self.calls),
                "tokens": sum(call.get("input_tokens", 0) + call.get("output_tokens", 0) for call in self.calls),
                "stage": current["stage"] if current else None,
                "role": current["role"] if current else None,
                "stem": stem[:600],
                "error": self.error,
            }

    def record(self) -> dict:
        snap = self.snapshot()
        totals = snap["totals"]
        return {
            "id": self.id,
            "session_id": self.session_id,
            "created_at": datetime.fromtimestamp(self.created_at, timezone.utc).isoformat(),
            "status": self.status,
            "question_type": self.brief.question_type,
            "knowledge": self.meta.get("knowledge_label")
            or self.brief.knowledge_point_query
            or curriculum_label(self.result),
            "target_difficulty": self.brief.target_difficulty,
            "seconds": snap["elapsed"],
            "call_count": totals["calls"],
            "tokens": totals["input_tokens"] + totals["output_tokens"],
            "request": {"brief": snap["brief"], "meta": self.meta, "config": self.config},
            "result": self.result,
            "call_log": snap["calls"],
            "extra": {"lanes": snap["lanes"], "events": snap["events"], "winner": self.winner},
            "error": self.error,
        }


BATCH_MAX = 20


class Batch:
    """一次出好几道同类题：同一份要求，一道道交给出题流程，同时在出的不超过 parallel 道。
    后开始的题会拿到同批已经写出来的题干，要求换情境、换设问，免得出成一样的题。"""

    def __init__(self, batch_id: str, count: int, brief: QuestionDesignBrief, meta: dict, config: dict, parallel: int) -> None:
        self.id = batch_id
        self.count = count
        self.brief = brief
        self.meta = meta
        self.config = config
        self.parallel = parallel
        session = str((meta or {}).get("session_id") or "")
        self.session_id = session if re.fullmatch(r"[A-Za-z0-9_-]{4,40}", session) else batch_id
        self.job_ids: list[str] = []
        self.status = "running"
        self.stop_requested = False
        self.created_at = time.time()
        self.finished_at: float | None = None
        self.lock = threading.Lock()


class JobManager:
    def __init__(
        self,
        settings: Settings,
        curriculum_csv: Path,
        runtime_dir: Path,
        store: QuestionStore,
        models: ModelRegistry,
        bank=None,
    ) -> None:
        self.settings = settings
        self.curriculum = CsvCurriculumCatalog(curriculum_csv)
        # 各教材版本的知识点目录：知识点编号相同，册、章、节按各版教材编排；出题时按要求里的 edition 选
        self.catalogs = {
            edition: CsvCurriculumCatalog(path) for edition, (_, path) in CURRICULA.items() if Path(path).exists()
        }
        self.verifier = MathVerifier()
        self.model_runs = ModelOperationsRegistry(runtime_dir / "model-runs.sqlite3")
        self.models = models
        self.store = store
        self.bank = bank
        self._jobs: dict[str, Job] = {}
        self._batches: dict[str, Batch] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=settings.max_parallel_jobs,
            thread_name_prefix="mingti-job",
        )

    @property
    def ready(self) -> bool:
        return bool(self.models.options)

    def normalize_config(self, raw: dict | None) -> dict:
        """把界面传来的设置整理成：出题方式、几个方案（命题组）、是否独立验算、每个角色用哪个模型。"""
        raw = raw or {}
        lanes = max(1, min(int(raw.get("lanes") or 1), len(LANE_IDS)))
        main = raw.get("model") or self.models.default_id
        roles_raw = raw.get("roles") or {}
        roles = {role: roles_raw.get(role) or main for role in ROLES}
        known = {option.id for option in self.models.options}
        roles = {role: (model if model in known else self.models.default_id) for role, model in roles.items()}
        mode = raw.get("mode") if raw.get("mode") in ("fast", "careful", "strict", "custom") else None
        return {
            "mode": mode,
            "lanes": lanes,
            "verify": bool(raw.get("verify")),
            "thinking": raw.get("thinking") if raw.get("thinking") in THINK_LEVELS else "high",
            "model": roles["composer"],
            "roles": roles,
        }

    def submit(self, brief: QuestionDesignBrief, meta: dict, config: dict | None) -> Job:
        config = self.normalize_config(config)
        # 4、5 星（较难、压轴）的题至少用高思考强度（可以选极高）：这类题最容易算错、返工，界面上低、中也选不了
        if brief.target_difficulty >= 4 and config["thinking"] in ("low", "medium"):
            config["thinking"] = "high"
        job = Job(f"q_{uuid.uuid4().hex[:12]}", brief, meta, config, self.model_runs)
        with self._lock:
            self._jobs[job.id] = job
        self._executor.submit(self._run, job)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def stop(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None:
            return False
        job.stop_requested = True
        job.halt()
        return True

    # ---------- 批量出题 ----------

    def submit_batch(self, brief: QuestionDesignBrief, meta: dict, config: dict | None, count: int) -> Batch:
        count = max(1, min(int(count), BATCH_MAX))
        config = self.normalize_config(config)
        if brief.target_difficulty >= 4 and config["thinking"] in ("low", "medium"):
            config["thinking"] = "high"
        # 同时出几道：服务设置的并行上限，最多 3 道（再多模型服务容易限流，题也更容易撞车）
        parallel = max(1, min(self.settings.max_parallel_jobs, 3))
        batch = Batch(f"bt_{uuid.uuid4().hex[:12]}", count, brief, meta, config, parallel)
        with self._lock:
            self._batches[batch.id] = batch
        threading.Thread(target=self._run_batch, args=(batch,), name=f"mingti-batch-{batch.id}", daemon=True).start()
        return batch

    def get_batch(self, batch_id: str) -> Batch | None:
        with self._lock:
            return self._batches.get(batch_id)

    def stop_batch(self, batch_id: str) -> bool:
        batch = self.get_batch(batch_id)
        if batch is None:
            return False
        batch.stop_requested = True
        for job_id in list(batch.job_ids):
            self.stop(job_id)
        return True

    def _batch_brief(self, batch: Batch, index: int) -> QuestionDesignBrief:
        """第 index 道（从 0 数）的要求：原要求 + 这是同批第几道、别和同批已经写出的题重复。"""
        stems = []
        for job_id in batch.job_ids:
            job = self.get(job_id)
            stem = re.sub(r"\s+", " ", job.latest_stem()).strip() if job else ""
            if stem:
                stems.append(stem[:110])
        note = (
            f"这是同一批 {batch.count} 道同类题里的第 {index + 1} 道。"
            "和同批的其他题要在情境、设问角度、所给数据和解题入口上明显不同，不能只换数字或换个说法。"
        )
        base = batch.brief.additional_constraints.strip()
        budget = 1850 - len(base) - len(note)
        listed = []
        for stem in reversed(stems):  # 越近出的越要避开：从最近的往前放，放不下为止
            line = f"\n- {stem}"
            if budget - len(line) < 40:
                break
            listed.insert(0, line)
            budget -= len(line)
        if listed:
            note += "\n同批已经出的题（不要和它们重复）：" + "".join(listed)
        constraints = f"{base}\n{note}".strip()[:1880]
        return batch.brief.model_copy(update={"additional_constraints": constraints})

    def _run_batch(self, batch: Batch) -> None:
        try:
            for index in range(batch.count):
                # 等有空位再开下一道：这样它能看到前面几道已经写出来的题干
                while not batch.stop_requested and sum(
                    1 for job_id in batch.job_ids if (job := self.get(job_id)) and job.finished_at is None
                ) >= batch.parallel:
                    time.sleep(0.5)
                if batch.stop_requested:
                    break
                meta = {**batch.meta, "batch_id": batch.id, "batch_index": index + 1, "batch_count": batch.count}
                job = self.submit(self._batch_brief(batch, index), meta, batch.config)
                with batch.lock:
                    batch.job_ids.append(job.id)
                # 等这一道写出题干（最多 45 秒）再开下一道，下一道就能避开它；有空位也不急着同时开
                waited = 0.0
                while (
                    index + 1 < batch.count and not batch.stop_requested and waited < 45
                    and job.finished_at is None and not job.latest_stem()
                ):
                    time.sleep(0.5)
                    waited += 0.5
            while any((job := self.get(job_id)) and job.finished_at is None for job_id in batch.job_ids):
                time.sleep(0.5)
        except Exception:  # noqa: BLE001 - 批量出题的调度出错时记下来，已经开始的题照常跑完
            logger.exception("批量出题 %s 调度出错", batch.id)
        finally:
            with batch.lock:
                batch.status = "cancelled" if batch.stop_requested else "done"
                batch.finished_at = time.time()

    def batch_snapshot(self, batch: Batch) -> dict:
        with batch.lock:
            job_ids = list(batch.job_ids)
            status, finished = batch.status, batch.finished_at
        items = []
        for index in range(batch.count):
            job = self.get(job_ids[index]) if index < len(job_ids) else None
            if job is None:
                state = "cancelled" if status == "cancelled" else "waiting"
                items.append({"index": index + 1, "job_id": None, "status": state, "elapsed": 0, "calls": 0,
                              "tokens": 0, "stage": None, "role": None, "stem": "", "error": None})
            else:
                items.append({"index": index + 1, **job.overview()})
        end = finished or time.time()
        return {
            "id": batch.id,
            "session_id": batch.session_id,
            "status": status,
            "count": batch.count,
            "parallel": batch.parallel,
            "brief": batch.brief.model_dump(mode="json"),
            "meta": batch.meta,
            "config": batch.config,
            "elapsed": round(end - batch.created_at, 1),
            "items": items,
            "totals": {
                "done": sum(1 for item in items if item["status"] == "succeeded"),
                "failed": sum(1 for item in items if item["status"] in ("failed", "cancelled")),
                "calls": sum(item["calls"] for item in items),
                "tokens": sum(item["tokens"] for item in items),
            },
        }

    def _tidy_solution(self, job: Job, result: dict, route) -> dict:
        """按"解析详略"把审核过的完整推导整理成标准答案式的解析；选"详细"时不整理。"""
        detail = (job.meta or {}).get("detail") or "standard"
        if detail not in STYLES:
            return result
        lane = job.winner or "A"
        job.event(lane, "editor", "note", "把解析整理成标准答案的写法")
        tidied = condense(result, detail=detail, call=model_call(route, job.recorder_for(lane, after_win=True)))
        if tidied is result:
            job.event(lane, "editor", "note", "解析没整理成，保留解题员写的完整推导")
        else:
            job.event(
                lane,
                "editor",
                "pass",
                f"解析整理好了：{len(tidied['solution_steps'])} 步；完整推导另外保留",
            )
        return tidied

    def _draw_figure(self, job: Job, result: dict, route) -> dict:
        """题干说了"如图"、要求里写了"配图"或者考的是立体几何时，请配图员画一张图。"""
        if not needs_figure(result, job.brief.model_dump(), job.meta):
            return result
        lane = job.winner or "A"
        job.event(lane, "illustrator", "note", "这道题需要配图，按题干画一张")
        try:
            drawn = illustrate(result, call=model_call(route, job.recorder_for(lane, after_win=True)))
        except Exception:  # noqa: BLE001 - 配图出错不能连累已经审核通过的题
            logger.exception("命题任务 %s 配图出错", job.id)
            drawn = {"spec": None, "svg": "", "problems": ["配图程序出错，详情记在服务日志里"], "redrawn": []}
        if drawn is None:
            job.event(lane, "illustrator", "note", "配图员看过题目，认为这道题不需要配图")
            return result
        if drawn["spec"] is None:
            job.event(
                lane, "illustrator", "note", "这次没画成配图，题目照常交付；可以在题库里点“配图”再画", drawn["problems"]
            )
            return result
        if drawn["redrawn"]:
            job.event(lane, "illustrator", "reject", "第一版配图和题干对不上，重画了一次", drawn["redrawn"])
        if drawn["problems"]:
            job.event(lane, "illustrator", "reject", "配图和题干可能还有出入，用之前看一眼", drawn["problems"])
        else:
            job.event(lane, "illustrator", "pass", "配图画好了，和题干核对一致")
        return {**result, "figure": drawn["spec"], "figure_svg": drawn["svg"]}

    def _run(self, job: Job) -> None:
        with job.lock:
            job.status = "running"
            job.started_at = time.time()
        try:
            routes = {role: self.models.route(model_id) for role, model_id in job.config["roles"].items()}
            team = TeamProvider(
                lanes=job.config["lanes"],
                routes=routes,
                verify=job.config["verify"],
                thinking=job.config["thinking"],
                hooks=job,
            )
            catalog = self.catalogs.get((job.meta or {}).get("edition")) or self.curriculum
            authoring = QuestionAuthoring(curriculum=catalog, provider=team, math_verifier=self.verifier)
            # 需要配图的题，图里的信息也要写进题干：审题、解题、复核都按文字核对，图最后才画
            constraints = f"{job.brief.additional_constraints}\n{FIGURE_RULE}".strip()[:2000]
            draft = authoring.create_draft(job.brief.model_copy(update={"additional_constraints": constraints}))
            result = self._tidy_solution(job, draft.model_dump(mode="json"), routes["solver"])
            result = self._draw_figure(job, result, routes["solver"])
            with job.lock:
                job.result = result
                job.status = "succeeded"
        except QuestionAuthoringError as exc:
            with job.lock:
                job.error = (
                    f"这道题返工太多次，调用了 {len(job.calls)} 次模型还没出成，为了不再久等先停下了。"
                    "可以降一档难度、换个考点，或者把要求写得具体一些再试；过程记录里能看到每次退回的原因"
                    if job.over_budget else str(exc)
                )
                job.status = "cancelled" if job.stop_requested else "failed"
        except Exception as exc:  # noqa: BLE001 - 任何异常都要反馈到界面
            logger.exception("命题任务 %s 异常", job.id)
            with job.lock:
                job.error = f"内部错误：{exc}"
                job.status = "failed"
        finally:
            with job.lock:
                job.finished_at = time.time()
                # 胜出组交卷后，其余组会在下一次调用模型前停下；这里直接把状态记为已停止
                for info in job.lanes.values():
                    if info["status"] in ("waiting", "running"):
                        info["status"] = "stopped"
            record = job.record()
            self.store.save(record)
            # 出好的题自动收进题库
            if self.bank is not None and job.status == "succeeded":
                try:
                    self.bank.add_from_record(record)
                except Exception:  # noqa: BLE001 - 收进题库失败不影响这次出题的结果
                    logger.exception("命题任务 %s 没能收进题库", job.id)
