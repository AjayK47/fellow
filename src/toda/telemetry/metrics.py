"""Low-cardinality OpenTelemetry metrics for harness operations."""

from __future__ import annotations

from collections import Counter
from threading import Lock
from typing import Any

from opentelemetry.metrics import Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader


_ALLOWED_LABELS = {
    "outcome", "role", "provider", "model", "tool", "operation", "lane", "source", "kind",
}
_FORBIDDEN_LABEL_KEYS = {
    "organization_id", "user_id", "session_id", "task_id", "job_id",
    "channel_id", "prompt_id",
}

# Names are deliberately centralized so dashboards and readiness can verify
# the contract without depending on which workload happened to run first.
REQUIRED_METRICS = frozenset(
    {
        "toda_queue_depth",
        "toda_queue_oldest_ready_job_age_seconds",
        "toda_queue_expired_leases_total",
        "toda_queue_dead_letters_total",
        "toda_queue_wait_duration_ms",
        "toda_retrieval_duration_ms",
        "toda_retrieval_results_total",
        "toda_context_utilization_ratio",
        "toda_context_compactions_total",
        "toda_model_cache_input_tokens_total",
        "toda_model_cache_hit_ratio",
        "toda_model_retries_total",
        "toda_model_timeouts_total",
        "toda_tool_denied_total",
        "toda_tool_approval_required_total",
        "toda_connector_failures_total",
        "toda_run_active_duration_ms",
        "toda_run_wall_duration_ms",
        "toda_task_cost_per_success_usd",
        "toda_completeness_total",
        "toda_feedback_score",
    }
)


class TelemetryMetrics:
    """Metrics facade that rejects identifiers before they reach a label set."""

    def __init__(
        self,
        *,
        otlp_endpoint: str | None = None,
        otlp_headers: dict[str, str] | None = None,
        export_timeout_seconds: float = 10.0,
    ) -> None:
        readers: list[Any] = []
        if otlp_endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                    OTLPMetricExporter,
                )

                endpoint = str(otlp_endpoint).replace("/v1/traces", "/v1/metrics")
                exporter = OTLPMetricExporter(
                    endpoint=endpoint,
                    headers=dict(otlp_headers or {}),
                    timeout=float(export_timeout_seconds),
                )
                readers.append(
                    PeriodicExportingMetricReader(
                        exporter,
                        export_interval_millis=15_000,
                        export_timeout_millis=int(float(export_timeout_seconds) * 1000),
                    )
                )
            except Exception:
                # Local snapshots remain available when the optional
                # collector or exporter package is unavailable.
                pass
        self.provider = MeterProvider(metric_readers=readers)
        meter = self.provider.get_meter("toda")
        self._counters: dict[str, Any] = {}
        self._up_down_counters: dict[str, Any] = {}
        self._histograms: dict[str, Any] = {}
        self._gauges: dict[str, Any] = {}
        self._gauge_values: dict[str, dict[tuple[tuple[str, str], ...], float]] = {}
        self._snapshot: Counter[tuple[str, tuple[tuple[str, str], ...]]] = Counter()
        self._lock = Lock()
        self._meter = meter

    @staticmethod
    def _labels(labels: dict[str, Any] | None) -> dict[str, str]:
        result: dict[str, str] = {}
        for key, value in (labels or {}).items():
            key = str(key)
            lowered = key.lower()
            if key not in _ALLOWED_LABELS or lowered in _FORBIDDEN_LABEL_KEYS:
                raise ValueError(f"high-cardinality metric label is not allowed: {key}")
            if value not in (None, ""):
                result[key] = str(value)
        return result

    def counter(self, name: str, value: int = 1, *, labels: dict[str, Any] | None = None) -> None:
        attrs = self._labels(labels)
        with self._lock:
            instrument = self._counters.get(name)
            if instrument is None:
                instrument = self._meter.create_counter(name)
                self._counters[name] = instrument
            instrument.add(value, attrs)
            self._snapshot[(name, tuple(sorted(attrs.items())))] += value

    def histogram(self, name: str, value: float, *, labels: dict[str, Any] | None = None) -> None:
        attrs = self._labels(labels)
        with self._lock:
            instrument = self._histograms.get(name)
            if instrument is None:
                instrument = self._meter.create_histogram(name)
                self._histograms[name] = instrument
            instrument.record(value, attrs)

    def gauge(self, name: str, value: float, *, labels: dict[str, Any] | None = None) -> None:
        """Set a low-cardinality observable gauge series.

        Queue depth and active duration are current-state measurements, so
        publishing them as increment-only counters is misleading. One OTel
        observable instrument is created per metric name and reads the
        bounded series stored here at collection time.
        """
        attrs = self._labels(labels)
        key = tuple(sorted(attrs.items()))
        with self._lock:
            values = self._gauge_values.setdefault(name, {})
            values[key] = float(value)
            if name not in self._gauges:
                def observe(_options: Any, metric_name: str = name) -> list[Observation]:
                    with self._lock:
                        current = dict(self._gauge_values.get(metric_name, {}))
                    return [Observation(value, dict(series)) for series, value in current.items()]

                self._gauges[name] = self._meter.create_observable_gauge(
                    name, callbacks=[observe]
                )

    def up_down_counter(
        self, name: str, value: int, *, labels: dict[str, Any] | None = None
    ) -> None:
        """Record a current-in-flight value, which may decrease."""
        attrs = self._labels(labels)
        with self._lock:
            instrument = self._up_down_counters.get(name)
            if instrument is None:
                instrument = self._meter.create_up_down_counter(name)
                self._up_down_counters[name] = instrument
            instrument.add(value, attrs)
            self._snapshot[(name, tuple(sorted(attrs.items())))] += value

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "counters": [
                    {"name": name, "labels": dict(labels), "value": value}
                    for (name, labels), value in sorted(self._snapshot.items())
                ],
                "gauges": [
                    {"name": name, "labels": dict(labels), "value": value}
                    for name, series in sorted(self._gauge_values.items())
                    for labels, value in sorted(series.items())
                ],
            }

    def shutdown(self) -> None:
        self.provider.shutdown()
