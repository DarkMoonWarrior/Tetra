from __future__ import annotations

from pydantic import BaseModel, Field


class CurriculumNode(BaseModel):
    node_id: str
    parent_id: str | None = None
    volume: str
    node_type: str
    code: str
    name: str
    description: str = ""
    prerequisite_node_ids: list[str] = Field(default_factory=list)
    primary_competencies: list[str] = Field(default_factory=list)
    typical_question_types: list[str] = Field(default_factory=list)
    common_errors: list[str] = Field(default_factory=list)
    gaokao_priority: str
    status: str
    reviewed_by: str


class CurriculumTreeNode(CurriculumNode):
    children: list["CurriculumTreeNode"] = Field(default_factory=list)


class CurriculumSearchItem(BaseModel):
    node_id: str
    code: str
    name: str
    node_type: str
    volume: str
    chapter: str | None = None
    section: str | None = None
    description: str = ""
    primary_competencies: list[str] = Field(default_factory=list)
    gaokao_priority: str
    match_score: float = Field(ge=0, le=1)


class CurriculumSearchResponse(BaseModel):
    query: str
    total: int
    items: list[CurriculumSearchItem]
