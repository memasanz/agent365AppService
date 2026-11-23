"""Isolate real distro global providers; mock only identity and service exporter boundaries."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.conftest import PROJECT


@pytest.mark.parametrize("failing", [False, True])
def test_real_distro_dual_pipeline_in_fresh_process(telemetry_env, failing):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("OTEL_", "ENABLE_OBSERVABILITY", "ENABLE_A365_"))
    }
    env.update(telemetry_env)
    env["APPLICATIONINSIGHTS_STATSBEAT_DISABLED_ALL"] = "true"
    result = subprocess.run(
        [sys.executable, "-m", "tests.test_distro_integration", str(failing)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        env=env,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def probe(failing):
    from azure.monitor.opentelemetry.exporter import (
        AzureMonitorLogExporter,
        AzureMonitorMetricExporter,
        AzureMonitorTraceExporter,
    )
    from microsoft.opentelemetry.a365.core.exporters.agent365_exporter import _Agent365Exporter
    from opentelemetry import trace
    from opentelemetry.sdk.trace.export import SpanExportResult

    from src.observability import configure_observability

    left, right = [], []

    def export_left(self, spans, *args, **kwargs):
        left.extend(spans)
        if failing:
            raise ConnectionError("Offline test exporter outage")
        return SpanExportResult.SUCCESS

    def export_right(self, spans, *args, **kwargs):
        right.extend(spans)
        return SpanExportResult.SUCCESS

    async def model(_):
        return SimpleNamespace(text="ok", usage_details={})

    with (
        patch.object(_Agent365Exporter, "export", export_left),
        patch.object(AzureMonitorTraceExporter, "export", export_right),
        patch.object(
            AzureMonitorLogExporter,
            "__init__",
            side_effect=AssertionError("Operational log export must stay disabled"),
        ),
        patch.object(
            AzureMonitorMetricExporter,
            "__init__",
            side_effect=AssertionError("Metrics export must stay disabled"),
        ),
        patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("Network forbidden in offline tests"),
        ),
        patch("src.observability.ObservabilityTokenResolver") as resolver,
    ):
        resolver.return_value.return_value = "not-exported"
        runtime = configure_observability()
        response, trace_id = asyncio.run(
            runtime.run(model, "PRIVATE", "distro-probe", "127.0.0.1", "model", PROJECT),
        )
        assert response.text == "ok" and trace_id
        trace.get_tracer_provider().force_flush()
        assert len(left) == len(right) == 3
        assert {s.context.trace_id for s in left} == {s.context.trace_id for s in right}
        assert all("PRIVATE" not in str(dict(s.attributes)) for s in left + right)
        runtime.shutdown()


if __name__ == "__main__":
    probe(sys.argv[1] == "True")
