from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


ModelRunStatus = Literal["succeeded", "failed"]


class ModelRunView(BaseModel):
    run_id: str
    feature: str
    feature_label: str
    provider: str
    model: str
    prompt_version: str
    status: ModelRunStatus
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int
    estimated_cost_usd: float | None = None
    error_category: str | None = None
    error_message: str | None = None
    actor_id: str | None = None
    created_at: datetime


class ModelRouteStatus(BaseModel):
    feature: str
    label: str
    configured_mode: str
    effective_provider: str
    model: str
    ready: bool
    note: str


class ModelRunStats(BaseModel):
    total_runs: int = 0
    succeeded_runs: int = 0
    failed_runs: int = 0
    success_rate: float = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    average_latency_ms: int = 0
    estimated_cost_usd: float | None = None


class FeatureRunStats(BaseModel):
    feature: str
    label: str
    total_runs: int = 0
    succeeded_runs: int = 0
    failed_runs: int = 0
    average_latency_ms: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float | None = None


class ModelOperationsDashboard(BaseModel):
    api_configured: bool
    model: str
    reasoning_effort: str
    timeout_seconds: int
    pricing_configured: bool
    pricing_note: str
    routes: list[ModelRouteStatus]
    stats: ModelRunStats
    feature_stats: list[FeatureRunStats]
    recent_runs: list[ModelRunView] = Field(default_factory=list)
