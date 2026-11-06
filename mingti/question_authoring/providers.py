from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from mingti.deepseek import DeepSeekClientError, DeepSeekJsonClient, DeepSeekJsonResponseError
from mingti.math_content import MATH_DISPLAY_INSTRUCTIONS, tidy_math_text
from mingti.model_operations import ModelRunRecorder, NullModelRunRecorder
from mingti.topic_model import narrow_curriculum_lines
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
    scope_violations,
)


PROMPT_REVISION = "question-authoring-v15-agent-handoff"

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
# 解析被复核退回时，解题员按意见定点修改，最多改这么多轮。还没通过就整份重写这么多次；
# 再不行就把最好的一版交出去，并写明还有哪些意见没解决（宁可标明待核对，也不能让用户什么都拿不到）
SOLUTION_REPAIR_ATTEMPTS = 4
FRESH_SOLUTION_ATTEMPTS = 1
WHOLE_QUESTION_ATTEMPTS = 3
# 解析用了高中以外的方法时，复核员最多判断这么多次（每次之后解题员按判断改或者说明做不到）；
# 还改不掉就不在解析上耗了，退回命题员改题
SCOPE_REVIEW_ROUNDS = 2


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

# 解析被复核退回时，解题员看到的要求：是"改"不是"重写"。一份长解析重写一遍又会冒出新的小错，
# 永远过不了；定点修改每轮都在减少错误，几轮内就能收敛
_SOLUTION_REPAIR_RULES = (
    "你是解题员。复核员看过你写的完整解析（solution_steps，已编号）并提出了意见。请按意见修改，不要重写。\n"
    "1. 逐条核算 review_issues：意见成立就改；确实是复核员看错了就不要改，"
    "并在 disputed_issues 里写清楚这条意见为什么不成立，要有具体的推算。\n"
    "2. 只改有问题的步骤和受它影响的后续步骤，其余步骤一个字都不要动，也不要返回。\n"
    "3. 沿用原来的解法和思路，不要换方法；只有意见指出所用方法超纲或行不通时，才换掉这一小段的做法。\n"
    "4. 不得修改题面，不得添加题面没有的条件。\n"
    "5. edits 里每一项写要改的步骤编号 step 和改好以后的内容 replacement："
    "一步拆成几步就写几行，这一步要删掉就写空数组。\n"
    "6. reviewer_independent_answer 是复核员独立算的答案，可以对照，但要自己算清楚，不要直接照抄。\n"
    "7. earlier_rounds 是前几轮的意见和改动，已经改对的地方不要改回去。\n"
    "8. solution_method 只有思路概述里的说法也要跟着改时才写，否则写空字符串。\n"
    "9. 有 scope_review 时，问题是解析用了高中课程以外的内容，scope_review 里是复核员的判断："
    "wording 表示只是说法超纲，把指出的说法换成高中的说法；high_school 表示有高中方法，"
    "按 fixes 里的做法把这几步改成高中方法（需要的话可以连同受影响的后续步骤一起改写）；"
    "university_only 表示复核员认为只能用大学方法，请你自己再判断一次：能用高中方法做就照样改写，"
    "确实做不到才把 edits 留空，在 scope_objection 里写清楚为什么高中方法做不出来（卡在哪一步、缺什么工具）。"
    "没有 scope_review 时 scope_objection 写空字符串。\n"
)

# 程序在解析里查到高中课程以外的内容后，复核员看的要求：判断是说法问题、能换高中方法，还是题目只能用大学方法解
_SCOPE_REVIEW_RULES = (
    "你是复核员。程序在解题员写的解析里查到了高中课程以外的内容，scope_findings 写着是什么、在第几步。"
    "请对照题面和编号的解析判断属于哪一种，写进 verdict：\n"
    "wording：只是说法超纲，数学上用的仍是高中方法（比如把高中的随机游走、递推叫作“随机过程”“马尔可夫链”）。"
    "fixes 里写清楚第几步的哪个说法换成什么高中说法。\n"
    "high_school：用了大学方法，但这道题能用高中方法做。fixes 里按步骤写清楚改用哪种高中方法、"
    "关键的式子或结论怎么得到，要具体到解题员照着就能改。\n"
    "university_only：这道题用高中方法确实做不出来，只能用大学方法。summary 写清楚为什么；"
    "composer_advice 写给命题员：改哪些条件、设问或数据，才能保持原来的要求、难度和题型，又能用高中方法解。\n"
    "只有确实找不到高中方法时才判 university_only。earlier_scope_rounds 里是前几轮的判断和解题员的回复："
    "解题员在 solver_objection 里说高中方法做不出来时，请认真核算他的理由：理由成立就判 university_only，"
    "不成立就判 high_school，并在 fixes 里给出比上一轮更具体的做法。\n"
    "verdict 不是 university_only 时 composer_advice 写空字符串。只输出结论，不得泄露试算过程或思维链。"
)

# 中档及以上的题，这几步打开模型的思考模式：构造题目、审题员独立求解、规划解法、复核。
# 这几步算错会引发整轮返工（题设前后矛盾、法向量算错），思考几秒很划算；
# 写解析段落、提取答案、清理措辞这类调用多而简单，不开，免得整体太慢。
_THINKING_STAGES = re.compile(
    r"^(?:core|core-high-school-repair|question-review|solution-regeneration-question-review"
    r"|core-question-repair.*|post-solution-question-repair.*|answer-gate-repair.*|.*-review-check(?:-\d+)?"
    r"|solution-plan|solution-repair-\d+|solution-regeneration-plan|solution-scope-review(?:-\d+)?"
    r"|completed-solution-review(?:-repair-\d+)?|solution-regeneration-verification)$"
)
# 上面这些里，审题员和复核员检查的几步。思考强度选"中"时这几步不开思考，
# 只在出题的几步（构造题目、改题、规划解法）开：出错少、返工少，又比全开快
_CHECK_STAGES = re.compile(
    r"^(?:question-review|solution-regeneration-question-review|.*-review-check(?:-\d+)?|solution-scope-review(?:-\d+)?"
    r"|completed-solution-review(?:-repair-\d+)?|solution-regeneration-verification)$"
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

# 整道题重出时命题员看到的话：上一版为什么被退回。不知道原因的重出和抽签差不多，同样的问题会再犯
_REAUTHOR_NOTE = (
    "earlier_rejected_versions 是之前按同一要求出过、但被退回的版本（rejected_stem）和退回的原因（problems）。"
    "这一版要针对这些原因重新设计：避开同样的问题（比如条件不足或互相矛盾、数据不是由构造算出、"
    "只能用大学方法才能解、推导太长太繁），同时仍然满足 design_brief 的全部要求；不要把旧题稍改几个字再交一遍。"
)
# 整份重写解析时解题员看到的话：上一份解析为什么被退回
_RESOLVE_NOTE = (
    "earlier_solution_problems 是这道题上一份解析被退回的原因。这一份是重新写的：规划时针对这些原因，"
    "不要再犯同样的错误；如果原因是用了高中课程以外的方法，从一开始就只用高中方法。"
)


@dataclass(frozen=True)
class PipelineSwitches:
    """对比实验（eval/ablation.py）用的开关。正常出题全用默认值，也就是现在的完整流程。"""

    stop_after: Literal["compose", "review", "solve"] = "solve"  # 只要命题员 / 加上审题员 / 完整闭环
    thinking: bool = True  # 关键阶段打开思考模式；False 时哪一步都不开（包括 4、5 星的题）
    construct_first: bool = True  # 命题员先构造、后成题，并给出按构造算出的答案
    answer_gate: bool = True  # 写解析前先把构造答案和审题员独立算的答案对一次
    key_results: bool = True  # 解析规划列出关键中间结果，各段共用
    reviewer_cross_check: bool = True  # 解析的答案最后再和审题员的独立答案对一次
    targeted_repair: bool = True  # 解析被退回时按复核意见定点修改、到上限交付最好的一版；False 时整份重写、出不成就失败


class QuestionAuthoringProviderError(RuntimeError):
    pass


class _QuestionInvalidAfterSolution(QuestionAuthoringProviderError):
    """The full solve exposed a statement flaw missed by the pre-solve review."""

    def __init__(self, *, summary: str, issues: list[str]) -> None:
        self.summary = summary
        self.issues = issues
        super().__init__("完整求解复核发现题面仍有问题，必须先完善题面并重新审核。")


class _QuestionNeedsHighSchoolRework(_QuestionInvalidAfterSolution):
    """解题员和复核员都认为这道题只能用大学方法解：题面本身没错，但要退回命题员改成高中方法能解的题。
    issues 里带着复核员的判断、改题建议和解题员的理由，命题员改题时都看得到。"""


class _SolutionInvalidAfterReview(QuestionAuthoringProviderError):
    """The statement is valid, but the generated derivation or answer is not."""

    def __init__(
        self,
        *,
        summary: str,
        issues: list[str],
        independent_answer: str = "",
        answer_disputed: bool = False,
        needs_fresh_solve: bool = False,
        scope: dict | None = None,
    ) -> None:
        self.summary = summary
        self.issues = issues
        self.independent_answer = independent_answer  # 复核员自己算的答案，修改时给解题员对照
        self.answer_disputed = answer_disputed  # 分歧在答案本身，不只是过程里有错
        self.needs_fresh_solve = needs_fresh_solve  # 要再独立解一遍才说得清，改解析解决不了
        # 解析用了高中以外的内容时，复核员的判断（verdict / summary / fixes / composer_advice / terms）
        self.scope = scope
        self.repairs_exhausted = False  # 定点修改的轮数用完了
        self.reported = False  # 已经记进过程记录
        super().__init__("解析与题面不一致，必须保留题面并重新生成解析。")


class _SolutionSectionFailed(_SolutionInvalidAfterReview):
    """写解析时某一段试了几次都没写成（返回的内容读不了、写进了思考过程）。这是解题员自己输出的问题，
    不是题的问题：整份重写解析（带着原因），不能因此让整个任务失败。"""

    def __init__(self, *, summary: str, issues: list[str]) -> None:
        super().__init__(summary=summary, issues=issues)


class _ContentCandidateRejected(QuestionAuthoringProviderError):
    """One complete candidate failed quality recovery and must be replaced.

    stem / problems：被退回的题面和退回的原因。整道题重出时交给命题员，让他知道上一版错在哪里，
    而不是什么都不知道地再出一版（那样和抽签差不多，同样的问题会再犯）。"""

    def __init__(self, message: str = "", *, stem: str = "", problems: list[str] | tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.stem = stem
        self.problems = [str(item) for item in problems if str(item).strip()]

    def feedback(self) -> dict | None:
        """给下一版命题员看的一条：被退回的题面（截短）和原因。"""
        if not self.stem and not self.problems:
            return None
        problems = [item[:500] for item in (self.problems or [str(self)])[:6]]  # 复核意见有时很长，只留要点
        return {"rejected_stem": self.stem[:800], "problems": problems}


@dataclass(frozen=True)
class _SolutionCandidate:
    """写完了、也得出了答案，但复核还有意见的一版。返工到上限时从这里挑最好的一版交付。"""

    core: Any
    plan: Any
    steps: tuple[str, ...]
    conclusion: Any
    summary: str
    issues: tuple[str, ...]
    answer_disputed: bool


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
    # 交付前最后一关的检查（结构、可信检查、范围），不抛异常，返回 [(谁来改, 问题)]，"statement" 是命题员、
    # "solution" 是解题员。由 QuestionAuthoring 提供：出题流程在交出一道题之前先跑一遍，有问题就退回对应的角色，
    # 不会等题交出去以后才让整个任务失败
    acceptance: Callable[[ProviderQuestionDraft], list[tuple[str, str]]] | None = field(
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


class _SolutionEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step: int = Field(ge=1)
    replacement: list[str] = Field(default_factory=list, max_length=12)


class _SolutionRepair(BaseModel):
    """解题员按复核意见改的地方：只列改动，没列的步骤一字不动。"""

    model_config = ConfigDict(extra="forbid")

    edits: list[_SolutionEdit] = Field(default_factory=list, max_length=40)
    solution_method: str = Field(default="", max_length=500)
    disputed_issues: list[str] = Field(default_factory=list, max_length=10)
    # 只在超纲的退回里用：解题员认为高中方法确实做不出来时，写清楚理由
    scope_objection: str = Field(default="", max_length=1500)


class _ScopeReview(BaseModel):
    """复核员对"解析用了高中以外的内容"的判断，决定交给解题员改，还是退回命题员改题。"""

    model_config = ConfigDict(extra="forbid")

    verdict: Literal["wording", "high_school", "university_only"]
    summary: str = Field(min_length=1, max_length=1500)
    fixes: list[str] = Field(default_factory=list, max_length=10)
    composer_advice: str = Field(default="", max_length=1500)


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
        earlier: list[dict] = []  # 前几版被退回的题面和原因，重出时交给命题员
        for candidate_attempt in range(1, WHOLE_QUESTION_ATTEMPTS + 1):
            try:
                return self._generate_candidate(
                    context,
                    reauthoring=candidate_attempt > 1,
                    earlier=earlier[-2:],
                )
            except _ContentCandidateRejected as exc:
                last_error = exc
                if exc.feedback():
                    earlier.append(exc.feedback())
                continue
        best = self.best_candidate_draft()
        if best is not None:
            return best
        raise QuestionAuthoringProviderError(
            "多份候选题均未通过闭环审核，需要继续重新命题。"
        ) from last_error

    def _generate_candidate(
        self,
        context: QuestionAuthoringContext,
        *,
        reauthoring: bool,
        earlier: list[dict] | tuple[dict, ...] = (),
    ) -> ProviderQuestionDraft:
        """earlier：前几版被退回的题面和原因（_ContentCandidateRejected.feedback）。重出时命题员看得到，
        知道上一版错在哪里，不会什么都不知道地再出一版、再犯同样的问题。"""
        provider_input = self._provider_input(context)
        self._think = self._think_level(context.brief.target_difficulty)
        self._candidate_context = context
        earlier = [item for item in earlier if item]
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
                    f"{_REAUTHOR_NOTE if earlier else ''}"
                ),
                input_value={
                    "design_brief": provider_input["design_brief"],
                    "resolved_question_type": provider_input["resolved_question_type"],
                    "teacher_curriculum_guidance": provider_input[
                        "teacher_curriculum_guidance"
                    ],
                    **({"earlier_rejected_versions": earlier} if earlier else {}),
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
            core, question_review = self._fix_statement_format(core, question_review, provider_input, context.question_type)
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
            fresh_after_repairs = 0
            solve_attempt = 0
            solution_problems: list[str] = []  # 上一份解析为什么被退回，重新写解析时交给解题员
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
                        earlier_problems=solution_problems,
                    )
                    draft = self._draft_from(context, core, plan, steps, conclusion)
                    # 交出去之前先过一遍最后一关的检查：题面的问题退回命题员，解析的问题让解题员重写，
                    # 不会等题交出去以后才让整个任务失败
                    self._check_acceptance(context, draft)
                except _QuestionInvalidAfterSolution as exc:
                    if not isinstance(exc, _QuestionNeedsHighSchoolRework):
                        # 题面本身不成立，这个题面上的解析不能交付。只是要改成高中方法能解的题时不一样：
                        # 原来的题面和手里那版（高中方法写的）解析都没错，到上限时仍可以交付
                        self._forget_candidates(core)
                    if question_repair_count >= POST_SOLUTION_QUESTION_REPAIR_ATTEMPTS:
                        raise _ContentCandidateRejected(
                            "题面经完整求解、完善和重新审核后仍无法成立，无法交付完整题目。",
                            stem=core.stem_plain,
                            problems=[exc.summary, *exc.issues],
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
                    solution_problems = []  # 题面改过了，旧解析的问题不再适用
                    continue
                except _SolutionInvalidAfterReview as exc:
                    if exc.repairs_exhausted:
                        fresh_after_repairs += 1
                    if (
                        fresh_after_repairs > FRESH_SOLUTION_ATTEMPTS
                        or solution_regeneration_count >= SOLUTION_REGENERATION_ATTEMPTS
                    ):
                        # 题面是成立的，手里有写完的解析：交出最好的一版并写明没解决的意见，不扔掉重出
                        best = self.best_candidate_draft()
                        if best is not None:
                            return best
                        raise _ContentCandidateRejected(
                            "题面已确认成立，但解析经过多轮重新生成仍未通过一致性审核，无法交付半成品。",
                            stem=core.stem_plain,
                            problems=[
                                f"题面审核通过了，但解析写了几版都没有通过复核：{exc.summary}",
                                *exc.issues[:3],
                                "重出时把数据和设问设计得更干净，让推导更短、更不容易出错。",
                            ],
                        ) from exc
                    solution_regeneration_count += 1
                    solution_problems = self._fresh_solve_notes(exc)
                    if solution_regeneration_count == 1:
                        context.report_progress("regenerating_solution")
                    continue
                except (DeepSeekJsonResponseError, KeyError, TypeError, ValueError) as exc:
                    # 写解析的某一步返回的内容读不了或缺字段：题面已经审过，留着题面重写解析，和复核退回一起计次
                    if solution_regeneration_count >= SOLUTION_REGENERATION_ATTEMPTS:
                        best = self.best_candidate_draft()
                        if best is not None:
                            return best
                        raise _ContentCandidateRejected(
                            "解析几次返回的内容都不完整或格式不对",
                            stem=core.stem_plain,
                            problems=["解析写了几次都没能完整写出来，可能推导太长、计算太繁；重出时让推导更简洁。"],
                        ) from exc
                    solution_regeneration_count += 1
                    if solution_regeneration_count == 1:
                        context.report_progress("regenerating_solution")
                    continue

                return draft
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
        earlier_problems: list[str] | None = None,
    ) -> tuple[_SolutionPlan, list[str], _SolutionConclusion]:
        """earlier_problems：这道题上一份解析被退回的原因。整份重写时交给解题员，不让他蒙着头再写一份。"""
        earlier_problems = [item for item in earlier_problems or [] if item]
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
                    "不要把模型推断冒充教师确认，也用高中课本里的说法。"
                    f"{_KEY_RESULTS_PLAN if self.switches.key_results else ''}"
                    f"{HIGH_SCHOOL_METHODS}"
                    f"{_RESOLVE_NOTE if earlier_problems else ''}"
                ),
                input_value={
                    "design_brief": provider_input["design_brief"],
                    "question_statement": core.model_dump(mode="json"),
                    "allowed_curriculum_nodes": self._plan_curriculum_nodes(provider_input, core),
                    **({"earlier_solution_problems": earlier_problems} if earlier_problems else {}),
                },
                output_schema=self._solution_plan_schema(),
                max_tokens=3500,
                recovery_instruction="只修复 JSON 结构；保留覆盖全部小问所需的解析分段。",
            )
        )
        plan = self._clean_plan_labels(self._normalized_plan(plan))
        sections = self._expand_sections(core, plan, stage_suffix=stage_suffix)
        steps = [step for section in sections for step in section.steps if step.strip()]  # 模型偶尔多写一个空步骤，丢掉即可

        # 复核退回后不重写：把意见交给解题员，只改有问题的地方，再复核。前几轮的意见和改动都带着，
        # 解题员不会把改对的地方改回去，复核员也知道上一轮提过什么
        limit = SOLUTION_REPAIR_ATTEMPTS if self.switches.targeted_repair else 0
        rounds: list[dict] = []
        scope_rounds: list[dict] = []  # 超纲问题上复核员的判断和解题员的回复，见 _check_solution_scope
        for round_no in range(limit + 1):
            tag = f"-repair-{round_no}" if round_no else ""
            conclusion = None
            try:
                self._check_solution_scope(core, plan, steps, scope_rounds, stage_suffix=stage_suffix)
                conclusion = self._derive_solution_conclusion(
                    core,
                    steps,
                    stage=f"solution-conclusion{tag}{stage_suffix}",
                )
                if on_review is not None:
                    on_review()
                self._verify_completed_solution(
                    core,
                    steps,
                    conclusion,
                    question_review=question_review,
                    stage=f"completed-solution-review{tag}{stage_suffix}",
                    earlier_rounds=rounds,
                )
                return plan, steps, conclusion
            except _SolutionInvalidAfterReview as exc:
                self._report_solution_rejected(exc)
                exc.reported = True
                if conclusion is not None:
                    self._remember_candidate(core, plan, steps, conclusion, exc)
                if exc.needs_fresh_solve or not self.switches.targeted_repair:
                    raise
                if round_no >= limit:
                    exc.repairs_exhausted = True
                    raise
                plan, steps = self._repair_solution(
                    core,
                    plan,
                    steps,
                    exc,
                    rounds,
                    round_no=round_no + 1,
                    stage_suffix=stage_suffix,
                    scope_rounds=scope_rounds,
                )
        raise AssertionError("定点修改的循环一定在上面返回或抛出")

    def _repair_solution(
        self,
        core: _QuestionStatement,
        plan: _SolutionPlan,
        steps: list[str],
        rejection: _SolutionInvalidAfterReview,
        rounds: list[dict],
        *,
        round_no: int,
        stage_suffix: str,
        scope_rounds: list[dict] | None = None,
    ) -> tuple[_SolutionPlan, list[str]]:
        """解题员读复核意见，只改有问题的步骤；其余步骤由程序原样保留，不经过模型。
        超纲的退回（rejection.scope）还带着复核员的判断；解题员认为高中方法做不出来时可以在 scope_objection 里说明，
        记进 scope_rounds，下一轮由复核员核算（见 _check_solution_scope）。"""
        input_value = {
            "question_statement": core.model_dump(mode="json"),
            "solution_method": plan.solution_method,
            "key_results": plan.key_results,
            "solution_steps": [{"step": index, "text": text} for index, text in enumerate(steps, 1)],
            "review_summary": rejection.summary,
            "review_issues": rejection.issues,
            "reviewer_independent_answer": rejection.independent_answer,
            "earlier_rounds": rounds,
        }
        if rejection.scope:
            input_value["scope_review"] = {
                key: rejection.scope.get(key) for key in ("verdict", "summary", "fixes", "terms")
            }
        for attempt in range(2):
            try:
                payload = self._request_stage(
                    stage=f"solution-repair-{round_no}{stage_suffix}",
                    instructions=f"{_SOLUTION_REPAIR_RULES}{_POLISHED_SOLUTION_INSTRUCTIONS}{MATH_DISPLAY_INSTRUCTIONS}",
                    input_value=input_value,
                    output_schema=self._solution_repair_schema(),
                    max_tokens=6000,
                    recovery_instruction="只修复 JSON 结构；保留 edits 里的修改内容，不要改写其他步骤。",
                )
                repair = _SolutionRepair.model_validate(payload)
                repaired, changed = self._apply_solution_edits(steps, repair)
                break
            except (DeepSeekJsonResponseError, KeyError, TypeError, ValueError) as exc:
                if attempt == 1:  # 这一轮没改成：解析留着，由外层决定整份重写还是交付最好的一版
                    rejection.repairs_exhausted = True
                    raise rejection from exc
        objection = repair.scope_objection.strip() if rejection.scope else ""
        rounds.append(
            {
                "review_issues": rejection.issues,
                "changed_text": changed,
                "solver_disputes": repair.disputed_issues,
                **({"solver_scope_objection": objection} if objection else {}),
            }
        )
        if rejection.scope and scope_rounds and scope_rounds[-1] is rejection.scope:
            rejection.scope["solver_objection"] = objection
            rejection.scope["solver_changed"] = changed[:6]
        self._report_solution_repaired(round_no, changed, repair.disputed_issues, objection)
        # 思路概述跟着改；考查方法、知识点里超纲的词不进交付内容，直接去掉
        return (
            self._clean_plan_labels(
                plan.model_copy(update={"solution_method": repair.solution_method.strip() or plan.solution_method})
            ),
            repaired,
        )

    @staticmethod
    def _clean_plan_labels(plan: _SolutionPlan) -> _SolutionPlan:
        """知识点、考查方法、易错点只是标签。里面出现大学的名词（多半是照抄了要求里的说法，比如"随机过程中的递推"）
        就去掉这一条：去掉不影响题目，留着的话交付前的范围检查会把整道题拦下。"""

        def keep(items: list[str]) -> list[str]:
            return [item for item in items if not scope_violations(item)]

        return plan.model_copy(
            update={
                "knowledge_points": keep(plan.knowledge_points),
                "assessed_methods": keep(plan.assessed_methods),
                "common_mistakes": keep(plan.common_mistakes),
            }
        )

    def _apply_solution_edits(self, steps: list[str], repair: _SolutionRepair) -> tuple[list[str], list[str]]:
        """把改动合进解析：没提到的步骤原样保留。返回修改后的解析和新写的内容。"""
        replacements: dict[int, list[str]] = {}
        for edit in repair.edits:
            if not 1 <= edit.step <= len(steps):
                raise ValueError(f"修改的步骤编号 {edit.step} 不存在")
            replacements[edit.step] = [line.strip() for line in edit.replacement if line.strip()]
        if not replacements and not repair.disputed_issues and not repair.scope_objection.strip():
            raise ValueError("既没有修改，也没有说明复核意见为什么不成立")
        merged: list[str] = []
        for index, text in enumerate(steps, 1):
            merged.extend(replacements.get(index, [text]))
        changed = [line for lines in replacements.values() for line in lines]
        if not merged:
            raise ValueError("修改后解析是空的")
        if self._contains_deliberation(changed):
            raise ValueError("修改的内容里有模型的思考或自我纠错叙述")
        return merged, changed

    def _remember_candidate(
        self,
        core: _QuestionStatement,
        plan: _SolutionPlan,
        steps: list[str],
        conclusion: _SolutionConclusion,
        rejection: _SolutionInvalidAfterReview,
    ) -> None:
        if not self.switches.targeted_repair:
            return
        self.__dict__.setdefault("_candidates", []).append(
            _SolutionCandidate(
                core=core,
                plan=plan,
                steps=tuple(steps),
                conclusion=conclusion,
                summary=rejection.summary,
                issues=tuple(rejection.issues),
                answer_disputed=rejection.answer_disputed,
            )
        )
        self._report_candidate()

    def _forget_candidates(self, core: _QuestionStatement) -> None:
        kept = [item for item in self.__dict__.get("_candidates", []) if item.core.stem_plain != core.stem_plain]
        self.__dict__["_candidates"] = kept

    def best_candidate_draft(self) -> ProviderQuestionDraft | None:
        """返工到上限时交付的那一版：答案没有分歧的优先，其次意见最少；一样好时取最新的。
        要写明还有哪些意见没解决，老师才知道该核对什么。"""
        candidates = self.__dict__.get("_candidates") or []
        context = self.__dict__.get("_candidate_context")
        check = getattr(context, "acceptance", None)
        # 一样好时取最新的；过不了交付前最后一关检查的那几版不交（交出去也会被拦下，任务照样失败）
        for best in sorted(reversed(candidates), key=lambda item: (item.answer_disputed, len(item.issues))):
            note = "；".join(best.issues[:3]) or best.summary
            if best.answer_disputed:
                note = f"答案和复核员算的不一致：{note}"
            draft = self._draft_from(context, best.core, best.plan, list(best.steps), best.conclusion, issue=note[:480])
            if check is None or not check(draft):
                return draft
        return None

    _TYPE_NAMES = {"single_choice": "单选题", "multiple_choice": "多选题", "fill_blank": "填空题", "open_response": "解答题"}

    @classmethod
    def _statement_format_problems(cls, core: _QuestionStatement, expected: str) -> list[str]:
        """题面的格式和题型对不对得上（只看结构，不看数学）：题型、选择题的四个选项。"""
        problems = []
        if core.question_type != expected:
            problems.append(
                f"要出的是{cls._TYPE_NAMES.get(expected, expected)}，这一版写成了{cls._TYPE_NAMES.get(core.question_type, core.question_type)}"
            )
        if expected in ("single_choice", "multiple_choice"):
            keys = [option.key.strip().upper() for option in core.options]
            texts = [re.sub(r"[\s$]", "", option.text) for option in core.options]
            if len(core.options) < 4:
                problems.append(f"选择题要给出 A、B、C、D 四个选项，这一版只有 {len(core.options)} 个")
            if len(set(keys)) != len(keys):
                problems.append("选项编号有重复")
            if any(not text for text in texts) or len(set(texts)) != len(texts):
                problems.append("选项内容不能为空，也不能有两个选项完全相同")
        return problems

    def _fix_statement_format(
        self,
        core: _QuestionStatement,
        question_review: _QuestionReview,
        provider_input: dict,
        expected: str,
    ) -> tuple[_QuestionStatement, _QuestionReview]:
        """写解析之前先把题面的格式理顺：非选择题多给的选项直接去掉；题型不对、选择题选项不全或重复，
        退回命题员按具体问题改（改完审题员再审）。以前这类问题要到最后的结构检查才发现，整个任务失败。"""
        if expected not in ("single_choice", "multiple_choice") and core.question_type == expected and core.options:
            core = core.model_copy(update={"options": []})
        problems = self._statement_format_problems(core, expected)
        if not problems:
            return core, question_review
        return self._repair_question_from_issues(
            core,
            provider_input,
            review_summary="题面的格式和题型要求对不上",
            review_issues=problems,
            stage_prefix="core-format-repair",
            attempts=2,
        )

    def _check_acceptance(self, context: QuestionAuthoringContext | None, draft: ProviderQuestionDraft) -> None:
        """交出去之前过一遍最后一关的检查（context.acceptance，和 QuestionAuthoring 交付时的检查是同一套）。
        题面的问题退回命题员改（_QuestionInvalidAfterSolution），解析和答案的问题让解题员重写（_SolutionInvalidAfterReview）。"""
        check = getattr(context, "acceptance", None)
        problems = check(draft) if check is not None else []
        if not problems:
            return
        statement = [text for kind, text in problems if kind == "statement"]
        solution = [text for kind, text in problems if kind != "statement"]
        self._report_acceptance(statement or solution, to_composer=bool(statement))
        if statement:
            raise _QuestionInvalidAfterSolution(summary="交付前的检查发现题面不合要求", issues=[*statement, *solution])
        raise _SolutionInvalidAfterReview(summary="交付前的检查发现解析或答案不完整", issues=solution)

    def _report_acceptance(self, problems: list[str], *, to_composer: bool) -> None:
        """交付前的检查退回了这一版；多智能体版本把它记进过程记录。"""

    def _draft_from(
        self,
        context: QuestionAuthoringContext | None,
        core: _QuestionStatement,
        plan: _SolutionPlan,
        steps: list[str],
        conclusion: _SolutionConclusion,
        *,
        issue: str | None = None,
    ) -> ProviderQuestionDraft:
        guided = context.curriculum.node_id if context is not None and context.curriculum else None
        return ProviderQuestionDraft(
            question_type=core.question_type,
            stem_plain=core.stem_plain,
            stem_latex=core.stem_latex,
            options=core.options,
            answer_value=conclusion.answer_value,
            solution_method=plan.solution_method,
            solution_steps=steps,
            solution_status="open_review_issues" if issue else "ready",
            solution_issue=issue,
            final_answer=conclusion.final_answer,
            authoring_intent=plan.authoring_intent,
            estimated_difficulty=core.estimated_difficulty,
            curriculum_node_id=plan.curriculum_node_id or guided,
            knowledge_points=plan.knowledge_points,
            assessed_methods=plan.assessed_methods,
            common_mistakes=plan.common_mistakes,
        )

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
                plan = self._normalized_plan(plan)
                try:
                    sections = self._expand_sections(core, plan, stage_suffix=suffix)
                    steps = [step for section in sections for step in section.steps if step.strip()]  # 模型偶尔多写一个空步骤，丢掉即可
                    self._check_solution_scope(core, plan, steps)
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
        if [item.order for item in sections] != expected:  # 段号由程序定，正常不会出现；万一出现就整份重写，不让任务失败
            raise _SolutionSectionFailed(summary="解析各段没能拼成完整的一份", issues=["分段解析序号不完整"])
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
        except _SolutionSectionFailed as failure:
            # 多半是这一段太长、输出被截断或写乱了：拆成几个小段再写；还不行由外层整份重写解析
            return self._subdivide_and_expand(
                core=core,
                plan=plan,
                section=section,
                original_error=failure,
            )

    def _request_solution_section(
        self,
        *,
        section: _SolutionPlanSection,
        stage: str,
        input_value: dict,
        instructions: str,
    ) -> _SolutionSection:
        """写一段解析。模型回复里的段号只是重复了一遍要写哪一段，程序知道是第几段，不一致就按要写的那段算，
        不当成错误（以前段号写错一个数字，整个任务就失败了）。返回的内容读不了、清理后还有思考过程，重试一次；
        还不行抛 _SolutionSectionFailed，由外层拆小段或整份重写，不让任务失败。接口出错（网络、余额）照常往外抛。"""
        last_error: Exception | None = None
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
                parsed = _SolutionSection.model_validate({**payload, "order": section.order})
                if self._contains_deliberation(parsed.steps):
                    parsed = self._cleanup_solution_section(
                        section=section,
                        input_value=input_value,
                        contaminated=parsed,
                    )
                return parsed
            except (DeepSeekJsonResponseError, KeyError, TypeError, ValueError) as exc:
                last_error = exc
        raise _SolutionSectionFailed(
            summary=f"第 {section.order} 段解析试了两次都没写成",
            issues=[f"第 {section.order} 段（{section.title}）：{str(last_error)[:200]}"],
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
        # 段号只是重复一遍是哪一段，以要清理的这一段为准（模型写错段号以前会让整个任务失败）
        cleaned = _SolutionCleanup.model_validate({**payload, "order": section.order})
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
        return _SolutionSection(order=section.order, steps=cleaned.steps)

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
        """一段解析写不成时拆成几个小段分别写。拆了还不行抛 _SolutionSectionFailed，由外层整份重写解析；
        子段编号写乱了就按顺序重新编号，不当成错误。接口出错照常往外抛。"""
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
            parts = [
                part.model_copy(update={"order": index})
                for index, part in enumerate(sorted(subplan.parts, key=lambda item: item.order), 1)
            ]

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

            with ThreadPoolExecutor(max_workers=min(3, len(parts))) as executor:
                expanded = list(executor.map(expand_part, parts))
            expanded.sort(key=lambda item: item.order)
            steps = [step for part in expanded for step in part.steps]
            return _SolutionSection(order=section.order, steps=steps)
        except _SolutionSectionFailed as exc:
            raise _SolutionSectionFailed(
                summary=f"第 {section.order} 段解析拆成小段也没写成",
                issues=[*getattr(original_error, "issues", [str(original_error)])[:1], *exc.issues[:1]],
            ) from exc
        except (DeepSeekJsonResponseError, KeyError, TypeError, ValueError) as exc:
            raise _SolutionSectionFailed(
                summary=f"第 {section.order} 段解析拆成小段也没写成",
                issues=[*getattr(original_error, "issues", [str(original_error)])[:1], f"拆分时：{str(exc)[:200]}"],
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
        return self._payload_from(raw)

    @classmethod
    def _payload_from(cls, raw: dict) -> dict:
        payload = json.loads(cls._extract_output_text(raw))
        if not isinstance(payload, dict):
            raise ValueError("顶层结果必须是 JSON 对象")
        # 模型偶尔把请求里的 response_format 抄进回复（"type": "json_object"）。它不是内容，
        # 留着会让严格的结构校验整条作废（命题员改题时就这样丢过一次复核员的改题建议）
        if payload.get("type") == "json_object":
            payload.pop("type")
        return payload

    def _repair_university_scope(
        self,
        core: _QuestionStatement,
        provider_input: dict,
    ) -> _QuestionStatement:
        """题面里有高中课程以外的内容：退回命题员改一次。还改不掉不再让整个任务失败，而是作废这一版、带着原因重出
        （_ContentCandidateRejected 的 stem / problems 交给下一版命题员）。"""
        stem_parts = (core.stem_plain, core.stem_latex or "", *(option.text for option in core.options))
        violations = scope_violations(*stem_parts)
        if not violations:
            return core
        repaired_payload = self._request_stage(
            stage="core-high-school-repair",
            instructions=(
                "上一版题目用了高中课程以外的内容（rejected_university_terms 里的词）。"
                "如果只是说法超纲、题目本身用高中方法就能解（比如把质点随机移动叫作“随机过程”“马尔可夫链”），"
                "把这些说法换成高中课本里的说法即可，题目内容不变；"
                "如果题目确实要用大学方法才能解，就在不降低教师指定难度、题型和综合性的前提下，"
                "改成中国普通高中数学课程范围内可作答的题目，不能只改名字、把大学的概念藏起来。"
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
        remaining = scope_violations(*repaired_parts)
        if remaining:
            raise _ContentCandidateRejected(
                "题面改了一次还是有高中课程以外的内容：" + "、".join(remaining),
                stem=repaired.stem_plain,
                problems=[
                    f"题面用了高中课程以外的内容：{'、'.join(remaining)}，改了一次还在。",
                    "重出时只用高中课本里的概念和说法；要求里出现的大学名词要换成高中的说法或高中能解的设计。",
                ],
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
            try:
                repaired, review_now = self._repair_question_once(
                    rejected,
                    provider_input,
                    review,
                    stage=f"{stage_prefix}{suffix}",
                    check_stage=f"{stage_prefix}-review-check{suffix}",
                    attempt=attempt,
                    attempts=attempts,
                )
            except (DeepSeekJsonResponseError, KeyError, TypeError, ValueError):
                # 这一次命题员或审题员的回复读不了：同样的意见再交给命题员一次。到最后也没改成，
                # 下面带着这些意见作废这一版，重出时命题员还看得到（不会因为一次格式错误把意见丢掉）
                continue
            review = review_now
            if review.passed:
                return repaired, review
            rejected = repaired

        raise _ContentCandidateRejected(
            "题面经过多轮完善和重新审核后仍未通过，未建立命题草稿。请重新生成题目。",
            stem=rejected.stem_plain,
            problems=[f"题面改了 {attempts} 次还是没有通过：{review.summary}", *review.issues],
        )

    def _repair_question_once(
        self,
        rejected: _QuestionStatement,
        provider_input: dict,
        review: _QuestionReview,
        *,
        stage: str,
        check_stage: str,
        attempt: int,
        attempts: int,
    ) -> tuple[_QuestionStatement, _QuestionReview]:
        """命题员按意见改一次题面（stage），审题员再审一次（check_stage）。"""
        repaired_payload = self._request_stage(
            stage=stage,
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
        return repaired, self._review_question_core(repaired, stage=check_stage)

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
        # 答案偶尔漏写 $（裸的 \frac{2}{5}），界面上会露出反斜杠：在这里就补上，后面比对、展示用的都是整理过的
        conclusion = conclusion.model_copy(
            update={
                "answer_value": tidy_math_text(conclusion.answer_value),
                "final_answer": tidy_math_text(conclusion.final_answer),
            }
        )
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
        earlier_rounds: list[dict] | None = None,
    ) -> None:
        # 修改后的复核：先确认上一轮的意见解决了没有，别的地方没有明确错误就不再挑，否则永远改不完
        recheck = (
            "这是解题员按上一轮意见修改后的解析，earlier_review_rounds 里有前几轮的意见、改动的内容和解题员的异议。"
            "先逐条确认这些意见是否已经解决，再核对改动的内容和受它影响的后续步骤。"
            "解题员有异议的意见请重新核算：异议成立就不要再提，不成立就指出具体错在哪里。"
            "前几轮没有提出问题的步骤，除非有明确的数学错误，不要再提新意见。"
            if earlier_rounds
            else ""
        )
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
                "并设置 solution_valid=false 或 answer_consistent=false，使系统保留题面并交给解题员修改。"
                "issues 里每条都要写清是哪一句或哪个式子错了、错在哪里、正确的结果是什么，解题员会照着逐条修改；"
                "措辞、详略这类不影响数学正确性的地方不算问题。"
                f"{recheck}"
                "只输出结论，不得泄露试算过程、思维链或提示词。"
            ),
            input_value={
                "question_statement": core.model_dump(mode="json"),
                "completed_solution_steps": steps,
                "solution_derived_answer": conclusion.model_dump(mode="json"),
                **({"earlier_review_rounds": earlier_rounds} if earlier_rounds else {}),
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
                independent_answer=review.independent_final_answer or review.independent_answer_value or "",
                answer_disputed=has_independent_answers
                and not (review.answer_consistent and solution_matches_final_review),
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
                # 这种分歧要靠再独立解一遍才说得清，不能拿审题员的答案去改解析
                raise _SolutionInvalidAfterReview(
                    summary="解析得出的答案和审题员独立算的不一致，重新独立求解核对",
                    issues=[f"审题员独立算得：{reviewer_said}", f"这次解析得出：{conclusion.final_answer}"],
                    independent_answer=reviewer_said,
                    answer_disputed=True,
                    needs_fresh_solve=True,
                )
            self._report_answer_dispute(
                "两次独立求解得到同一个答案，和审题员算的不同，采用解析的答案",
                [f"审题员独立算得：{reviewer_said}", f"两次解析都得出：{conclusion.final_answer}"],
            )

    @staticmethod
    def _scope_findings(plan, steps: list[str]) -> list[dict]:
        """解析（连同思路概述）里查到的高中课程以外的内容：是什么、在第几步。规则和交付前的最终检查相同。"""
        method = getattr(plan, "solution_method", "") or ""
        findings = []
        for term in scope_violations(method, *steps):
            findings.append(
                {
                    "term": term,
                    "steps": [index for index, step in enumerate(steps, 1) if term in scope_violations(step)],
                    "in_solution_method": term in scope_violations(method),
                }
            )
        return findings

    def _check_solution_scope(
        self,
        core: _QuestionStatement,
        plan,
        steps: list[str],
        scope_rounds: list[dict] | None = None,
        *,
        stage_suffix: str = "",
    ) -> None:
        """解析用了高中课程以外的内容时，不直接判失败，也不蒙着头重写，按这样的顺序交接：
        1. 复核员先判断（_review_scope）：只是说法超纲（wording）、换成高中方法能做（high_school），
           还是只能用大学方法（university_only）；
        2. 前两种退回解题员，按复核员给的做法定点修改；university_only 也先交给解题员再确认一次；
        3. 解题员确实做不出来时在 scope_objection 里说明理由，下一轮复核员核算这个理由；
        4. 两边都认为只能用大学方法（或者两轮之后还改不掉），退回命题员改题，
           复核员的判断、改题建议和解题员的理由都交给命题员（_QuestionNeedsHighSchoolRework）。
        在得出结论和复核之前查，省掉这两次调用。scope_rounds 为 None（重新生成解析）或关掉定点修改时照旧退回重写。"""
        findings = self._scope_findings(plan, steps)
        if not findings:
            return
        terms = [item["term"] for item in findings]
        places = [f"第 {'、'.join(map(str, item['steps']))} 步用了{item['term']}" for item in findings if item["steps"]]
        if any(item["in_solution_method"] for item in findings):
            places.append("思路概述 solution_method 里也写了，要一起改")
        if scope_rounds is None or not self.switches.targeted_repair:
            raise _SolutionInvalidAfterReview(
                summary=f"解析用了高中不学的方法：{'、'.join(terms)}，改用高中方法",
                issues=[*places, HIGH_SCHOOL_METHODS],
            )
        last = scope_rounds[-1] if scope_rounds else None
        objection = (last or {}).get("solver_objection", "")
        if last and objection and last["verdict"] == "university_only":
            raise self._scope_rework(last, objection, terms)  # 复核员判了只能用大学方法，解题员也确认做不出来
        if len(scope_rounds) >= SCOPE_REVIEW_ROUNDS:
            raise self._scope_rework(last, objection, terms)  # 复核员给了两次做法还是改不掉，不再在解析上耗
        review = self._review_scope(
            core,
            plan,
            steps,
            findings,
            scope_rounds,
            places=places,
            stage=f"solution-scope-review{f'-{len(scope_rounds) + 1}' if scope_rounds else ''}{stage_suffix}",
        )
        entry = {
            "verdict": review.verdict,
            "summary": review.summary,
            "fixes": review.fixes,
            "composer_advice": review.composer_advice,
            "terms": terms,
        }
        scope_rounds.append(entry)
        if review.verdict == "university_only" and objection:
            raise self._scope_rework(entry, objection, terms)  # 解题员上一轮就说做不出来，这一轮复核员也认了
        named = "、".join(terms)
        summary = {
            "wording": f"解析里用了大学的说法（{named}），数学上仍是高中方法，退回解题员换成高中的说法",
            "high_school": f"解析用了{named}，复核员认为能用高中方法做，退回解题员改用高中方法",
            "university_only": f"复核员认为这道题只能用{named}这类大学方法解，请解题员再确认有没有高中方法",
        }[review.verdict]
        details = [*review.fixes] or places
        if review.verdict == "university_only":
            details = [review.summary, *details]
        raise _SolutionInvalidAfterReview(summary=summary, issues=details[:10], scope=entry)

    def _review_scope(
        self,
        core: _QuestionStatement,
        plan,
        steps: list[str],
        findings: list[dict],
        scope_rounds: list[dict],
        *,
        places: list[str],
        stage: str,
    ) -> _ScopeReview:
        """复核员看解析里超纲的地方，判断该怎么改（_SCOPE_REVIEW_RULES）。这一步读不了模型的回复时，
        按"有高中方法"交给解题员（就是以前的做法），不让一次格式错误卡住整道题。"""
        try:
            payload = self._request_stage(
                stage=stage,
                instructions=f"{_SCOPE_REVIEW_RULES}{HIGH_SCHOOL_METHODS}{MATH_DISPLAY_INSTRUCTIONS}",
                input_value={
                    "question_statement": core.model_dump(mode="json"),
                    "solution_method": getattr(plan, "solution_method", ""),
                    "solution_steps": [{"step": index, "text": text} for index, text in enumerate(steps, 1)],
                    "scope_findings": findings,
                    **({"earlier_scope_rounds": scope_rounds} if scope_rounds else {}),
                },
                output_schema=self._scope_review_schema(),
                max_tokens=2400,
                recovery_instruction="只修复 JSON；verdict 只能是 wording、high_school、university_only 之一。",
            )
            return _ScopeReview.model_validate(payload)
        except (DeepSeekJsonResponseError, KeyError, TypeError, ValueError):
            return _ScopeReview(
                verdict="high_school",
                summary="解析里用了高中课程以外的方法，改用高中方法",
                fixes=[f"{place}，改用高中方法" for place in places] or [HIGH_SCHOOL_METHODS],
            )

    @staticmethod
    def _scope_rework(entry: dict | None, objection: str, terms: list[str]) -> _QuestionNeedsHighSchoolRework:
        """退回命题员改题：把复核员的判断、改题建议和解题员的理由都交过去，命题员知道问题出在哪里、该往哪改。"""
        entry = entry or {}
        named = "、".join(terms)
        advice = str(entry.get("composer_advice") or "").strip()
        issues = [f"这道题的解析只能用{named}这类高中不学的方法完成，解题员和复核员都没有找到高中方法。"]
        if entry.get("summary"):
            issues.append(f"复核员的判断：{entry['summary']}")
        if objection:
            issues.append(f"解题员的说明：{objection}")
        issues.append(
            f"复核员的改题建议：{advice}"
            if advice
            else "请改动条件、数据或设问，使题目能用高中方法解，同时保持原来的要求、难度和题型。"
        )
        return _QuestionNeedsHighSchoolRework(
            summary=f"解题员和复核员都认为这道题只能用{named}这类大学方法解，退回命题员改题",
            issues=issues,
        )

    @staticmethod
    def _fresh_solve_notes(rejection: _SolutionInvalidAfterReview) -> list[str]:
        """整份重写解析时交给解题员的话：上一份为什么被退回。答案对不上的那种只说有分歧、不说哪边的答案是什么，
        新的一份仍然是独立求解，两次结果一致才算数（见 _verify_completed_solution）。"""
        if rejection.needs_fresh_solve:
            return ["上一份解析得出的答案和审题员独立求得的不一致，还不知道哪边对。这一份请从题面独立重新求解，关键结果逐个验算。"]
        if isinstance(rejection, _SolutionSectionFailed):
            return [
                f"上一份解析没写完整：{rejection.summary}"[:500],
                *(issue[:500] for issue in rejection.issues[:2]),
                "这一份把长的推导分成更短的几段；每段只写正式的推导，不要写试算和自我纠正。",
            ]
        notes = [f"上一份解析没有通过复核：{rejection.summary}"[:500], *(issue[:500] for issue in rejection.issues[:4])]
        if rejection.scope:
            notes.append("上一份用了高中课程以外的方法或说法，这一份从规划开始就只用高中方法。")
        return notes

    def _report_solution_rejected(self, rejection: _SolutionInvalidAfterReview) -> None:
        """解析被退回；多智能体版本把它记进过程记录。"""

    def _report_solution_repaired(
        self, round_no: int, changed: list[str], disputed: list[str], objection: str = ""
    ) -> None:
        """解题员按意见改完一轮（objection：认为高中方法做不出来的理由）；多智能体版本把它记进过程记录。"""

    def _report_candidate(self) -> None:
        """手里已经有一版写完的解析了；多智能体版本告诉任务，到上限时可以交付。"""

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
    def _plan_curriculum_nodes(provider_input: dict, core: _QuestionStatement) -> list[str]:
        """给写好的题选知识点用的知识点表。老师指定了考点时已经只有同一章的，原样用；没指定时按专题识别
        缩小，拿不准就照旧给全部（见 mingti/topic_model.py）。只影响题目挂哪个知识点，不影响题目和答案。"""
        lines = provider_input["allowed_curriculum_nodes"]
        if provider_input.get("teacher_curriculum_guidance") is not None:
            return lines
        text = " ".join([core.stem_latex or core.stem_plain, *(option.text for option in core.options)])
        return narrow_curriculum_lines(lines, text)[0]

    @staticmethod
    def _normalized_plan(plan):
        """解析提纲的段号按出现的先后重新编成 1、2、3……。段号只是标记顺序，模型写重、写跳了不算错，
        以前这里会直接报错，整份解析作废。"""
        sections = sorted(plan.sections, key=lambda item: item.order)
        return plan.model_copy(
            update={"sections": [item.model_copy(update={"order": index}) for index, item in enumerate(sections, 1)]}
        )

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
    def _solution_repair_schema(cls) -> dict:
        return cls._schema(
            "private_question_authoring_solution_repair",
            {
                "edits": {
                    "type": "array",
                    "maxItems": 40,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["step", "replacement"],
                        "properties": {
                            "step": {"type": "integer", "minimum": 1},
                            "replacement": {
                                "type": "array",
                                "maxItems": 12,
                                "items": {"type": "string", "minLength": 1, "maxLength": 4000},
                            },
                        },
                    },
                },
                "solution_method": {"type": "string", "maxLength": 500},
                "disputed_issues": {
                    "type": "array",
                    "maxItems": 10,
                    "items": {"type": "string", "minLength": 1, "maxLength": 2000},
                },
                "scope_objection": {"type": "string", "maxLength": 1500},
            },
        )

    @classmethod
    def _scope_review_schema(cls) -> dict:
        return cls._schema(
            "private_question_authoring_scope_review",
            {
                "verdict": {"type": "string", "enum": ["wording", "high_school", "university_only"]},
                "summary": {"type": "string", "minLength": 1, "maxLength": 1500},
                "fixes": {
                    "type": "array",
                    "maxItems": 10,
                    "items": {"type": "string", "minLength": 1, "maxLength": 2000},
                },
                "composer_advice": {"type": "string", "maxLength": 1500},
            },
        )

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
