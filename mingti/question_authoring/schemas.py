from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


QuestionType = Literal[
    "single_choice",
    "multiple_choice",
    "fill_blank",
    "open_response",
]
GradeLevel = Literal["高一", "高二", "高三"]
QuestionDraftTrustCheckCode = Literal[
    "solvability",
    "answer_solution_consistency",
    "choice_answer_cardinality",
    "distractor_quality",
    "math_verifier",
    "difficulty_disclosure",
]
QuestionDraftTrustCheckStatus = Literal[
    "passed",
    "failed",
    "inconclusive",
    "not_applicable",
]
QuestionDraftTrustEvidenceSource = Literal[
    "deterministic_structure",
    "math_verifier",
    "model_estimate",
    "none",
]
# open_review_issues：解析写完了，但返工到上限复核还有意见没解决。照常交付，solution_issue 写明这些意见
QuestionSolutionStatus = Literal["ready", "needs_teacher_verification", "open_review_issues"]


class QuestionDesignBrief(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_text: str = Field(min_length=5, max_length=4000)
    grade: GradeLevel | None = None
    curriculum_node_id: str | None = Field(default=None, max_length=160)
    knowledge_point_query: str | None = Field(default=None, max_length=300)
    question_type: QuestionType | None = None
    target_difficulty: int = Field(default=3, ge=1, le=5)
    teaching_purpose: str = Field(default="课堂练习", max_length=300)
    expected_minutes: int | None = Field(default=None, ge=1, le=120)
    assessed_method: str = Field(default="", max_length=1000)
    misconception_requirements: str = Field(default="", max_length=1000)
    additional_constraints: str = Field(default="", max_length=2000)
    reference_question_id: str | None = Field(default=None, max_length=160)


class QuestionDraftOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=12)
    text: str = Field(min_length=1, max_length=4000)


class QuestionAuthoringStatementPreview(BaseModel):
    """Answer-free statement exposed only after the statement review passes."""

    model_config = ConfigDict(extra="forbid")

    question_type: QuestionType
    stem_plain: str = Field(min_length=5, max_length=20000)
    stem_latex: str | None = Field(default=None, max_length=20000)
    options: list[QuestionDraftOption] = Field(default_factory=list, max_length=20)
    estimated_difficulty: int = Field(ge=1, le=5)


class QuestionDraftCurriculum(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    name: str
    volume: str
    chapter: str
    section: str
    mapping_source: Literal["teacher_selected", "brief_match", "agent_inferred"]
    mapping_status: Literal[
        "teacher_confirmed",
        "pending_teacher_confirmation",
    ] = "pending_teacher_confirmation"
    match_confidence: float | None = Field(default=None, ge=0, le=1)
    curriculum_status: str
    reviewed_by: str


class QuestionDraftTrustCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: QuestionDraftTrustCheckCode
    status: QuestionDraftTrustCheckStatus
    capability: Literal["supported", "unsupported"]
    evidence_source: QuestionDraftTrustEvidenceSource
    summary: str = Field(min_length=1, max_length=500)
    details: list[str] = Field(default_factory=list, max_length=20)


class QuestionDraftVerification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["checks_failed", "teacher_review_required"]
    math_verifier_status: Literal[
        "unsupported",
        "passed",
        "failed",
        "inconclusive",
    ]
    math_verifier_checked: bool
    teacher_review_required: Literal[True]
    verifier_version: str = Field(min_length=1, max_length=120)
    checks: list[QuestionDraftTrustCheck] = Field(min_length=6, max_length=6)
    details: list[str] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_trust_contract(self) -> "QuestionDraftVerification":
        expected = {
            "solvability",
            "answer_solution_consistency",
            "choice_answer_cardinality",
            "distractor_quality",
            "math_verifier",
            "difficulty_disclosure",
        }
        codes = [check.code for check in self.checks]
        if set(codes) != expected or len(codes) != len(set(codes)):
            raise ValueError("可信检查必须恰好包含六个唯一检查项")
        has_failure = any(check.status == "failed" for check in self.checks)
        if (self.status == "checks_failed") != has_failure:
            raise ValueError("可信检查总状态与阻断项不一致")
        math_check = next(check for check in self.checks if check.code == "math_verifier")
        if self.math_verifier_status == "unsupported":
            if self.math_verifier_checked or math_check.capability != "unsupported":
                raise ValueError("MathVerifier unsupported 状态不得冒充已运行")
        elif self.math_verifier_status in {"passed", "failed"}:
            if not self.math_verifier_checked or math_check.status != self.math_verifier_status:
                raise ValueError("MathVerifier 结果与检查证据不一致")
        return self


class QuestionDraftEditableContent(BaseModel):
    """Teacher-editable fields; provenance and trust evidence stay server-owned."""

    model_config = ConfigDict(extra="forbid")

    question_type: QuestionType
    stem_plain: str = Field(min_length=5, max_length=20000)
    stem_latex: str | None = Field(default=None, max_length=20000)
    options: list[QuestionDraftOption] = Field(default_factory=list, max_length=20)
    answer_value: str = Field(min_length=1, max_length=4000)
    solution_method: str = Field(min_length=1, max_length=500)
    solution_steps: list[str] = Field(min_length=1, max_length=100)
    final_answer: str = Field(min_length=1, max_length=4000)
    authoring_intent: str = Field(min_length=1, max_length=2000)
    knowledge_points: list[str] = Field(default_factory=list, max_length=20)
    assessed_methods: list[str] = Field(default_factory=list, max_length=20)
    common_mistakes: list[str] = Field(default_factory=list, max_length=20)
    curriculum_confirmation_node_id: str | None = Field(default=None, max_length=160)


class GeneratedQuestionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft_id: str
    request_text: str
    question_type: QuestionType
    stem_plain: str = Field(min_length=5, max_length=20000)
    stem_latex: str | None = Field(default=None, max_length=20000)
    options: list[QuestionDraftOption] = Field(default_factory=list, max_length=20)
    answer_value: str = Field(min_length=1, max_length=4000)
    solution_method: str = Field(min_length=1, max_length=500)
    solution_steps: list[str] = Field(min_length=1, max_length=100)
    solution_status: QuestionSolutionStatus = "ready"
    solution_issue: str | None = Field(default=None, max_length=500)
    final_answer: str = Field(min_length=1, max_length=4000)
    authoring_intent: str = Field(min_length=1, max_length=2000)
    curriculum: QuestionDraftCurriculum | None = None
    knowledge_points: list[str] = Field(default_factory=list, max_length=20)
    assessed_methods: list[str] = Field(default_factory=list, max_length=20)
    common_mistakes: list[str] = Field(default_factory=list, max_length=20)
    pedagogy_source: Literal["model_generated", "teacher_edited"] = "model_generated"
    target_difficulty: int = Field(ge=1, le=5)
    estimated_difficulty: int = Field(ge=1, le=5)
    difficulty_source: Literal["model_estimated"] = "model_estimated"
    verification: QuestionDraftVerification
    source_type: Literal["ai_generated"] = "ai_generated"
    visibility: Literal["owner_only"] = "owner_only"
    public_search_allowed: Literal[False] = False
    share_allowed: Literal[False] = False
    commercial_use_allowed: Literal[False] = False
    model_training_allowed: Literal[False] = False
    saved: Literal[False] = False
    provider: str
    model: str
    prompt_revision: str
    warnings: list[str] = Field(default_factory=list)
    created_at: str


class ProviderQuestionDraft(BaseModel):
    """Internal provider output; trust/privacy fields are never model-controlled."""

    model_config = ConfigDict(extra="forbid")

    question_type: QuestionType
    stem_plain: str = Field(min_length=5, max_length=20000)
    stem_latex: str | None = Field(default=None, max_length=20000)
    options: list[QuestionDraftOption] = Field(default_factory=list, max_length=20)
    answer_value: str = Field(min_length=1, max_length=4000)
    solution_method: str = Field(min_length=1, max_length=500)
    solution_steps: list[str] = Field(min_length=1, max_length=100)
    solution_status: QuestionSolutionStatus = "ready"
    solution_issue: str | None = Field(default=None, max_length=500)
    final_answer: str = Field(min_length=1, max_length=4000)
    authoring_intent: str = Field(min_length=1, max_length=2000)
    estimated_difficulty: int = Field(ge=1, le=5)
    curriculum_node_id: str | None = Field(default=None, max_length=160)
    knowledge_points: list[str] = Field(default_factory=list, max_length=20)
    assessed_methods: list[str] = Field(default_factory=list, max_length=20)
    common_mistakes: list[str] = Field(default_factory=list, max_length=20)


class ProviderQuestionSolution(BaseModel):
    """Provider-owned re-solve result, including the answer derived from it."""

    model_config = ConfigDict(extra="forbid")

    solution_method: str = Field(min_length=1, max_length=500)
    solution_steps: list[str] = Field(min_length=1, max_length=100)
    answer_value: str = Field(min_length=1, max_length=4000)
    final_answer: str = Field(min_length=1, max_length=4000)
    solution_status: QuestionSolutionStatus = "ready"
    solution_issue: str | None = Field(default=None, max_length=500)
