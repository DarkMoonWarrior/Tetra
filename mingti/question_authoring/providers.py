from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from mingti.deepseek import DeepSeekClientError, DeepSeekJsonClient, DeepSeekJsonResponseError
from mingti.math_content import MATH_DISPLAY_INSTRUCTIONS
from mingti.model_operations import ModelRunRecorder, NullModelRunRecorder
from mingti.question_authoring.schemas import (
    ProviderQuestionDraft,
    ProviderQuestionSolution,
    QuestionAuthoringStatementPreview,
    QuestionDesignBrief,
    QuestionDraftCurriculum,
    QuestionDraftOption,
    QuestionType,
)
from mingti.question_authoring.scope import (
    HIGH_SCHOOL_METHODS,
    beyond_curriculum_methods,
    university_only_terms,
)


PROMPT_REVISION = "question-authoring-v13-background-closed-loop"

QuestionAuthoringProgressStage = Literal[
    "composing",
    "reauthoring",
    "reviewing",
    "refining",
    "solving",
    "reviewing_solution",
    "regenerating_solution",
]
QUESTION_REPAIR_ATTEMPTS = 3
POST_SOLUTION_QUESTION_REPAIR_ATTEMPTS = 2
SOLUTION_REGENERATION_ATTEMPTS = 3
WHOLE_QUESTION_ATTEMPTS = 3


_DELIBERATION_PATTERNS = (
    r"让我们(?:重新|再|先|仔细)",
    r"我们(?:可能|需要重新|重新审视|重新检查|忘记|算错|犯了|理解错|错误地)",
    r"我(?:可能|需要重新|重新审视|重新检查|算错|犯了|理解错)",
    r"(?:重新|再次)(?:核对|检查|审题|推导|计算)",
    r"(?:前面|之前).{0,30}(?:算错|不对|错误|有误)",
    r"题(?:面|目|干).{0,40}(?:有问题|有误|错误|缺失|矛盾)",
    r"(?:与题设|与初始条件|初始条件).{0,30}(?:不符|不一致|矛盾)",
    r"无法(?:验证|选出|得到).{0,20}(?:答案|结论)",
    r"作为(?:解题|模型|AI)",
    r"final[_ ]?answer",
    r"(?:let us|let's)\s+(?:reconsider|recalculate|check again|start over)",
    r"(?:we|i)\s+(?:may have|might have|need to)\s+(?:made|reconsider|recalculate)",
)


_POLISHED_SOLUTION_INSTRUCTIONS = (
    "输出必须是可直接交给教师和学生阅读的成稿解析。"
    "先在模型内部完成试算、核对和纠错，返回内容只保留最终采用的连贯推导。"
    "严禁出现第一人称独白、思考过程、尝试失败、回退、自我怀疑、提示词讨论、"
    "字段名或诸如‘让我们重新审视’‘我们可能算错了’的元叙述。"
    f"{HIGH_SCHOOL_METHODS}"
)

# 中档及以上的题，这几步打开模型的思考模式：构造题目、审题员独立求解、规划解法、复核。
# 这几步算错会引发整轮返工（题设前后矛盾、法向量算错），思考几秒很划算；
# 写解析段落、提取答案、清理措辞这类调用多而简单，不开，免得整体太慢。
_THINKING_STAGES = re.compile(
    r"^(?:core|core-high-school-repair|question-review|solution-regeneration-question-review"
    r"|core-question-repair.*|post-solution-question-repair.*|answer-gate-repair.*|.*-review-check(?:-\d+)?"
    r"|solution-plan|solution-regeneration-plan|completed-solution-review|solution-regeneration-verification)$"
)
# 上面这些里，审题员和复核员检查的几步。思考强度选"中"时这几步不开思考，
# 只在出题的几步（构造题目、改题、规划解法）开：出错少、返工少，又比全开快
_CHECK_STAGES = re.compile(
    r"^(?:question-review|solution-regeneration-question-review|.*-review-check(?:-\d+)?"
    r"|completed-solution-review|solution-regeneration-verification)$"
)
# 思考强度，从低到高四档：低 = 都不开，中 = 只开出题的几步，高 = _THINKING_STAGES 里的几步都开，
# 极高 = 同样这几步，但请模型用它最深的一档思考（DeepSeek、智谱、Claude 的 max）
THINK_LEVELS = ("low", "medium", "high", "max")

# 命题员先构造、后成题（参考 PromptCoT 的"概念 → 思路 → 题目"）：题中数值都由构造算出，
# 这样条件自洽、答案唯一；按构造算出的答案只给系统核对，不进题面，也不给解题员看
_CONSTRUCT_FIRST = (
    "先构造、后成题：先选定具体的数学对象和数据（坐标、参数、函数、点的位置、概率模型等），"
    "在 construction 里写清构造和核算过程，并按构造把每一问的结论都算出来写进 planned_answer；"
    "题干里出现的每个数值都必须由构造算出，不能随手写。成题前逐问核对：结论唯一，条件不多余也不互相矛盾，"
    "用高中方法能求出。construction 和 planned_answer 只给系统核对用，不会展示给学生和解题员；"
    "题干和选项里不得出现或暗示答案。"
)

# 解题员规划时先把关键中间结果定下来，各段解析统一使用
_KEY_RESULTS_PLAN = (
    "在 key_results 里列出解题要用到的关键中间结果并核算无误（如建系后各点坐标、平面的法向量、关键方程的解、数列的通项），"
    "后面分段展开时各段统一使用，避免各段各算一套、前后矛盾。"
)
_KEY_RESULTS_SECTION = "坐标、法向量、方程的解等关键结果以 key_results 为准，写出必要的推导，不要另算出不同的结果。"


@dataclass(frozen=True)
class PipelineSwitches:
    """对比实验（eval/ablation.py）用的开关。正常出题全用默认值，也就是现在的完整流程。"""

    stop_after: Literal["compose", "review", "solve"] = "solve"  # 只要命题员 / 加上审题员 / 完整闭环
    thinking: bool = True  # 关键阶段打开思考模式；False 时哪一步都不开（包括 4、5 星的题）
    construct_first: bool = True  # 命题员先构造、后成题，并给出按构造算出的答案
    answer_gate: bool = True  # 写解析前先把构造答案和审题员独立算的答案对一次
    key_results: bool = True  # 解析规划列出关键中间结果，各段共用
    reviewer_cross_check: bool = True  # 解析的答案最后再和审题员的独立答案对一次


class QuestionAuthoringProviderError(RuntimeError):
    pass


class _QuestionInvalidAfterSolution(QuestionAuthoringProviderError):
    """The full solve exposed a statement flaw missed by the pre-solve review."""

    def __init__(self, *, summary: str, issues: list[str]) -> None:
        self.summary = summary
        self.issues = issues
        super().__init__("完整求解复核发现题面仍有问题，必须先完善题面并重新审核。")


class _SolutionInvalidAfterReview(QuestionAuthoringProviderError):
    """The statement is valid, but the generated derivation or answer is not."""

    def __init__(self, *, summary: str, issues: list[str]) -> None:
        self.summary = summary
        self.issues = issues
        super().__init__("解析与题面不一致，必须保留题面并重新生成解析。")


class _ContentCandidateRejected(QuestionAuthoringProviderError):
    """One complete candidate failed quality recovery and must be replaced."""


@dataclass(frozen=True)
class QuestionAuthoringContext:
    brief: QuestionDesignBrief
    question_type: QuestionType
    curriculum: QuestionDraftCurriculum | None
    curriculum_options: tuple[QuestionDraftCurriculum, ...]
    on_progress: Callable[[QuestionAuthoringProgressStage], None] | None = field(
        default=None,
        compare=False,
        repr=False,
    )
    on_statement_approved: Callable[[QuestionAuthoringStatementPreview], None] | None = field(
        default=None,
        compare=False,
        repr=False,
    )

    def report_progress(self, stage: QuestionAuthoringProgressStage) -> None:
        if self.on_progress is not None:
            self.on_progress(stage)

    def report_statement_approved(self, statement: _QuestionStatement) -> None:
        if self.on_statement_approved is not None:
            self.on_statement_approved(
                QuestionAuthoringStatementPreview.model_validate(
                    statement.model_dump(mode="json")
                )
            )


class QuestionAuthoringProvider(Protocol):
    name: str
    model: str

    def generate(self, context: QuestionAuthoringContext) -> ProviderQuestionDraft: ...

    def regenerate_solution(
        self,
        current: ProviderQuestionDraft,
    ) -> ProviderQuestionSolution: ...


class _QuestionStatement(BaseModel):
    """Question wording and options; no answer is fixed before solving."""

    model_config = ConfigDict(extra="forbid")

    question_type: QuestionType
    stem_plain: str = Field(min_length=5, max_length=20000)
    stem_latex: str | None = Field(default=None, max_length=20000)
    options: list[QuestionDraftOption] = Field(default_factory=list, max_length=20)
    estimated_difficulty: int = Field(ge=1, le=5)


class _QuestionReview(BaseModel):
    """Independent solvability review of the answer-free question statement."""

    model_config = ConfigDict(extra="forbid")

    passed: bool
    summary: str = Field(min_length=1, max_length=4000)
    issues: list[str] = Field(default_factory=list, max_length=10)
    independent_answer_value: str | None = Field(default=None, max_length=4000)
    independent_final_answer: str | None = Field(default=None, max_length=4000)


class _SolutionPlanSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order: int = Field(ge=1, le=12)
    title: str = Field(min_length=1, max_length=120)
    goal: str = Field(min_length=1, max_length=500)


class _SolutionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    solution_method: str = Field(min_length=1, max_length=500)
    sections: list[_SolutionPlanSection] = Field(min_length=1, max_length=12)
    authoring_intent: str = Field(min_length=1, max_length=2000)
    curriculum_node_id: str | None = Field(default=None, max_length=160)
    key_results: list[str] = Field(default_factory=list, max_length=16)
    knowledge_points: list[str] = Field(default_factory=list, max_length=20)
    assessed_methods: list[str] = Field(default_factory=list, max_length=20)
    common_mistakes: list[str] = Field(default_factory=list, max_length=20)


class _SolutionRegenerationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    solution_method: str = Field(min_length=1, max_length=500)
    sections: list[_SolutionPlanSection] = Field(min_length=1, max_length=12)
    key_results: list[str] = Field(default_factory=list, max_length=16)


class _SolutionSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order: int = Field(ge=1, le=12)
    steps: list[str] = Field(min_length=1, max_length=100)


class _SolutionCleanup(_SolutionSection):
    mathematically_valid: bool
    issue_summary: str | None = Field(default=None, max_length=500)
    fault_source: Literal["question", "solution"] | None = None


class _SolutionConclusion(BaseModel):
    """Canonical answer extracted only after the complete derivation exists."""

    model_config = ConfigDict(extra="forbid")

    answer_value: str = Field(min_length=1, max_length=4000)
    final_answer: str = Field(min_length=1, max_length=4000)


class _CompletedSolutionReview(BaseModel):
    """Independent post-solution audit of statement, derivation, and answer."""

    model_config = ConfigDict(extra="forbid")

    passed: bool
    question_valid: bool
    solution_valid: bool
    answer_consistent: bool
    summary: str = Field(min_length=1, max_length=4000)
    issues: list[str] = Field(default_factory=list, max_length=10)
    independent_answer_value: str | None = Field(
        default=None,
        min_length=1,
        max_length=4000,
    )
    independent_final_answer: str | None = Field(
        default=None,
        min_length=1,
        max_length=4000,
    )


class _SolutionSubplan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parts: list[_SolutionPlanSection] = Field(min_length=2, max_length=6)


class DeepSeekQuestionAuthoringProvider:
    """Generate a long draft through bounded internal stages behind one provider seam."""

    name = "deepseek"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        timeout_seconds: int = 90,
        recorder: ModelRunRecorder | None = None,
    ) -> None:
        self.model = model
        self.client = DeepSeekJsonClient(
            api_key=api_key,
            model=model,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
        )
        self.recorder = recorder or NullModelRunRecorder()

    def generate(self, context: QuestionAuthoringContext) -> ProviderQuestionDraft:
        last_error: _ContentCandidateRejected | None = None
        for candidate_attempt in range(1, WHOLE_QUESTION_ATTEMPTS + 1):
            try:
                return self._generate_candidate(
                    context,
                    reauthoring=candidate_attempt > 1,
                )
            except _ContentCandidateRejected as exc:
                last_error = exc
                continue
        raise QuestionAuthoringProviderError(
            "多份候选题均未通过闭环审核，需要继续重新命题。"
        ) from last_error

    def _generate_candidate(
        self,
        context: QuestionAuthoringContext,
        *,
        reauthoring: bool,
    ) -> ProviderQuestionDraft:
        provider_input = self._provider_input(context)
        self._think = self._think_level(context.brief.target_difficulty)
        try:
            context.report_progress("reauthoring" if reauthoring else "composing")
            core_payload = self._request_stage(
                stage="core",
                instructions=(
                    "你是面向中国高中数学教师的辅助命题模块。先只生成完整题面与选项。"
                    "教师可能要求综合题、多小问题或长题干；必须完整保留解题所需条件和所有小问，"
                    "不得为了缩短输出删题、合并小问或降低难度。题目必须可独立作答且条件自洽。"
                    "选择题必须给出明确且互不重复的完整选项。"
                    "题目只能使用中国普通高中数学课程范围内的概念、方法与符号；"
                    "不得引入马尔可夫链、随机过程、傅里叶分析、微积分、线性代数等大学内容。"
                    "不得虚构学校、考试、年份、教材页码、题库来源或版权信息。"
                    f"{MATH_DISPLAY_INSTRUCTIONS}"
                    "stem_plain 应是无需 LaTeX 渲染也能校对的题面；stem_latex 是供页面展示的可读版本。"
                    "选择题的 stem_plain 与 stem_latex 只能写公共题干，严禁包含 A/B/C/D 等选项；"
                    "全部选项必须且只能逐项写入 options 数组，option.text 不得重复写选项编号。"
                    "本阶段不展开解析；标准答案在完整解析完成后另行产生。"
                    f"{_CONSTRUCT_FIRST if self.switches.construct_first else ''}"
                ),
                input_value={
                    "design_brief": provider_input["design_brief"],
                    "resolved_question_type": provider_input["resolved_question_type"],
                    "teacher_curriculum_guidance": provider_input[
                        "teacher_curriculum_guidance"
                    ],
                },
                output_schema=self._core_schema(),
                max_tokens=8192,
                recovery_instruction="只修复 JSON 结构并完整返回题面与全部小问；不得返回答案或截短题目。",
            )
            core = self._statement_from(core_payload)
            if self.switches.stop_after == "compose":
                return self._unsolved_draft(core, self.__dict__.get("_planned_answers", {}).get(core.stem_plain, ""))
            core = self._repair_university_scope(core, provider_input)
            context.report_progress("reviewing")
            core, question_review = self._review_and_repair_question(
                core,
                provider_input,
                on_repair=lambda: context.report_progress("refining"),
            )
            core, question_review = self._check_planned_answer(
                core,
                question_review,
                provider_input,
                on_repair=lambda: context.report_progress("refining"),
            )
            context.report_statement_approved(core)
            if self.switches.stop_after == "review":
                return self._unsolved_draft(
                    core, question_review.independent_answer_value or "", question_review.independent_final_answer or ""
                )
            question_repair_count = 0
            solution_regeneration_count = 0
            solve_attempt = 0
            while True:
                solve_attempt += 1
                if solution_regeneration_count == 0:
                    context.report_progress("solving")
                try:
                    plan, steps, conclusion = self._solve_reviewed_statement(
                        core,
                        provider_input,
                        question_review=question_review,
                        stage_suffix=("" if solve_attempt == 1 else f"-retry-{solve_attempt}"),
                        on_review=lambda: context.report_progress("reviewing_solution"),
                    )
                except _QuestionInvalidAfterSolution as exc:
                    if question_repair_count >= POST_SOLUTION_QUESTION_REPAIR_ATTEMPTS:
                        raise _ContentCandidateRejected(
                            "题面经完整求解、完善和重新审核后仍无法成立，无法交付完整题目。"
                        ) from exc
                    question_repair_count += 1
                    context.report_progress("refining")
                    core, question_review = self._repair_question_from_issues(
                        core,
                        provider_input,
                        review_summary=exc.summary,
                        review_issues=exc.issues,
                        stage_prefix=(
                            "post-solution-question-repair"
                            if question_repair_count == 1
                            else f"post-solution-question-repair-{question_repair_count}"
                        ),
                    )
                    core, question_review = self._check_planned_answer(core, question_review, provider_input)
                    context.report_statement_approved(core)
                    solution_regeneration_count = 0
                    continue
                except _SolutionInvalidAfterReview as exc:
                    if solution_regeneration_count >= SOLUTION_REGENERATION_ATTEMPTS:
                        raise _ContentCandidateRejected(
                            "题面已确认成立，但解析经过多轮重新生成仍未通过一致性审核，无法交付半成品。"
                        ) from exc
                    solution_regeneration_count += 1
                    if solution_regeneration_count == 1:
                        context.report_progress("regenerating_solution")
                    continue
                except (DeepSeekJsonResponseError, KeyError, TypeError, ValueError) as exc:
                    # 写解析的某一步返回的内容读不了或缺字段：题面已经审过，留着题面重写解析，和复核退回一起计次
                    if solution_regeneration_count >= SOLUTION_REGENERATION_ATTEMPTS:
                        raise _ContentCandidateRejected("解析几次返回的内容都不完整或格式不对") from exc
                    solution_regeneration_count += 1
                    if solution_regeneration_count == 1:
                        context.report_progress("regenerating_solution")
                    continue

                return ProviderQuestionDraft(
                    question_type=core.question_type,
                    stem_plain=core.stem_plain,
                    stem_latex=core.stem_latex,
                    options=core.options,
                    answer_value=conclusion.answer_value,
                    solution_method=plan.solution_method,
                    solution_steps=steps,
                    solution_status="ready",
                    solution_issue=None,
                    final_answer=conclusion.final_answer,
                    authoring_intent=plan.authoring_intent,
                    estimated_difficulty=core.estimated_difficulty,
                    curriculum_node_id=(
                        plan.curriculum_node_id
                        if plan.curriculum_node_id
                        else (context.curriculum.node_id if context.curriculum else None)
                    ),
                    knowledge_points=plan.knowledge_points,
                    assessed_methods=plan.assessed_methods,
                    common_mistakes=plan.common_mistakes,
                )
        except QuestionAuthoringProviderError:
            raise
        except (DeepSeekJsonResponseError, KeyError, TypeError, ValueError) as exc:
            # 某一步返回的内容读不了或缺字段（关掉思考时多见）：这一版作废、重新出题，由调用方计入整题重试次数。
            # 以前这里直接让整个任务失败。网络、余额这类接口错误不是这一版的问题，仍然直接报出去（下一条）
            raise _ContentCandidateRejected("模型返回的内容不完整或格式不对") from exc
        except DeepSeekClientError as exc:
            raise QuestionAuthoringProviderError(str(exc)) from exc

    def _solve_reviewed_statement(
        self,
        core: _QuestionStatement,
        provider_input: dict,
        *,
        question_review: _QuestionReview,
        stage_suffix: str,
        on_review: Callable[[], None] | None = None,
    ) -> tuple[_SolutionPlan, list[str], _SolutionConclusion]:
        plan = _SolutionPlan.model_validate(
            self._request_stage(
                stage=f"solution-plan{stage_suffix}",
                instructions=(
                    "根据已通过题面自审核的题干与选项，规划一份适合教师审核和讲解的完整解析。"
                    "不得接收或猜测命题阶段的答案，必须从题面独立求解。"
                    "先把长解析拆成有明确目标的若干段，本阶段不要展开详细计算。"
                    "多小问题必须逐问覆盖；复杂推导应继续拆段，不能因篇幅省略关键论证。"
                    "教师可以只描述想要的题，不得因缺少知识点、考查方法或易错点而拒绝。"
                    "生成题目后，再从 allowed_curriculum_nodes 中选择最贴切的 curriculum_node_id；"
                    "只能返回列表里某一行开头的编号（如 kp_r1_5_6_03），无法可靠映射时返回 null。"
                    "knowledge_points、assessed_methods 与 common_mistakes 必须依据最终题目归纳，"
                    "不要把模型推断冒充教师确认。"
                    f"{_KEY_RESULTS_PLAN if self.switches.key_results else ''}"
                    f"{HIGH_SCHOOL_METHODS}"
                ),
                input_value={
                    "design_brief": provider_input["design_brief"],
                    "question_statement": core.model_dump(mode="json"),
                    "allowed_curriculum_nodes": provider_input["allowed_curriculum_nodes"],
                },
                output_schema=self._solution_plan_schema(),
                max_tokens=3500,
                recovery_instruction="只修复 JSON 结构；保留覆盖全部小问所需的解析分段。",
            )
        )
        self._validate_plan_orders(plan)
        sections = self._expand_sections(core, plan, stage_suffix=stage_suffix)
        steps = [step for section in sections for step in section.steps if step.strip()]  # 模型偶尔多写一个空步骤，丢掉即可
        self._check_solution_scope(plan, steps)
        conclusion = self._derive_solution_conclusion(
            core,
            steps,
            stage=f"solution-conclusion{stage_suffix}",
        )
        if on_review is not None:
            on_review()
        self._verify_completed_solution(
            core,
            steps,
            conclusion,
            question_review=question_review,
            stage=f"completed-solution-review{stage_suffix}",
        )
        return plan, steps, conclusion

    def regenerate_solution(
        self,
        current: ProviderQuestionDraft,
    ) -> ProviderQuestionSolution:
        """Re-solve an immutable statement and derive a fresh canonical answer."""

        core = _QuestionStatement(
            question_type=current.question_type,
            stem_plain=current.stem_plain,
            stem_latex=current.stem_latex,
            options=current.options,
            estimated_difficulty=current.estimated_difficulty,
        )
        self._think = self._think_level(current.estimated_difficulty)
        try:
            question_review = self._review_question_core(
                core,
                stage="solution-regeneration-question-review",
            )
            if not question_review.passed:
                raise QuestionAuthoringProviderError(
                    "当前题面未通过重新生成解析前的独立审核，未生成新的标准答案。"
                )
            for attempt in range(SOLUTION_REGENERATION_ATTEMPTS + 1):
                suffix = "" if attempt == 0 else f"-retry-{attempt}"
                plan = _SolutionRegenerationPlan.model_validate(
                    self._request_stage(
                        stage=f"solution-regeneration-plan{suffix}",
                        instructions=(
                            "为已经锁定的题面和选项重新规划一份全新的解析。"
                            "不得接收旧答案，也不得修改或补写题面条件；必须从题面独立求解。"
                            "多小问必须逐问覆盖。"
                            "本阶段只返回新的解法概述和分段提纲，不展开计算。"
                            "在 key_results 里列出解题要用到的关键中间结果并核算无误（如建系后各点坐标、平面的法向量、关键方程的解、数列的通项），后面分段展开时各段统一使用，避免各段各算一套、前后矛盾。"
                            f"{_POLISHED_SOLUTION_INSTRUCTIONS}"
                        ),
                        input_value={"question_statement": core.model_dump(mode="json")},
                        output_schema=self._solution_regeneration_plan_schema(),
                        max_tokens=2500,
                        recovery_instruction="只修复 JSON 结构；保留覆盖全部小问所需的解析分段。",
                    )
                )
                self._validate_plan_orders(plan)
                try:
                    sections = self._expand_sections(core, plan, stage_suffix=suffix)
                    steps = [step for section in sections for step in section.steps if step.strip()]  # 模型偶尔多写一个空步骤，丢掉即可
                    self._check_solution_scope(plan, steps)
                    conclusion = self._derive_solution_conclusion(
                        core,
                        steps,
                        stage=f"solution-regeneration-conclusion{suffix}",
                    )
                    self._verify_completed_solution(
                        core,
                        steps,
                        conclusion,
                        question_review=question_review,
                        stage=f"solution-regeneration-verification{suffix}",
                    )
                except _QuestionInvalidAfterSolution as exc:
                    raise QuestionAuthoringProviderError(
                        "当前题面本身未通过独立审核；重新生成解析不能修改题面。"
                    ) from exc
                except _SolutionInvalidAfterReview:
                    if attempt >= SOLUTION_REGENERATION_ATTEMPTS:
                        raise QuestionAuthoringProviderError(
                            "解析经过多轮重新生成仍未通过一致性审核，未返回半成品。"
                        )
                    continue
                return ProviderQuestionSolution(
                    solution_method=plan.solution_method,
                    solution_steps=steps,
                    answer_value=conclusion.answer_value,
                    final_answer=conclusion.final_answer,
                )
            raise QuestionAuthoringProviderError("未能生成通过审核的完整解析。")
        except QuestionAuthoringProviderError:
            raise
        except DeepSeekClientError as exc:
            raise QuestionAuthoringProviderError(str(exc)) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise QuestionAuthoringProviderError(
                f"DeepSeek 返回的重生成解析不符合结构：{exc}"
            ) from exc

    def _expand_sections(
        self,
        core: _QuestionStatement,
        plan: _SolutionPlan | _SolutionRegenerationPlan,
        *,
        stage_suffix: str = "",
    ) -> list[_SolutionSection]:
        workers = min(3, len(plan.sections))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            sections = list(
                executor.map(
                    lambda section: self._expand_solution_section(
                        core,
                        plan,
                        section,
                        stage_suffix=stage_suffix,
                    ),
                    plan.sections,
                )
            )
        sections.sort(key=lambda item: item.order)
        expected = list(range(1, len(plan.sections) + 1))
        if [item.order for item in sections] != expected:
            raise QuestionAuthoringProviderError("分段解析序号不完整，未组装为草稿")
        return sections

    def _expand_solution_section(
        self,
        core: _QuestionStatement,
        plan: _SolutionPlan | _SolutionRegenerationPlan,
        section: _SolutionPlanSection,
        *,
        stage_suffix: str = "",
    ) -> _SolutionSection:
        input_value = {
            "question_statement": core.model_dump(mode="json"),
            "solution_method": plan.solution_method,
            "solution_outline": [item.model_dump(mode="json") for item in plan.sections],
            "key_results": plan.key_results,
            "section": section.model_dump(mode="json"),
        }
        instructions = (
            "只展开解析提纲中指定的当前一段，给出教师和学生都能读懂的完整推导步骤。"
            "必须只依据题面独立推导，不得接收预设答案、改题或添加未给条件。"
            f"{_KEY_RESULTS_SECTION if self.switches.key_results else ''}"
            "每一步都写清数学依据；涉及多个小问时保留当前小问的层次。"
            "不要重复整道题，也不要重复其他解析段。"
            f"{_POLISHED_SOLUTION_INSTRUCTIONS}"
            f"{MATH_DISPLAY_INSTRUCTIONS}"
            "不要为了减少字数省略关键等价变形、分类讨论、取值范围或结论核对。"
        )
        try:
            return self._request_solution_section(
                section=section,
                stage=f"solution-section-{section.order}{stage_suffix}",
                input_value=input_value,
                instructions=instructions,
            )
        except (_QuestionInvalidAfterSolution, _SolutionInvalidAfterReview):
            raise
        except QuestionAuthoringProviderError as original_error:
            return self._subdivide_and_expand(
                core=core,
                plan=plan,
                section=section,
                original_error=original_error,
            )

    def _request_solution_section(
        self,
        *,
        section: _SolutionPlanSection,
        stage: str,
        input_value: dict,
        instructions: str,
    ) -> _SolutionSection:
        last_error: DeepSeekClientError | ValueError | None = None
        for attempt in range(2):
            try:
                payload = self._request_stage(
                    stage=stage,
                    instructions=instructions,
                    input_value=input_value,
                    output_schema=self._solution_section_schema(),
                    max_tokens=8192,
                    recovery_instruction="只修复当前解析段的 JSON；完整保留该段推导，不要返回其他段。",
                )
                parsed = _SolutionSection.model_validate(payload)
                if parsed.order != section.order:
                    raise ValueError(
                        f"解析段序号应为 {section.order}，实际为 {parsed.order}"
                    )
                if self._contains_deliberation(parsed.steps):
                    parsed = self._cleanup_solution_section(
                        section=section,
                        input_value=input_value,
                        contaminated=parsed,
                    )
                return parsed
            except (DeepSeekClientError, ValueError) as exc:
                last_error = exc
                if attempt == 0:
                    continue
        raise QuestionAuthoringProviderError(
            f"第 {section.order} 段解析生成失败：{last_error}"
        )

    def _cleanup_solution_section(
        self,
        *,
        section: _SolutionPlanSection,
        input_value: dict,
        contaminated: _SolutionSection,
    ) -> _SolutionSection:
        payload = self._request_stage(
            stage=f"solution-section-{section.order}-cleanup",
            instructions=(
                "把当前解析草稿改写为可直接展示的正式解析。删除所有试算、回退、自我纠错、"
                "第一人称独白、提示词或字段讨论，只保留最终采用的严谨推导；不要缩短必要步骤。"
                "题面已经锁定，但答案尚未产生。若你确认题面本身或当前推导在数学上不成立，"
                "将 mathematically_valid 设为 false，用 fault_source 明确区分 question 或 solution，"
                "并用 issue_summary 简洁说明问题；只有题面条件本身错误才标记 question，"
                "若题面成立但当前推导错误则必须标记 solution。"
                "此时 steps 仍返回一条简短的教师复核提示，不得伪造证明。"
                f"{MATH_DISPLAY_INSTRUCTIONS}"
            ),
            input_value={
                **input_value,
                "contaminated_solution_steps": contaminated.steps,
            },
            output_schema=self._solution_cleanup_schema(),
            max_tokens=8192,
            recovery_instruction="只修复清理后解析的 JSON，不得恢复思考过程。",
        )
        cleaned = _SolutionCleanup.model_validate(payload)
        if cleaned.order != section.order:
            raise ValueError(
                f"清理后解析段序号应为 {section.order}，实际为 {cleaned.order}"
            )
        if not cleaned.mathematically_valid:
            issue = (cleaned.issue_summary or "题面或当前解析在数学上不成立").strip()
            if self._contains_deliberation([issue]):
                issue = "题面或当前解析存在数学冲突。"
            if cleaned.fault_source == "question":
                raise _QuestionInvalidAfterSolution(
                    summary=f"第 {section.order} 段解析发现题面问题。",
                    issues=[issue],
                )
            raise _SolutionInvalidAfterReview(
                summary=f"第 {section.order} 段解析生成错误。",
                issues=[issue],
            )
        if self._contains_deliberation(cleaned.steps):
            raise ValueError("清理后的解析仍包含模型思考或自我纠错叙述")
        return _SolutionSection(order=cleaned.order, steps=cleaned.steps)

    @staticmethod
    def _contains_deliberation(steps: list[str]) -> bool:
        combined = "\n".join(steps)
        return any(
            re.search(pattern, combined, flags=re.IGNORECASE | re.DOTALL)
            for pattern in _DELIBERATION_PATTERNS
        )

    def _subdivide_and_expand(
        self,
        *,
        core: _QuestionStatement,
        plan: _SolutionPlan | _SolutionRegenerationPlan,
        section: _SolutionPlanSection,
        original_error: QuestionAuthoringProviderError,
    ) -> _SolutionSection:
        try:
            subplan = _SolutionSubplan.model_validate(
                self._request_stage(
                    stage=f"solution-section-{section.order}-subplan",
                    instructions=(
                        "当前解析段对于一次模型输出过长。只把该段拆成 2 到 6 个连续、互不重叠的原子子段。"
                        "子段合并后必须完整实现原段目标，不得删减推导或改变题面。"
                        "本阶段只返回子段标题和目标，不展开计算。"
                    ),
                    input_value={
                        "question_statement": core.model_dump(mode="json"),
                        "solution_method": plan.solution_method,
                        "parent_section": section.model_dump(mode="json"),
                    },
                    output_schema=self._solution_subplan_schema(),
                    max_tokens=1800,
                    recovery_instruction="只修复子段提纲 JSON；保留全部连续子段。",
                )
            )
            orders = [part.order for part in subplan.parts]
            if orders != list(range(1, len(orders) + 1)):
                raise ValueError("解析子段序号必须从 1 连续递增且不可重复")

            def expand_part(part: _SolutionPlanSection) -> _SolutionSection:
                return self._request_solution_section(
                    section=part,
                    stage=f"solution-section-{section.order}-part-{part.order}",
                    input_value={
                        "question_statement": core.model_dump(mode="json"),
                        "solution_method": plan.solution_method,
                        "solution_outline": [
                            item.model_dump(mode="json") for item in plan.sections
                        ],
                        "key_results": plan.key_results,
                        "parent_section": section.model_dump(mode="json"),
                        "section": part.model_dump(mode="json"),
                    },
                    instructions=(
                        "只展开当前原子子段，完整写出教师可读的数学推导；不得重复父段或其他子段。"
                        "必须只使用锁定题面独立推导，不得改题或省略当前目标所需的依据。"
                        f"{_POLISHED_SOLUTION_INSTRUCTIONS}"
                        f"{MATH_DISPLAY_INSTRUCTIONS}"
                    ),
                )

            with ThreadPoolExecutor(max_workers=min(3, len(subplan.parts))) as executor:
                expanded = list(executor.map(expand_part, subplan.parts))
            expanded.sort(key=lambda item: item.order)
            steps = [step for part in expanded for step in part.steps]
            return _SolutionSection(order=section.order, steps=steps)
        except QuestionAuthoringProviderError:
            raise
        except (DeepSeekClientError, KeyError, TypeError, ValueError) as exc:
            raise QuestionAuthoringProviderError(
                f"第 {section.order} 段拆分恢复失败（原错误：{original_error}）：{exc}"
            ) from exc

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
        with self.recorder.track(
            feature="question_authoring",
            provider=self.name,
            model=self.model,
            prompt_version=f"{PROMPT_REVISION}:{stage}",
        ) as run:
            raw = self.client.request(
                instructions=instructions,
                input_text=json.dumps(input_value, ensure_ascii=False),
                action=f"辅助命题（{stage}）",
                output_schema=output_schema,
                max_tokens=max_tokens,
                recovery_instruction=recovery_instruction,
                thinking=self._thinking_for(stage),
            )
            run.capture_response(raw)
        content = self._extract_output_text(raw)
        payload = json.loads(content)
        if not isinstance(payload, dict):
            raise ValueError("顶层结果必须是 JSON 对象")
        return payload

    def _repair_university_scope(
        self,
        core: _QuestionStatement,
        provider_input: dict,
    ) -> _QuestionStatement:
        stem_parts = (core.stem_plain, core.stem_latex or "", *(option.text for option in core.options))
        violations = university_only_terms(*stem_parts) + beyond_curriculum_methods(*stem_parts)
        if not violations:
            return core
        repaired_payload = self._request_stage(
            stage="core-high-school-repair",
            instructions=(
                "上一版题目误用了大学数学内容。请在不降低教师指定难度、题型和综合性的前提下，"
                "把题目完整重写为中国普通高中数学课程范围内可作答的题目。"
                "可以使用高中数列、概率、函数、几何等方法，但不得只改名或隐藏大学概念。"
                "选择题的题干不得包含选项；所有选项只写入 options 数组，且必须完整给出。"
                f"{MATH_DISPLAY_INSTRUCTIONS}"
                f"{_CONSTRUCT_FIRST if self.switches.construct_first else ''}"
            ),
            input_value={
                "design_brief": provider_input["design_brief"],
                "resolved_question_type": provider_input["resolved_question_type"],
                "rejected_university_terms": violations,
                "rejected_question_statement": core.model_dump(mode="json"),
            },
            output_schema=self._core_schema(),
            max_tokens=8192,
            recovery_instruction="只修复 JSON，并完整返回高中范围内的题面和全部选项；不得返回答案。",
        )
        repaired = self._statement_from(repaired_payload)
        repaired_parts = (repaired.stem_plain, repaired.stem_latex or "", *(option.text for option in repaired.options))
        remaining = university_only_terms(*repaired_parts) + beyond_curriculum_methods(*repaired_parts)
        if remaining:
            raise QuestionAuthoringProviderError(
                "模型两次返回了超出高中数学范围的内容：" + "、".join(remaining)
            )
        return repaired

    # 用户选的思考强度（THINK_LEVELS 里的一档）；None 表示没选（直接调用 provider 时），按难度定
    think_preference: str | None = None
    # 对比实验关掉的机制（见 PipelineSwitches）；正常出题不改，用默认值
    switches: PipelineSwitches = PipelineSwitches()

    def _think_level(self, difficulty: int) -> str:
        """按用户选的强度；没选时中档及以上用高、以下用低。4、5 星的题至少用高（可以选极高）。"""
        if self.think_preference in THINK_LEVELS:
            level = self.think_preference
        else:
            level = "high" if difficulty >= 3 else "low"
        if difficulty >= 4 and level in ("low", "medium"):
            return "high"
        return level

    def _thinking_for(self, stage: str) -> bool | str:
        """这次调用的思考：False 不开；True 打开（模型默认的强度）；"max" 打开并用最深的一档。
        高、极高开 _THINKING_STAGES 里的几步，中只开其中出题的几步，低都不开。"""
        level = getattr(self, "_think", "low")
        stage = re.sub(r"-retry-\d+$", "", stage)
        if not self.switches.thinking or level == "low" or not _THINKING_STAGES.match(stage):
            return False
        if level == "medium":
            return not _CHECK_STAGES.match(stage)
        return "max" if level == "max" else True

    def _statement_from(self, payload: dict) -> _QuestionStatement:
        """命题员的输出 → 题面。构造过程和按构造算出的答案只留给系统核对，不进题面。"""
        planned = str(payload.pop("planned_answer", "") or "").strip()
        payload.pop("construction", None)
        statement = _QuestionStatement.model_validate(payload)
        if planned:
            self.__dict__.setdefault("_planned_answers", {})[statement.stem_plain] = planned
        return statement

    @staticmethod
    def _unsolved_draft(core: _QuestionStatement, answer_value: str, final_answer: str = "") -> ProviderQuestionDraft:
        """对比实验里只跑到命题或审题为止的配置：不写解析，交出题面和当时手里的答案。"""
        answer_value = answer_value.strip() or "（没有给出答案）"
        note = "对比实验：这一组配置不写解析"
        return ProviderQuestionDraft(
            question_type=core.question_type,
            stem_plain=core.stem_plain,
            stem_latex=core.stem_latex,
            options=core.options,
            answer_value=answer_value,
            final_answer=final_answer.strip() or answer_value,
            solution_method=note,
            solution_steps=[note],
            solution_status="needs_teacher_verification",
            solution_issue=note,
            authoring_intent=note,
            estimated_difficulty=core.estimated_difficulty,
        )

    def _report_answer_gate(self, planned: str, reviewer: str) -> None:
        """命题构造和审题员的答案对不上；多智能体版本把它记进过程记录。"""

    def _check_planned_answer(
        self,
        core: _QuestionStatement,
        question_review: _QuestionReview,
        provider_input: dict,
        *,
        on_repair: Callable[[], None] | None = None,
    ) -> tuple[_QuestionStatement, _QuestionReview]:
        """写长解析之前，先把命题员按构造算出的答案和审题员独立算的对一次。
        对不上多半是题设有问题（数值不是算出来的、条件不足或互相矛盾），这时改题干只要两三次很短的调用，
        比写完十几段解析才发现、整轮推倒重来便宜得多。只改一轮；改不好就按原题往下走，交给完整求解和复核。"""
        planned = self.__dict__.get("_planned_answers", {}).get(core.stem_plain, "")
        reviewer = self._independent_answer(core, question_review, final_first=True)
        if not self.switches.answer_gate or not planned or not reviewer or self._planned_agrees(core, planned, reviewer):
            return core, question_review
        self._report_answer_gate(planned, reviewer)
        if on_repair is not None:
            on_repair()
        try:
            return self._repair_question_from_issues(
                core,
                provider_input,
                review_summary="命题时按构造算出的答案和审题员独立求解的不一致，题设可能有问题",
                review_issues=[
                    f"命题构造算得：{planned}",
                    f"审题员独立算得：{reviewer}",
                    "逐问核对：题中数值是否都由构造算出，条件能否唯一确定结论，有没有多余或互相矛盾的条件；"
                    "按核对结果修改题面，使题设与答案一致。",
                ],
                stage_prefix="answer-gate-repair",
                attempts=1,
            )
        except (_ContentCandidateRejected, DeepSeekJsonResponseError, KeyError, TypeError, ValueError):
            return core, question_review  # 包括改题时模型返回的内容读不了或缺字段

    def _planned_agrees(self, core: _QuestionStatement, planned: str, reviewer: str) -> bool:
        """命题构造的答案和审题员的答案是否一致。选择题、填空题先按规则比；比不出来再请模型逐问比较。"""
        if self._answers_agree(core, planned, reviewer) and core.question_type != "open_response":
            return True
        if core.question_type in {"single_choice", "multiple_choice"}:
            return False
        payload = self._request_stage(
            stage="answer-gate-equivalence",
            instructions=(
                "下面是同一道高中数学题的两份答案：一份来自命题时的构造，一份来自独立求解。"
                "逐问比较最终结论（数值、表达式、取值范围、存在与否）；证明题的小问只要两份都认为结论成立即算一致。"
                "忽略叙述文字和写法差异（小数与分数、是否带 $、是否化简）。全部小问一致才算一致。"
            ),
            input_value={
                "question_statement": core.model_dump(mode="json"),
                "answer_1": planned,
                "answer_2": reviewer,
            },
            output_schema=self._schema(
                "private_question_authoring_answer_gate",
                {"equivalent": {"type": "boolean"}, "reason": {"type": "string", "maxLength": 400}},
            ),
            max_tokens=500,
            recovery_instruction="只修复 JSON；equivalent 只能是 true 或 false。",
        )
        return payload.get("equivalent") is True

    def _review_and_repair_question(
        self,
        core: _QuestionStatement,
        provider_input: dict,
        *,
        on_repair: Callable[[], None] | None = None,
    ) -> tuple[_QuestionStatement, _QuestionReview]:
        review = self._review_question_core(core, stage="question-review")
        if review.passed:
            return core, review
        if on_repair is not None:
            on_repair()
        return self._repair_question_from_issues(
            core,
            provider_input,
            review_summary=review.summary,
            review_issues=review.issues,
            stage_prefix="core-question-repair",
        )

    def _repair_question_from_issues(
        self,
        rejected: _QuestionStatement,
        provider_input: dict,
        *,
        review_summary: str,
        review_issues: list[str],
        stage_prefix: str,
        attempts: int = QUESTION_REPAIR_ATTEMPTS,
    ) -> tuple[_QuestionStatement, _QuestionReview]:
        review = _QuestionReview(
            passed=False,
            summary=review_summary,
            issues=review_issues,
        )
        for attempt in range(1, attempts + 1):
            suffix = "" if attempt == 1 else f"-{attempt}"
            repaired_payload = self._request_stage(
                stage=f"{stage_prefix}{suffix}",
                instructions=(
                    f"这是第 {attempt}/{attempts} 次题面完善。上一版题面在独立审核或完整求解复核中发现问题。"
                    "请依据审核问题完整重写题面和选项，不要解释旧题，也不要进入解析或返回答案。"
                    "必须重新独立检查所有数值、取值范围和选择题选项，逐项消除 review_issues，"
                    "不能只改文字。修订后必须条件相容、存在高中范围内"
                    "的有效对象、全部小问可作答；选择题还必须有完整且互不"
                    "重复的选项，单选恰有一个正确选项，多选答案集合明确。不得通过删除教师要求、"
                    "降低难度或改成另一题型来绕过问题。题干与选项继续严格分离。"
                    f"{MATH_DISPLAY_INSTRUCTIONS}"
                    f"{_CONSTRUCT_FIRST if self.switches.construct_first else ''}"
                ),
                input_value={
                    "design_brief": provider_input["design_brief"],
                    "resolved_question_type": provider_input["resolved_question_type"],
                    "rejected_question_statement": rejected.model_dump(mode="json"),
                    "review_summary": review.summary,
                    "review_issues": review.issues,
                },
                output_schema=self._core_schema(),
                max_tokens=8192,
                recovery_instruction="只修复 JSON；完整返回复核后的题面和全部选项，不得返回答案。",
            )
            repaired = self._statement_from(repaired_payload)
            repaired = self._repair_university_scope(repaired, provider_input)
            review = self._review_question_core(
                repaired,
                stage=f"{stage_prefix}-review-check{suffix}",
            )
            if review.passed:
                return repaired, review
            rejected = repaired

        raise _ContentCandidateRejected(
            "题面经过多轮完善和重新审核后仍未通过，未建立命题草稿。请重新生成题目。"
        )

    def _review_question_core(
        self,
        core: _QuestionStatement,
        *,
        stage: str,
    ) -> _QuestionReview:
        payload = self._request_stage(
            stage=stage,
            instructions=(
                "你是独立于命题步骤的高中数学题面审核器。你只能看到题面和选项，看不到任何预设答案。"
                "在本阶段严禁生成展示用解析。请从零开始在内部逐项代入、求解或构造反例，并检查："
                "(1) 几何、代数、概率与取值范围条件是否相容且对象真实存在；"
                "(2) 每个小问是否信息充分、表述无歧义且能在高中范围内作答；"
                "(3) 是否能独立推出明确结论；"
                "(4) 选择题是否包含完整选项，并符合单选唯一正确或多选答案集合；"
                "(5) 是否暗含大学专属概念。"
                "任何一项无法确认都必须 passed=false；不得因为后续可以提示教师复核而放行。"
                "审核通过时必须同时返回独立求得的 answer_value 和 final_answer，供后续交叉核验；"
                "这不是最终标准答案，后续解析仍须重新独立求解。输出不得泄露试算过程或思维链。"
            ),
            input_value={"question_statement": core.model_dump(mode="json")},
            output_schema=self._question_review_schema(),
            max_tokens=1800,
            recovery_instruction="只修复审核 JSON；不得生成解析或修改题面。",
        )
        review = _QuestionReview.model_validate(payload)
        if review.passed and review.issues:
            review = review.model_copy(update={"passed": False})
        if review.passed and not (
            review.independent_answer_value and review.independent_final_answer
        ):
            raise ValueError("题面审核通过时必须提供独立求得的答案用于交叉核验")
        if not review.passed and not review.issues:
            raise ValueError("题面审核未通过时必须给出至少一个明确问题")
        return review

    def _derive_solution_conclusion(
        self,
        core: _QuestionStatement,
        steps: list[str],
        *,
        stage: str = "solution-conclusion",
    ) -> _SolutionConclusion:
        payload = self._request_stage(
            stage=stage,
            instructions=(
                "完整解析已经生成。只根据题面、选项和这份最终采用的解析步骤提取标准答案。"
                "不得参考命题阶段的任何答案，因为命题阶段没有锁定答案。"
                "answer_value 用于答题卡：单选只写一个选项编号，多选用英文逗号分隔；"
                "填空题和解答题的 answer_value 必须与 final_answer 完全相同。"
                "final_answer 写可直接展示的简洁标准答案，不重复解析过程；"
                "填空题只写每个空要填的结果，多个空用“；”隔开（如“$0.12$；$0.02$”），不要写成句子。"
                f"{MATH_DISPLAY_INSTRUCTIONS}"
            ),
            input_value={
                "question_statement": core.model_dump(mode="json"),
                "completed_solution_steps": steps,
            },
            output_schema=self._solution_conclusion_schema(),
            max_tokens=2200,
            recovery_instruction="只修复标准答案 JSON；答案必须来自已经完成的解析。",
        )
        conclusion = _SolutionConclusion.model_validate(payload)
        if core.question_type in {"fill_blank", "open_response"}:
            conclusion = conclusion.model_copy(
                update={"answer_value": conclusion.final_answer.strip()}
            )
        try:
            self._validate_conclusion_shape(core, conclusion)
        except QuestionAuthoringProviderError as exc:
            raise _SolutionInvalidAfterReview(
                summary="解析结论不符合当前题型的标准答案结构。",
                issues=[str(exc)],
            ) from exc
        return conclusion

    def _verify_completed_solution(
        self,
        core: _QuestionStatement,
        steps: list[str],
        conclusion: _SolutionConclusion,
        *,
        question_review: _QuestionReview,
        stage: str = "completed-solution-review",
    ) -> None:
        payload = self._request_stage(
            stage=stage,
            instructions=(
                "你是最终独立复核器。请从题面重新求解，不得沿用先前审核答案或当前标准答案，"
                "并逐项检查：题面条件与数学对象是否成立、全部小问是否可解、解析每步是否正确且完整、"
                "解析推出的答案是否与标准答案一致、选择题是否恰有规定数量的正确选项。"
                "只有这些项目全部成立时 passed 才能为 true；任一项目无法确认都必须失败。"
                "同时返回你独立求得的 answer_value 与 final_answer 供服务端交叉核验。"
                "若复核失败且确实无法得到独立答案，这两个字段返回 null，不得省略其他判责字段。"
                "如果完整求解暴露出题面条件不可能、结论不唯一或问题无解，必须明确设置 question_valid=false，"
                "使系统返回完善题面步骤，而不是把它伪装成解析问题。若题面本身成立，"
                "但当前解析步骤、解析结论或标准答案错误，则必须保持 question_valid=true，"
                "并设置 solution_valid=false 或 answer_consistent=false，使系统保留题面并重新生成解析。"
                "只输出结论，不得泄露试算过程、思维链或提示词。"
            ),
            input_value={
                "question_statement": core.model_dump(mode="json"),
                "completed_solution_steps": steps,
                "solution_derived_answer": conclusion.model_dump(mode="json"),
            },
            output_schema=self._completed_solution_review_schema(),
            max_tokens=2400,
            recovery_instruction="只修复最终复核 JSON；不得修改题面、解析或标准答案。",
        )
        review = _CompletedSolutionReview.model_validate(payload)
        has_independent_answers = bool(
            review.independent_answer_value and review.independent_final_answer
        )
        if review.passed and not has_independent_answers:
            review = review.model_copy(
                update={
                    "passed": False,
                    "solution_valid": False,
                    "answer_consistent": False,
                    "issues": [
                        *review.issues,
                        "最终复核没有返回独立求解答案，当前解析必须重新生成并复核。",
                    ],
                }
            )
        if review.passed and review.issues:
            review = review.model_copy(update={"passed": False})
        if not review.passed and not review.issues:
            review = review.model_copy(
                update={"issues": ["最终复核未通过，但复核器没有给出具体说明。"]}
            )
        if not review.question_valid:
            raise _QuestionInvalidAfterSolution(
                summary=review.summary,
                issues=review.issues or ["完整求解无法确认题面成立。"],
            )
        solution_matches_final_review = has_independent_answers and self._same_answer(
            core, conclusion.answer_value, self._independent_answer(core, review), stage=stage
        )

        if not (
            review.passed
            and review.solution_valid
            and review.answer_consistent
            and solution_matches_final_review
        ):
            issues = list(review.issues)
            if not has_independent_answers:
                issues.append("最终复核未返回独立答案。")
            elif not solution_matches_final_review:
                issues.append("解析所得答案与最终独立复核答案不一致。")
            raise _SolutionInvalidAfterReview(
                summary=review.summary,
                issues=issues or ["解析或标准答案未通过最终独立一致性审核。"],
            )

        # 审题员审题干时只看题面独立算过一遍答案。复核员看得到解析，容易顺着解析的错一起错，
        # 所以这里再和审题员的答案对一次。对不上时两边都可能错（审题员那次是快速求解，也会算错），
        # 就退回重新独立求解：新解析和审题员一致，或者两次解析得到同一个答案，才采用。
        reviewer_answer = self._independent_answer(core, question_review)
        if (
            self.switches.reviewer_cross_check
            and reviewer_answer
            and not self._same_answer(core, conclusion.answer_value, reviewer_answer, stage=stage)
        ):
            reviewer_said = question_review.independent_final_answer or reviewer_answer
            earlier = self._disputed_answers(core)
            if not any(self._same_answer(core, conclusion.answer_value, answer, stage=stage) for answer in earlier):
                earlier.append(conclusion.answer_value)
                raise _SolutionInvalidAfterReview(
                    summary="解析得出的答案和审题员独立算的不一致，重新独立求解核对",
                    issues=[f"审题员独立算得：{reviewer_said}", f"这次解析得出：{conclusion.final_answer}"],
                )
            self._report_answer_dispute(
                "两次独立求解得到同一个答案，和审题员算的不同，采用解析的答案",
                [f"审题员独立算得：{reviewer_said}", f"两次解析都得出：{conclusion.final_answer}"],
            )

    @staticmethod
    def _check_solution_scope(plan, steps: list[str]) -> None:
        """解析（连同思路、考查方法）用了高中不学的方法就退回重写；在得出结论和复核之前查，省掉这两次调用。"""
        out_of_scope = beyond_curriculum_methods(
            plan.solution_method,
            *getattr(plan, "knowledge_points", []),
            *getattr(plan, "assessed_methods", []),
            *steps,
        )
        if out_of_scope:
            raise _SolutionInvalidAfterReview(
                summary=f"解析用了高中不学的方法：{'、'.join(out_of_scope)}，重新用高中方法求解",
                issues=[HIGH_SCHOOL_METHODS],
            )

    def _disputed_answers(self, core: _QuestionStatement) -> list[str]:
        """和审题员答案对不上、退回重解过的答案，按题干分开记（题干改过就重新记）。"""
        return self.__dict__.setdefault("_answer_disputes", {}).setdefault(core.stem_plain, [])

    def _report_answer_dispute(self, text: str, details: list[str]) -> None:
        """答案分歧最后怎么定的；多智能体版本把它记进过程记录。"""

    def _same_answer(self, core: _QuestionStatement, first: str, second: str, *, stage: str) -> bool:
        """两个答案是不是同一个。先按规则比；填空题规则比不出来时（比如一边把答案写成了一句话），
        再请模型只比较每个空的结果是否等价。"""
        if self._answers_agree(core, first, second):
            return True
        if core.question_type != "fill_blank" or not first.strip() or not second.strip():
            return False
        retry = re.search(r"-retry-\d+$", stage)
        payload = self._request_stage(
            stage="completed-solution-review-equivalence" + (retry.group(0) if retry else ""),
            instructions=(
                "判断下面两个高中数学填空题答案是否表示同一个结果。只比较每个空最终填的值、表达式或集合，"
                "忽略叙述文字和写法差异（小数与分数、是否带 $、多余的说明）；任何一个空的结果不同就是不等价。"
            ),
            input_value={
                "question_statement": core.model_dump(mode="json"),
                "answer_1": first,
                "answer_2": second,
            },
            output_schema=self._schema(
                "private_question_authoring_answer_equivalence",
                {"equivalent": {"type": "boolean"}, "reason": {"type": "string", "maxLength": 400}},
            ),
            max_tokens=400,
            recovery_instruction="只修复 JSON；equivalent 只能是 true 或 false。",
        )
        return payload.get("equivalent") is True

    @classmethod
    def _answers_agree(cls, core: _QuestionStatement, first: str, second: str) -> bool:
        """两个答案是不是同一个。选择题比选项；填空题比整理后的写法，逐空比时 0.5 和 \\frac{1}{2} 也算一样。
        解答题的答案是一段话，没法可靠地比，按一致处理（靠复核员把关）。"""
        if core.question_type in {"single_choice", "multiple_choice"}:
            return cls._choice_answer_keys(core, first) == cls._choice_answer_keys(core, second)
        if core.question_type != "fill_blank":
            return True
        first, second = cls._canonical_answer(first), cls._canonical_answer(second)
        if first == second:
            return bool(first)
        parts_first, parts_second = re.split(r"[;,]", first), re.split(r"[;,]", second)
        if len(parts_first) != len(parts_second):
            return False
        for one, other in zip(parts_first, parts_second):
            if one == other:
                continue
            value_one, value_other = cls._number_value(one), cls._number_value(other)
            if value_one is None or value_other is None or abs(value_one - value_other) > 1e-9 * max(1.0, abs(value_one)):
                return False
        return True

    @staticmethod
    def _canonical_answer(value: str) -> str:
        """填空题答案的统一写法：去掉小问编号、$、排版命令和空白，全角标点换成半角，常见符号统一。"""
        text = re.sub(r"^(?:答案(?:为|是)?|故填|填)\s*[:：]?\s*", "", str(value or "").strip())
        text = re.sub(r"[（(]\s*\d+\s*[）)]|[①②③④⑤⑥⑦⑧⑨⑩]|第\s*[一二三四五六七八九十\d]+\s*[空问]\s*[:：]?", " ", text)
        for old, new in (
            ("$", ""), ("\\(", ""), ("\\)", ""), ("\\left", ""), ("\\right", ""), ("\\displaystyle", ""),
            ("\\dfrac", "\\frac"), ("\\tfrac", "\\frac"), ("−", "-"), ("√", "\\sqrt"), ("π", "\\pi"), ("∞", "\\infty"),
            ("，", ","), ("；", ";"), ("：", ":"), ("（", "("), ("）", ")"), ("、", ","),
        ):
            text = text.replace(old, new)
        text = re.sub(r"\\geq?(?![a-zA-Z])", "≥", text)
        text = re.sub(r"\\leq?(?![a-zA-Z])", "≤", text)
        text = re.sub(r"\\(?:quad|qquad|[,;:! ])", "", text)
        return re.sub(r"\s+", "", text).strip("。.;,")

    @staticmethod
    def _number_value(text: str) -> float | None:
        """单独一个数（小数、a/b、\\frac{a}{b}、百分数）的值；不是单独一个数时返回 None。"""
        match = re.fullmatch(r"(-?)\\frac\{(-?\d+(?:\.\d+)?)\}\{(\d+(?:\.\d+)?)\}", text)
        if match:
            sign, top, bottom = match.groups()
            return (-1 if sign else 1) * float(top) / float(bottom) if float(bottom) else None
        match = re.fullmatch(r"(-?\d+(?:\.\d+)?)(?:/(\d+(?:\.\d+)?))?(%)?", text)
        if not match:
            return None
        whole, bottom, percent = match.groups()
        if bottom is not None and not float(bottom):
            return None
        value = float(whole) / float(bottom) if bottom else float(whole)
        return value / 100 if percent else value

    def _validate_conclusion_shape(
        self,
        core: _QuestionStatement,
        conclusion: _SolutionConclusion,
    ) -> None:
        if core.question_type not in {"single_choice", "multiple_choice"}:
            return
        keys = self._choice_answer_keys(core, conclusion.answer_value)
        expected_count = 1 if core.question_type == "single_choice" else None
        if expected_count is not None and len(keys) != expected_count:
            raise QuestionAuthoringProviderError("单项选择题的解析结论必须唯一对应一个选项")
        if core.question_type == "multiple_choice" and not keys:
            raise QuestionAuthoringProviderError("多项选择题的解析结论没有给出正确选项")

    @staticmethod
    def _choice_answer_keys(
        core: _QuestionStatement,
        answer_value: str,
    ) -> tuple[str, ...]:
        valid = {option.key.strip().upper() for option in core.options}
        discovered = {
            match
            for match in re.findall(r"(?<![A-Z])[A-Z](?![A-Z])", answer_value.upper())
            if match in valid
        }
        return tuple(sorted(discovered))

    @classmethod
    def _independent_answer(
        cls,
        core: _QuestionStatement,
        review: _QuestionReview | _CompletedSolutionReview,
        *,
        final_first: bool = False,
    ) -> str:
        """审题员或复核员独立算出的答案，拿来和别的答案比。选择题比的是选项字母，可它们常把数值写进
        answer_value、把字母写进 final_answer（也有反过来的），所以取含选项字母的那个字段；
        两个字段都没有选项字母就返回空串（比不了）。以前只看一个字段，同一个选项也会被当成对不上。"""
        fields = [review.independent_answer_value or "", review.independent_final_answer or ""]
        if final_first:
            fields.reverse()
        if core.question_type in {"single_choice", "multiple_choice"}:
            return next((text for text in fields if cls._choice_answer_keys(core, text)), "")
        return next((text for text in fields if text), "")

    @staticmethod
    def _provider_input(context: QuestionAuthoringContext) -> dict:
        return {
            "design_brief": context.brief.model_dump(
                mode="json",
                exclude_none=True,
                exclude={"reference_question_id"},
            ),
            "resolved_question_type": context.question_type,
            "teacher_curriculum_guidance": (
                context.curriculum.model_dump(mode="json")
                if context.curriculum is not None
                else None
            ),
            # 老师指定了考点时只给同一章的知识点；没指定时给全部，但每个只写"编号 章 › 节 › 名称"一行。
            # 原来每次规划解法都带上 302 个知识点的完整字段，一次就一万多 Token
            "allowed_curriculum_nodes": [
                f"{item.node_id} {item.chapter} › {item.section} › {item.name}"
                for item in context.curriculum_options
                if context.curriculum is None or item.chapter == context.curriculum.chapter
            ],
        }

    @staticmethod
    def _validate_plan_orders(
        plan: _SolutionPlan | _SolutionRegenerationPlan,
    ) -> None:
        orders = [item.order for item in plan.sections]
        if orders != list(range(1, len(orders) + 1)):
            raise ValueError("解析提纲序号必须从 1 连续递增且不可重复")

    @staticmethod
    def _extract_output_text(payload: dict) -> str:
        for item in payload.get("output", []):
            if item.get("type") != "message":
                continue
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    return str(content["text"])
        raise KeyError("output_text")

    @staticmethod
    def _option_schema() -> dict:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "key": {"type": "string", "minLength": 1, "maxLength": 12},
                "text": {"type": "string", "minLength": 1, "maxLength": 4000},
            },
            "required": ["key", "text"],
        }

    def _core_schema(self) -> dict:
        cls = type(self)
        properties = {
            "question_type": {
                "type": "string",
                "enum": [
                    "single_choice",
                    "multiple_choice",
                    "fill_blank",
                    "open_response",
                ],
            },
            "stem_plain": {"type": "string", "minLength": 5, "maxLength": 20000},
            "stem_latex": {"type": ["string", "null"], "maxLength": 20000},
            "options": {"type": "array", "maxItems": 20, "items": cls._option_schema()},
            "estimated_difficulty": {"type": "integer", "minimum": 1, "maximum": 5},
            "construction": {"type": "string", "maxLength": 4000},
            "planned_answer": {"type": "string", "maxLength": 2000},
        }
        if not self.switches.construct_first:
            del properties["construction"], properties["planned_answer"]
        return cls._schema("private_question_authoring_core", properties)

    @classmethod
    def _question_review_schema(cls) -> dict:
        properties = {
            "passed": {"type": "boolean"},
            "summary": {"type": "string", "minLength": 1, "maxLength": 4000},
            "issues": {
                "type": "array",
                "maxItems": 10,
                "items": {"type": "string", "minLength": 1, "maxLength": 2000},
            },
            "independent_answer_value": {"type": ["string", "null"], "maxLength": 4000},
            "independent_final_answer": {"type": ["string", "null"], "maxLength": 4000},
        }
        return cls._schema("private_question_authoring_question_review", properties)

    def _solution_plan_schema(self) -> dict:
        cls = type(self)
        section = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "order": {"type": "integer", "minimum": 1, "maximum": 12},
                "title": {"type": "string", "minLength": 1, "maxLength": 120},
                "goal": {"type": "string", "minLength": 1, "maxLength": 500},
            },
            "required": ["order", "title", "goal"],
        }
        properties = {
            "solution_method": {"type": "string", "minLength": 1, "maxLength": 500},
            "sections": {
                "type": "array",
                "minItems": 1,
                "maxItems": 12,
                "items": section,
            },
            "authoring_intent": {"type": "string", "minLength": 1, "maxLength": 2000},
            "curriculum_node_id": {"type": ["string", "null"], "maxLength": 160},
            "key_results": {"type": "array", "maxItems": 16, "items": {"type": "string", "maxLength": 400}},
            "knowledge_points": {
                "type": "array",
                "maxItems": 20,
                "items": {"type": "string", "maxLength": 300},
            },
            "assessed_methods": {
                "type": "array",
                "maxItems": 20,
                "items": {"type": "string", "maxLength": 300},
            },
            "common_mistakes": {
                "type": "array",
                "maxItems": 20,
                "items": {"type": "string", "maxLength": 300},
            },
        }
        if not self.switches.key_results:
            del properties["key_results"]
        return cls._schema("private_question_authoring_solution_plan", properties)

    @classmethod
    def _solution_regeneration_plan_schema(cls) -> dict:
        section = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "order": {"type": "integer", "minimum": 1, "maximum": 12},
                "title": {"type": "string", "minLength": 1, "maxLength": 120},
                "goal": {"type": "string", "minLength": 1, "maxLength": 500},
            },
            "required": ["order", "title", "goal"],
        }
        properties = {
            "solution_method": {"type": "string", "minLength": 1, "maxLength": 500},
            "sections": {
                "type": "array",
                "minItems": 1,
                "maxItems": 12,
                "items": section,
            },
            "key_results": {"type": "array", "maxItems": 16, "items": {"type": "string", "maxLength": 400}},
        }
        return cls._schema(
            "private_question_authoring_solution_regeneration_plan",
            properties,
        )

    @classmethod
    def _solution_section_schema(cls) -> dict:
        properties = {
            "order": {"type": "integer", "minimum": 1, "maximum": 12},
            "steps": {
                "type": "array",
                "minItems": 1,
                "maxItems": 100,
                "items": {"type": "string", "minLength": 1, "maxLength": 6000},
            },
        }
        return cls._schema("private_question_authoring_solution_section", properties)

    @classmethod
    def _solution_cleanup_schema(cls) -> dict:
        properties = {
            "order": {"type": "integer", "minimum": 1, "maximum": 12},
            "steps": {
                "type": "array",
                "minItems": 1,
                "maxItems": 100,
                "items": {"type": "string", "minLength": 1, "maxLength": 6000},
            },
            "mathematically_valid": {"type": "boolean"},
            "issue_summary": {"type": ["string", "null"], "maxLength": 500},
            "fault_source": {
                "type": ["string", "null"],
                "enum": ["question", "solution", None],
            },
        }
        return cls._schema("private_question_authoring_solution_cleanup", properties)

    @classmethod
    def _solution_conclusion_schema(cls) -> dict:
        properties = {
            "answer_value": {"type": "string", "minLength": 1, "maxLength": 4000},
            "final_answer": {"type": "string", "minLength": 1, "maxLength": 4000},
        }
        return cls._schema("private_question_authoring_solution_conclusion", properties)

    @classmethod
    def _completed_solution_review_schema(cls) -> dict:
        properties = {
            "passed": {"type": "boolean"},
            "question_valid": {"type": "boolean"},
            "solution_valid": {"type": "boolean"},
            "answer_consistent": {"type": "boolean"},
            "summary": {"type": "string", "minLength": 1, "maxLength": 4000},
            "issues": {
                "type": "array",
                "maxItems": 10,
                "items": {"type": "string", "minLength": 1, "maxLength": 2000},
            },
            "independent_answer_value": {
                "type": ["string", "null"],
                "minLength": 1,
                "maxLength": 4000,
            },
            "independent_final_answer": {
                "type": ["string", "null"],
                "minLength": 1,
                "maxLength": 4000,
            },
        }
        return cls._schema(
            "private_question_authoring_completed_solution_review",
            properties,
        )

    @classmethod
    def _solution_subplan_schema(cls) -> dict:
        part = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "order": {"type": "integer", "minimum": 1, "maximum": 6},
                "title": {"type": "string", "minLength": 1, "maxLength": 120},
                "goal": {"type": "string", "minLength": 1, "maxLength": 500},
            },
            "required": ["order", "title", "goal"],
        }
        properties = {
            "parts": {
                "type": "array",
                "minItems": 2,
                "maxItems": 6,
                "items": part,
            }
        }
        return cls._schema("private_question_authoring_solution_subplan", properties)

    @staticmethod
    def _schema(name: str, properties: dict) -> dict:
        return {
            "type": "json_schema",
            "name": name,
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": properties,
                "required": list(properties),
            },
        }
