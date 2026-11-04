from __future__ import annotations

import re
import sqlite3
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from types import TracebackType
from typing import Mapping, Protocol
from uuid import uuid4

from mingti.model_operations.schemas import (
    FeatureRunStats,
    ModelOperationsDashboard,
    ModelRouteStatus,
    ModelRunStats,
    ModelRunView,
)


FEATURE_LABELS = {
    "lesson_plan_generation": "教案生成",
    "lesson_block_rewrite": "教案局部改写",
    "question_variant": "题目变式",
    "question_variant_polish": "教师变式润色",
    "question_authoring": "私有辅助命题",
    "solution_assistant": "解题助手",
    "private_resource_ocr": "私人资料 OCR",
    "private_question_ocr": "私人单题 OCR",
}


class ModelRunSpan(Protocol):
    def capture_response(self, payload: dict) -> None: ...


class ModelRunRecorder(Protocol):
    def track(
        self,
        *,
        feature: str,
        provider: str,
        model: str,
        prompt_version: str,
        actor_id: str | None = None,
        validation_owner_id: str | None = None,
    ) -> AbstractContextManager[ModelRunSpan]: ...


class ModelUsageSink(Protocol):
    def record_model_usage(
        self,
        *,
        owner_id: str,
        feature: str,
        task_ref: str,
        model_cost_usd: Decimal,
    ) -> object: ...


class NullModelRunSpan:
    def capture_response(self, payload: dict) -> None:
        return None


class NullModelRunRecorder:
    def track(
        self,
        *,
        feature: str,
        provider: str,
        model: str,
        prompt_version: str,
        actor_id: str | None = None,
        validation_owner_id: str | None = None,
    ) -> "_NullModelRunContext":
        return _NullModelRunContext()


class _NullModelRunContext:
    def __enter__(self) -> NullModelRunSpan:
        return NullModelRunSpan()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        return False


class _SQLiteModelRunSpan:
    def __init__(
        self,
        registry: "ModelOperationsRegistry",
        *,
        feature: str,
        provider: str,
        model: str,
        prompt_version: str,
        actor_id: str | None,
        validation_owner_id: str | None,
    ) -> None:
        self.registry = registry
        self.feature = feature
        self.provider = provider
        self.model = model
        self.prompt_version = prompt_version
        self.actor_id = actor_id
        self.validation_owner_id = validation_owner_id
        self.started_at = perf_counter()
        self.created_at = datetime.now(UTC)
        self.usage = {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }

    def __enter__(self) -> "_SQLiteModelRunSpan":
        return self

    def capture_response(self, payload: dict) -> None:
        usage = payload.get("usage") or {}
        input_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
        self.usage = {
            "input_tokens": int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
            "cached_input_tokens": int(
                input_details.get("cached_tokens")
                or usage.get("prompt_cache_hit_tokens")
                or 0
            ),
            "output_tokens": int(usage.get("output_tokens") or usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
        }
        if not self.usage["total_tokens"]:
            self.usage["total_tokens"] = (
                self.usage["input_tokens"] + self.usage["output_tokens"]
            )

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        latency_ms = max(0, round((perf_counter() - self.started_at) * 1000))
        self.registry._finish(
            feature=self.feature,
            provider=self.provider,
            model=self.model,
            prompt_version=self.prompt_version,
            actor_id=self.actor_id,
            validation_owner_id=self.validation_owner_id,
            created_at=self.created_at,
            latency_ms=latency_ms,
            usage=self.usage,
            error=exc,
        )
        return False


class ModelOperationsRegistry:
    """Persistent observability seam for all model-backed adapters."""

    def __init__(
        self,
        database_path: Path,
        *,
        input_rate: float | None = None,
        cached_input_rate: float | None = None,
        output_rate: float | None = None,
        provider_rates: Mapping[str, tuple[float, float, float]] | None = None,
        usage_sink: ModelUsageSink | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.input_rate = input_rate
        self.cached_input_rate = cached_input_rate
        self.output_rate = output_rate
        self.provider_rates = dict(provider_rates or {})
        if all(rate is not None for rate in (input_rate, cached_input_rate, output_rate)):
            self.provider_rates.setdefault(
                "openai",
                (float(input_rate), float(cached_input_rate), float(output_rate)),
            )
        self.usage_sink = usage_sink
        self._initialize()

    @property
    def pricing_configured(self) -> bool:
        return bool(self.provider_rates)

    def track(
        self,
        *,
        feature: str,
        provider: str,
        model: str,
        prompt_version: str,
        actor_id: str | None = None,
        validation_owner_id: str | None = None,
    ) -> _SQLiteModelRunSpan:
        return _SQLiteModelRunSpan(
            self,
            feature=feature,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
            actor_id=actor_id,
            validation_owner_id=validation_owner_id,
        )

    def dashboard(
        self,
        *,
        api_configured: bool,
        model: str,
        reasoning_effort: str,
        timeout_seconds: int,
        lesson_plan_provider: str,
        question_variant_provider: str,
        solution_provider: str,
        external_provider: str = "openai",
        provider_configuration: dict[str, tuple[bool, str]] | None = None,
        ocr_api_configured: bool | None = None,
        limit: int = 50,
    ) -> ModelOperationsDashboard:
        runs = self.list_runs(limit=limit)
        stats = self._aggregate(runs=None)
        feature_stats = [
            self._aggregate_feature(feature)
            for feature in FEATURE_LABELS
        ]
        provider_configuration = provider_configuration or {
            external_provider: (api_configured, model)
        }
        routes = [
            self._route("lesson_plan_generation", "教案生成", lesson_plan_provider, provider_configuration, local_provider="local_template", external_provider=external_provider),
            self._route("lesson_block_rewrite", "教案局部改写", lesson_plan_provider, provider_configuration, local_provider="local_template", external_provider=external_provider),
            self._route("question_variant", "题目变式", question_variant_provider, provider_configuration, local_provider="local_rule", external_provider=external_provider),
            self._route("solution_assistant", "解题助手", solution_provider, provider_configuration, local_provider="verified_answer", external_provider=external_provider),
            ModelRouteStatus(
                feature="private_resource_ocr",
                label="私人资料 OCR",
                configured_mode="openai",
                effective_provider="openai" if (ocr_api_configured if ocr_api_configured is not None else api_configured) else "unavailable",
                model=provider_configuration.get("openai", (False, "—"))[1] if (ocr_api_configured if ocr_api_configured is not None else api_configured) else "—",
                ready=ocr_api_configured if ocr_api_configured is not None else api_configured,
                note="仅在教师明确同意外部处理后调用" if (ocr_api_configured if ocr_api_configured is not None else api_configured) else "需要配置 OpenAI API Key 或使用可视化重建",
            ),
        ]
        return ModelOperationsDashboard(
            api_configured=api_configured,
            model=model,
            reasoning_effort=reasoning_effort,
            timeout_seconds=timeout_seconds,
            pricing_configured=self.pricing_configured,
            pricing_note=(
                "仅按已配置供应商的环境费率快照估算，未配置供应商不计入成本"
                if self.pricing_configured
                else "尚未配置费率快照，暂只统计 token 用量"
            ),
            routes=routes,
            stats=stats,
            feature_stats=feature_stats,
            recent_runs=runs,
        )

    def list_runs(self, *, limit: int = 50) -> list[ModelRunView]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM model_runs ORDER BY created_at DESC LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
        return [self._row_to_view(row) for row in rows]

    def _finish(
        self,
        *,
        feature: str,
        provider: str,
        model: str,
        prompt_version: str,
        actor_id: str | None,
        validation_owner_id: str | None,
        created_at: datetime,
        latency_ms: int,
        usage: dict[str, int],
        error: BaseException | None,
    ) -> None:
        estimated_cost = self._estimate_cost(provider=provider, usage=usage)
        error_message = self._safe_error(error) if error else None
        run_id = f"run_{uuid4().hex}"
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO model_runs (
                    run_id, feature, provider, model, prompt_version, status,
                    input_tokens, cached_input_tokens, output_tokens, total_tokens,
                    latency_ms, estimated_cost_usd, error_category, error_message,
                    actor_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, feature, provider, model, prompt_version,
                    "failed" if error else "succeeded",
                    usage["input_tokens"], usage["cached_input_tokens"],
                    usage["output_tokens"], usage["total_tokens"], latency_ms,
                    estimated_cost, error.__class__.__name__ if error else None,
                    error_message, actor_id, created_at.isoformat(),
                ),
            )
        if validation_owner_id and estimated_cost is not None and estimated_cost > 0:
            try:
                if self.usage_sink is not None:
                    self.usage_sink.record_model_usage(
                        owner_id=validation_owner_id,
                        feature=feature,
                        task_ref=run_id,
                        model_cost_usd=Decimal(str(estimated_cost)),
                    )
            except Exception:
                # Validation telemetry is fail-open by contract: a metric sink
                # must never turn a successful model-backed product task into
                # a user-visible failure.
                pass

    def _estimate_cost(self, *, provider: str, usage: dict[str, int]) -> float | None:
        if provider.startswith("local_") or provider in {"verified_answer", "local_rule"}:
            return 0.0
        rates = self.provider_rates.get(provider)
        if rates is None:
            return None
        input_rate, cached_input_rate, output_rate = rates
        uncached_input = max(0, usage["input_tokens"] - usage["cached_input_tokens"])
        cost = (
            uncached_input * input_rate
            + usage["cached_input_tokens"] * cached_input_rate
            + usage["output_tokens"] * output_rate
        ) / 1_000_000
        return round(cost, 8)

    def _aggregate(self, runs: list[ModelRunView] | None) -> ModelRunStats:
        del runs
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) total_runs,
                       SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) succeeded_runs,
                       SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) failed_runs,
                       COALESCE(SUM(input_tokens), 0) input_tokens,
                       COALESCE(SUM(cached_input_tokens), 0) cached_input_tokens,
                       COALESCE(SUM(output_tokens), 0) output_tokens,
                       COALESCE(SUM(total_tokens), 0) total_tokens,
                       COALESCE(AVG(latency_ms), 0) average_latency_ms,
                       SUM(estimated_cost_usd) estimated_cost_usd
                FROM model_runs
                """
            ).fetchone()
        total = int(row["total_runs"] or 0)
        succeeded = int(row["succeeded_runs"] or 0)
        return ModelRunStats(
            total_runs=total,
            succeeded_runs=succeeded,
            failed_runs=int(row["failed_runs"] or 0),
            success_rate=round(succeeded / total * 100, 1) if total else 0,
            input_tokens=int(row["input_tokens"]),
            cached_input_tokens=int(row["cached_input_tokens"]),
            output_tokens=int(row["output_tokens"]),
            total_tokens=int(row["total_tokens"]),
            average_latency_ms=round(float(row["average_latency_ms"])),
            estimated_cost_usd=(
                round(float(row["estimated_cost_usd"]), 8)
                if self.pricing_configured and row["estimated_cost_usd"] is not None
                else None
            ),
        )

    def _aggregate_feature(self, feature: str) -> FeatureRunStats:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) total_runs,
                       SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) succeeded_runs,
                       SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) failed_runs,
                       COALESCE(AVG(latency_ms), 0) average_latency_ms,
                       COALESCE(SUM(total_tokens), 0) total_tokens,
                       SUM(estimated_cost_usd) estimated_cost_usd
                FROM model_runs WHERE feature = ?
                """,
                (feature,),
            ).fetchone()
        return FeatureRunStats(
            feature=feature,
            label=FEATURE_LABELS.get(feature, feature),
            total_runs=int(row["total_runs"] or 0),
            succeeded_runs=int(row["succeeded_runs"] or 0),
            failed_runs=int(row["failed_runs"] or 0),
            average_latency_ms=round(float(row["average_latency_ms"] or 0)),
            total_tokens=int(row["total_tokens"] or 0),
            estimated_cost_usd=(
                round(float(row["estimated_cost_usd"]), 8)
                if self.pricing_configured and row["estimated_cost_usd"] is not None
                else None
            ),
        )

    @staticmethod
    def _route(
        feature: str,
        label: str,
        configured_mode: str,
        provider_configuration: dict[str, tuple[bool, str]],
        *,
        local_provider: str,
        external_provider: str = "openai",
    ) -> ModelRouteStatus:
        selected_provider = configured_mode if configured_mode in {"openai", "deepseek"} else external_provider
        configured, selected_model = provider_configuration.get(selected_provider, (False, "—"))
        wants_external = configured_mode in {"openai", "deepseek"} or (
            configured_mode == "auto" and configured
        )
        effective = selected_provider if wants_external else local_provider
        ready = not (configured_mode in {"openai", "deepseek"} and not configured)
        return ModelRouteStatus(
            feature=feature,
            label=label,
            configured_mode=configured_mode,
            effective_provider=effective if ready else "unavailable",
            model=selected_model if wants_external and ready else local_provider,
            ready=ready,
            note=(
                "已连接外部模型"
                if wants_external and ready
                else "显式选择外部模型，但尚未配置对应 API Key"
                if not ready
                else "当前使用本地确定性能力"
            ),
        )

    @staticmethod
    def _safe_error(error: BaseException) -> str:
        message = str(error).replace("\n", " ").replace("\r", " ")
        message = re.sub(r"sk-[A-Za-z0-9_-]+", "[REDACTED]", message)
        message = re.sub(r"Bearer\s+\S+", "Bearer [REDACTED]", message, flags=re.I)
        return message[:240]

    def _row_to_view(self, row: sqlite3.Row) -> ModelRunView:
        return ModelRunView(
            run_id=row["run_id"],
            feature=row["feature"],
            feature_label=FEATURE_LABELS.get(row["feature"], row["feature"]),
            provider=row["provider"],
            model=row["model"],
            prompt_version=row["prompt_version"],
            status=row["status"],
            input_tokens=row["input_tokens"],
            cached_input_tokens=row["cached_input_tokens"],
            output_tokens=row["output_tokens"],
            total_tokens=row["total_tokens"],
            latency_ms=row["latency_ms"],
            estimated_cost_usd=row["estimated_cost_usd"],
            error_category=row["error_category"],
            error_message=row["error_message"],
            actor_id=row["actor_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS model_runs (
                    run_id TEXT PRIMARY KEY,
                    feature TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    latency_ms INTEGER NOT NULL,
                    estimated_cost_usd REAL,
                    error_category TEXT,
                    error_message TEXT,
                    actor_id TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_model_runs_created_at ON model_runs(created_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_model_runs_feature ON model_runs(feature, created_at DESC)"
            )
