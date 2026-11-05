"""多智能体并行命题。

一个"命题组"由下面几个角色组成，沿用原系统的闭环流程：
  命题员 → 审题员（不通过就退回命题员）→ 解题员 → 复核员
  （复核发现题面问题退回命题员，发现解析问题退回解题员）
可选的验算员：题干一定稿，就另开线程只看题面独立解题，和解题员同时进行，最后核对两边答案。

多个命题组同时开工，谁先交出全部通过的题就采用谁的，其余组在下一次调用模型前停下。
每个角色都可以指定不同的模型。
"""

from __future__ import annotations

import dataclasses
import json
import re
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from typing import Protocol

from mingti.math_content import MATH_DISPLAY_INSTRUCTIONS
from mingti.models import ModelRoute
from mingti.question_authoring import (
    PROMPT_REVISION,
    DeepSeekQuestionAuthoringProvider,
    QuestionAuthoringContext,
    QuestionAuthoringProviderError,
    QuestionAuthoringStatementPreview,
)
from mingti.question_authoring.providers import (
    THINK_LEVELS,
    WHOLE_QUESTION_ATTEMPTS,
    PipelineSwitches,
    _ContentCandidateRejected,
    _QuestionInvalidAfterSolution,
    _QuestionNeedsHighSchoolRework,
    _SolutionInvalidAfterReview,
    _SolutionSectionFailed,
)
from mingti.question_authoring.schemas import ProviderQuestionDraft
from mingti.tracking import StopRequested

ROLES = ("composer", "reviewer", "solver", "auditor", "verifier")
LANE_IDS = ("A", "B", "C")


def role_of(stage: str) -> str:
    """根据阶段名判断是哪个角色在调用模型。"""
    s = re.sub(r"-retry-\d+$", "", stage)
    if s.startswith(("independent-solve", "answer-equivalence")):
        return "verifier"
    if s.startswith("answer-gate-equivalence"):
        return "reviewer"
    if s.startswith("solution-condense"):
        return "editor"
    if s.startswith("figure-"):
        return "illustrator"
    if (
        s in ("question-review", "solution-regeneration-question-review")
        or re.search(r"-review-check(?:-\d+)?$", s)
    ):
        return "reviewer"
    if s.startswith(("completed-solution-review", "solution-regeneration-verification", "solution-scope-review")):
        return "auditor"
    if s.startswith("solution-"):
        return "solver"
    return "composer"


class TeamHooks(Protocol):
    """命题组向任务汇报进展的接口，由 jobs.Job 实现。"""

    def lane_status(self, lane: str, status: str, note: str = "") -> None: ...

    def lane_progress(self, lane: str, stage: str) -> None: ...

    def lane_statement(self, lane: str, preview: dict) -> None: ...

    def event(
        self, lane: str, role: str, kind: str, text: str, details: list[str] | None = None
    ) -> None: ...

    def recorder_for(self, lane: str): ...

    def should_stop(self) -> bool: ...

    def halt(self) -> None: ...

    def candidate_ready(self) -> None: ...


class LaneProvider(DeepSeekQuestionAuthoringProvider):
    """一个命题组：按阶段把请求交给对应角色的模型，并把审核意见报告出去。"""

    def __init__(self, *, lane: str, routes: dict[str, ModelRoute], recorder, hooks: TeamHooks) -> None:
        # 不调用父类构造函数：父类只建一个客户端，这里每个角色各用自己的模型
        self.lane = lane
        self.routes = routes
        self.recorder = recorder
        self.hooks = hooks
        self.model = routes["composer"].model
        self.client = routes["composer"].client
        self._last_role = "composer"

    def _request_stage(
        self,
        *,
        stage: str,
        instructions: str,
        input_value: dict,
        output_schema: dict,
        max_tokens: int,
        recovery_instruction: str,
    ) -> dict:
        role = role_of(stage)
        route = self.routes[role]
        self._last_role = role
        with self.recorder.track(
            feature="question_authoring",
            provider=route.provider,
            model=route.model,
            prompt_version=f"{PROMPT_REVISION}:{stage}",
        ) as run:
            raw = route.client.request(
                instructions=instructions,
                input_text=json.dumps(input_value, ensure_ascii=False),
                action=f"辅助命题（{stage}）",
                output_schema=output_schema,
                max_tokens=max_tokens,
                recovery_instruction=recovery_instruction,
                thinking=self._thinking_for(stage),
            )
            run.capture_response(raw)
        return self._payload_from(raw)

    def _review_question_core(self, core, *, stage):
        review = super()._review_question_core(core, stage=stage)
        if review.passed:
            details = (
                [f"审题员独立算得：{review.independent_final_answer}"]
                if review.independent_final_answer
                else []
            )
            self.hooks.event(self.lane, "reviewer", "pass", "题干审核通过", details)
        else:
            self.hooks.event(self.lane, "reviewer", "reject", review.summary, review.issues)
        return review

    def _report_answer_dispute(self, text, details):
        self.hooks.event(self.lane, "auditor", "note", text, details)

    def _report_answer_gate(self, planned, reviewer):
        self.hooks.event(
            self.lane, "reviewer", "reject", "审题员算的答案和命题时按构造算的对不上，先改题干",
            [f"命题构造算得：{planned}", f"审题员独立算得：{reviewer}"],
        )

    def _report_solution_rejected(self, rejection):
        if isinstance(rejection, _SolutionSectionFailed):
            # 解题员自己某一段没写成（输出格式不对之类）：内部重写，不算退回
            self.hooks.event(self.lane, "solver", "note", f"{rejection.summary}，重新写一份解析", rejection.issues)
            return
        # 超纲的退回：摘要里已经写了复核员的判断和交给谁（见 _check_solution_scope）
        text = rejection.summary if rejection.scope else f"解析没通过复核，退回解题员：{rejection.summary}"
        self.hooks.event(self.lane, "auditor", "reject", text, rejection.issues)

    def _report_acceptance(self, problems, *, to_composer):
        who = "退回命题员修改" if to_composer else "让解题员重写解析"
        self.hooks.event(self.lane, "check", "reject", f"交付前的检查没通过，{who}", problems)

    def _report_solution_repaired(self, round_no, changed, disputed, objection=""):
        if changed:
            text = f"按复核意见改了 {len(changed)} 处"
        elif objection:
            text = "核算后认为用高中方法做不出来，说明了理由，交回复核员"
        else:
            text = "核算后认为这轮的意见不成立，没有修改"
        details = [*changed[:3], *[f"有异议：{item}" for item in disputed]]
        if objection:
            details.append(f"高中方法做不出来的理由：{objection}")
        self.hooks.event(self.lane, "solver", "note", f"第 {round_no} 轮：{text}", details)

    def _report_candidate(self):
        self.hooks.candidate_ready()

    def _solve_reviewed_statement(self, core, provider_input, **kwargs):
        try:
            plan, steps, conclusion = super()._solve_reviewed_statement(core, provider_input, **kwargs)
        except _QuestionNeedsHighSchoolRework as exc:
            # 题面没有错，只是只能用大学方法解：命题员拿到复核员的判断、改题建议和解题员的理由
            self.hooks.event(self.lane, "auditor", "reject", exc.summary, exc.issues)
            raise
        except _QuestionInvalidAfterSolution as exc:
            role = "auditor" if self._last_role == "auditor" else "solver"
            self.hooks.event(self.lane, role, "reject", f"题面有问题，退回命题员：{exc.summary}", exc.issues)
            raise
        except _SolutionInvalidAfterReview as exc:
            if not exc.reported:  # 写解析时就出了问题，还没到复核那一步
                self._report_solution_rejected(exc)
            raise
        self.hooks.event(
            self.lane, "auditor", "pass", "解析与答案复核通过", [f"答案：{conclusion.final_answer}"]
        )
        return plan, steps, conclusion


_SOLVE_SCHEMA = {
    "type": "object",
    "properties": {
        "final_answer": {"type": "string"},
        "choice_keys": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["final_answer", "choice_keys", "confidence"],
}
_EQUIVALENCE_SCHEMA = {
    "type": "object",
    "properties": {"equivalent": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["equivalent", "reason"],
}


class IndependentVerifier:
    """验算员：只看题面独立求解，接触不到命题组的解析和答案。"""

    def __init__(self, *, lane: str, route: ModelRoute, recorder) -> None:
        self.lane = lane
        self.route = route
        self.recorder = recorder
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"verify-{lane}")
        self._pending: Future | None = None
        self._statement: dict | None = None

    def start(self, statement: dict) -> None:
        """题干每定稿一次就重新验算一次，旧结果作废。"""
        self._statement = statement
        self._pending = self._executor.submit(self._solve, statement)

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _call(self, stage: str, instructions: str, input_value: dict, schema: dict, max_tokens: int) -> dict:
        with self.recorder.track(
            feature="question_authoring",
            provider=self.route.provider,
            model=self.route.model,
            prompt_version=f"{PROMPT_REVISION}:{stage}",
        ) as run:
            raw = self.route.client.request(
                instructions=instructions,
                input_text=json.dumps(input_value, ensure_ascii=False),
                action=f"独立验算（{stage}）",
                output_schema=schema,
                max_tokens=max_tokens,
                recovery_instruction="只修复 JSON 结构。",
            )
            run.capture_response(raw)
        return json.loads(DeepSeekQuestionAuthoringProvider._extract_output_text(raw))

    def _solve(self, statement: dict) -> dict:
        return self._call(
            "independent-solve",
            "你是独立验算员，只能看到题面和选项，看不到任何解析或答案。请在内部独立求解，只输出结果。"
            "选择题在 choice_keys 里写出全部正确选项的字母（单选一个，多选写全）；"
            "填空题和解答题的 choice_keys 为空数组，final_answer 按小问顺序写出各问的最终结果。"
            "没有把握时如实给出 confidence=low。" + MATH_DISPLAY_INSTRUCTIONS,
            {"question_statement": statement},
            _SOLVE_SCHEMA,
            3000,
        )

    def check(self, draft: ProviderQuestionDraft) -> tuple[bool, str, list[str]]:
        """解析完成后核对两边答案，返回（是否一致、说明、细节）。验算本身出错时不拦截。"""
        if self._pending is None:
            return True, "没有拿到验算结果，本次不作判定", []
        try:
            result = self._pending.result(timeout=240)
        except StopRequested:
            raise
        except Exception as exc:  # noqa: BLE001 - 验算失败不应拖垮整组
            return True, f"验算没有完成（{exc}），本次不作判定", []

        mine = str(result.get("final_answer", "")).strip()
        if draft.question_type in ("single_choice", "multiple_choice"):
            theirs = set(re.findall(r"[A-H]", draft.answer_value.upper()))
            keys = {str(k).strip().upper() for k in result.get("choice_keys", []) if str(k).strip()}
            details = [f"验算员：{''.join(sorted(keys)) or '没算出'}", f"命题组：{''.join(sorted(theirs))}"]
            ok = bool(keys) and keys == theirs
            return ok, "选项一致" if ok else "选项不一致", details

        verdict = self._call(
            "answer-equivalence",
            "判断下面两个高中数学答案在数学上是否等价（写法、顺序可以不同），只比较最终结果，不评价过程。",
            {"question_statement": self._statement, "answer_1": draft.final_answer, "answer_2": mine},
            _EQUIVALENCE_SCHEMA,
            800,
        )
        details = [f"验算员：{mine}", f"命题组：{draft.final_answer}"]
        return bool(verdict.get("equivalent")), str(verdict.get("reason", "")), details


class TeamProvider:
    """多个命题组并行，第一个交出全部通过的题的组胜出。"""

    name = "team"

    def __init__(
        self,
        *,
        lanes: int,
        routes: dict[str, ModelRoute],
        verify: bool,
        hooks: TeamHooks,
        thinking: str | bool = "high",
        switches: PipelineSwitches | None = None,
    ) -> None:
        self.lane_ids = LANE_IDS[: max(1, min(lanes, len(LANE_IDS)))]
        self.routes = routes
        self.verify = verify
        # 思考强度：低 / 中 / 高 / 极高（THINK_LEVELS）；对比实验传 True / False，当作高 / 低
        self.thinking = thinking if thinking in THINK_LEVELS else ("high" if thinking is True else "low")
        self.switches = switches  # 只有对比实验会传，正常出题是 None（完整流程）
        self.hooks = hooks
        models = sorted({route.model for route in routes.values()})
        self.model = models[0] if len(models) == 1 else "+".join(models)

    def _lane_provider(self, lane: str) -> LaneProvider:
        provider = LaneProvider(
            lane=lane,
            routes=self.routes,
            recorder=self.hooks.recorder_for(lane),
            hooks=self.hooks,
        )
        provider.think_preference = self.thinking
        if self.switches is not None:
            provider.switches = self.switches
        return provider

    def generate(self, context: QuestionAuthoringContext) -> ProviderQuestionDraft:
        executor = ThreadPoolExecutor(max_workers=len(self.lane_ids), thread_name_prefix="team")
        futures = {executor.submit(self._run_lane, lane, context): lane for lane in self.lane_ids}
        winner: tuple[str, ProviderQuestionDraft] | None = None
        errors: list[str] = []
        try:
            for future in as_completed(futures):
                lane = futures[future]
                try:
                    draft = future.result()
                except StopRequested:
                    continue
                except QuestionAuthoringProviderError as exc:
                    errors.append(f"方案 {lane}：{exc}" if len(self.lane_ids) > 1 else str(exc))
                    continue
                winner = (lane, draft)
                break
        finally:
            if winner is not None:
                # 其余组在下一次调用模型前停下；正在进行的那次调用结束后不再继续
                self.hooks.halt()
                self.hooks.lane_status(winner[0], "won", winner[1].solution_issue or "")
            executor.shutdown(wait=False, cancel_futures=True)
        if winner is None:
            if self.hooks.should_stop():
                raise QuestionAuthoringProviderError("已按要求停止命题")
            raise QuestionAuthoringProviderError("没能得到通过全部检查的题：" + "；".join(errors))
        return winner[1]

    def _run_lane(self, lane: str, context: QuestionAuthoringContext) -> ProviderQuestionDraft:
        provider = self._lane_provider(lane)
        verifier = (
            IndependentVerifier(lane=lane, route=self.routes["verifier"], recorder=self.hooks.recorder_for(lane))
            if self.verify
            else None
        )

        def on_statement(preview: QuestionAuthoringStatementPreview) -> None:
            data = preview.model_dump(mode="json")
            self.hooks.lane_statement(lane, data)
            if verifier is not None:
                verifier.start(data)

        lane_context = dataclasses.replace(
            context,
            on_progress=lambda stage: self.hooks.lane_progress(lane, stage),
            on_statement_approved=on_statement,
        )
        self.hooks.lane_status(lane, "running")
        # 前几版被退回的题面和原因：重出时交给命题员，让他知道上一版错在哪里，而不是蒙着头再出一版
        earlier: list[dict] = []
        try:
            for attempt in range(1, WHOLE_QUESTION_ATTEMPTS + 1):
                try:
                    draft = provider._generate_candidate(lane_context, reauthoring=attempt > 1, earlier=earlier[-2:])
                except _ContentCandidateRejected as exc:
                    note = exc.feedback()
                    if note:
                        earlier.append(note)
                    lead = "这一版不合格，带着退回的原因重新出题" if note else "这一版不合格，重新出题"
                    self.hooks.event(lane, "composer", "reject", f"{lead}。{exc}", (note or {}).get("problems", []))
                    continue
                if verifier is not None and draft.solution_status == "ready":  # 待核对的版本不再为验算重出
                    ok, reason, details = verifier.check(draft)
                    if not ok:
                        self.hooks.event(
                            lane, "verifier", "reject", f"独立验算的答案和解析对不上，重新出题。{reason}", details
                        )
                        earlier.append(
                            {
                                "rejected_stem": draft.stem_plain[:800],
                                "problems": [
                                    "只看题面的独立验算和解析得出的答案对不上，题目的条件可能不足以唯一确定答案，或者数据有误。",
                                    *details,
                                ],
                            }
                        )
                        continue
                    self.hooks.event(lane, "verifier", "pass", "独立验算结果一致", details)
                return draft
            best = provider.best_candidate_draft()
            if best is not None:
                return best
            raise QuestionAuthoringProviderError(f"连续 {WHOLE_QUESTION_ATTEMPTS} 版题都没有通过")
        except StopRequested:
            # 用户点的停止就是停止；到了调用次数或时间上限，手里有写完的解析就交出来，不让用户白等
            best = provider.best_candidate_draft() if self.hooks.over_budget else None
            if best is not None:
                return best
            self.hooks.lane_status(lane, "stopped")
            raise
        except QuestionAuthoringProviderError as exc:
            # 模型接口出错（余额不足、网络断了）也一样：已经写好的不能丢
            best = provider.best_candidate_draft()
            if best is not None:
                return best
            self.hooks.lane_status(lane, "failed", str(exc))
            raise
        finally:
            if verifier is not None:
                verifier.close()

    def regenerate_solution(self, current: ProviderQuestionDraft):
        return self._lane_provider(self.lane_ids[0]).regenerate_solution(current)
