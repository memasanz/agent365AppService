import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult

from src import observability
from src.observability import TelemetryRuntime, TelemetrySettings
from tests.conftest import CHILD, PROJECT


def test_disabled_mode_does_not_initialize_distro_or_require_credentials(monkeypatch):
    configure = Mock()
    monkeypatch.setattr("microsoft.opentelemetry.use_microsoft_opentelemetry", configure)
    assert (
        observability.configure_observability(
            {
                "ENABLE_AGENT365_TELEMETRY": "false",
                "A365_EXPORT_TO_FOUNDRY": "true",
            }
        )
        is None
    )
    configure.assert_not_called()


@pytest.mark.parametrize(
    "setting",
    [
        "ENTRA_TENANT_ID",
        "ENTRA_BLUEPRINT_CLIENT_ID",
        "ENTRA_AGENT_IDENTITY_OBJECT_ID",
        "AZURE_MANAGED_IDENTITY_CLIENT_ID",
        "A365_SPONSOR_USER_ID",
        "A365_SPONSOR_DISPLAY_NAME",
        "A365_SPONSOR_UPN",
        "APPLICATIONINSIGHTS_CONNECTION_STRING",
    ],
)
def test_incomplete_enabled_configuration_is_sanitized(telemetry_env, setting):
    telemetry_env.pop(setting)
    with pytest.raises(ValueError) as error:
        TelemetrySettings.from_env(telemetry_env)
    assert setting in str(error.value)
    assert "sponsor@example.invalid" not in str(error.value)


@pytest.mark.parametrize("dual", [True, False])
def test_explicit_s2s_single_pipeline_and_destination_flags(monkeypatch, telemetry_env, dual):
    monkeypatch.setattr(observability, "_runtime", None)
    configure = Mock()
    monkeypatch.setattr("microsoft.opentelemetry.use_microsoft_opentelemetry", configure)
    monkeypatch.setattr(observability, "ObservabilityTokenResolver", Mock())
    monkeypatch.setattr(observability, "verify_pipeline", Mock())
    credential = Mock()
    monkeypatch.setattr("azure.identity.ManagedIdentityCredential", credential)
    telemetry_env["A365_EXPORT_TO_FOUNDRY"] = str(dual).lower()
    if not dual:
        telemetry_env.pop("APPLICATIONINSIGHTS_CONNECTION_STRING")
    result = observability.configure_observability(telemetry_env)
    assert observability.configure_observability(telemetry_env) is result
    configure.assert_called_once()
    kwargs = configure.call_args.kwargs
    assert kwargs["enable_a365"] and kwargs["a365_use_s2s_endpoint"]
    assert kwargs["a365_enable_observability_exporter"]
    assert kwargs["a365_token_resolver"] is result.resolver
    assert kwargs["enable_azure_monitor"] is dual
    assert kwargs["a365_exporter_disable_offline_storage"]
    assert kwargs["azure_monitor_exporter_disable_offline_storage"]
    assert kwargs["disable_logging"] and kwargs["disable_metrics"]
    assert all(not option["enabled"] for option in kwargs["instrumentation_options"].values())
    if dual:
        credential.assert_called_once_with()
        assert kwargs["azure_monitor_exporter_credential"] is credential.return_value
    else:
        credential.assert_not_called()


@pytest.mark.parametrize(
    "value",
    [
        "InstrumentationKey=PRIVATE",
        "InstrumentationKey=11111111-1111-4111-8111-111111111111;;Invalid",
        "InstrumentationKey=11111111-1111-4111-8111-111111111111;IngestionEndpoint=http://unsafe",
    ],
)
def test_invalid_connection_rejected_before_sdk_can_silently_downgrade(telemetry_env, value):
    telemetry_env["APPLICATIONINSIGHTS_CONNECTION_STRING"] = value
    with pytest.raises(ValueError, match="CONNECTION_STRING") as error:
        TelemetrySettings.from_env(telemetry_env)
    assert "PRIVATE" not in str(error.value)


class CollectingExporter(SpanExporter):
    def __init__(self):
        self.spans = []

    def export(self, spans):
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS


@pytest.fixture(scope="module")
def sinks():
    from microsoft.opentelemetry.a365.core.opentelemetry_scope import OpenTelemetryScope

    provider = TracerProvider()
    left, right = CollectingExporter(), CollectingExporter()
    provider.add_span_processor(SimpleSpanProcessor(left))
    provider.add_span_processor(SimpleSpanProcessor(right))
    # Keep test provider local to the real SDK scope, without resetting OTel global singletons.
    original = OpenTelemetryScope._tracer, OpenTelemetryScope._enabled_by_distro
    OpenTelemetryScope._tracer = provider.get_tracer("test")
    OpenTelemetryScope._enabled_by_distro = True
    yield left, right
    OpenTelemetryScope._tracer, OpenTelemetryScope._enabled_by_distro = original
    provider.shutdown()


async def test_real_scope_structure_redaction_and_concurrent_isolation(telemetry_env, sinks):
    left, right = sinks
    left.spans.clear()
    right.spans.clear()
    runtime = TelemetryRuntime(TelemetrySettings.from_env(telemetry_env), Mock())

    async def model(message):
        await asyncio.sleep(0.01)
        return SimpleNamespace(
            text="PRIVATE COMPLETION " + message,
            usage_details={"input_token_count": 4, "output_token_count": 7},
        )

    results = await asyncio.gather(
        *[
            runtime.run(model, "PRIVATE PROMPT", f"conv-{i}", "127.0.0.1", "model", PROJECT)
            for i in range(8)
        ]
    )
    assert len(left.spans) == len(right.spans) == 24
    assert {s.context.trace_id for s in left.spans} == {s.context.trace_id for s in right.spans}
    for i, (_, trace_id) in enumerate(results):
        spans = [s for s in left.spans if s.attributes["gen_ai.conversation.id"] == f"conv-{i}"]
        root = next(s for s in spans if s.attributes["gen_ai.operation.name"] == "invoke_agent")
        inference = next(s for s in spans if s.attributes["gen_ai.operation.name"] == "chat")
        output = next(
            s for s in spans if s.attributes["gen_ai.operation.name"] == "output_messages"
        )
        assert root.parent is None
        assert inference.parent.span_id == output.parent.span_id == root.context.span_id
        assert f"{root.context.trace_id:032x}" == trace_id
        assert {s.attributes["gen_ai.agent.id"] for s in spans} == {CHILD}
        assert inference.attributes["gen_ai.usage.input_tokens"] == 4
        for span in spans:
            serialized = str(dict(span.attributes))
            assert "PRIVATE" not in serialized
            assert "execute_tool" not in serialized
            assert "microsoft.agent.user.id" not in span.attributes
            messages = json.loads(span.attributes["gen_ai.output.messages"])
            assert messages[0]["role"] == "assistant"
    assert len({trace_id for _, trace_id in results}) == 8
    assert not trace.get_current_span().get_span_context().is_valid


async def test_token_outage_does_not_block_response(telemetry_env, sinks):
    runtime = TelemetryRuntime(
        TelemetrySettings.from_env(telemetry_env),
        Mock(side_effect=RuntimeError("TOKEN")),
    )

    async def model(_):
        return SimpleNamespace(text="ok", usage_details={})

    response, _ = await runtime.run(model, "hello", "outage", "127.0.0.1", "model", PROJECT)
    assert response.text == "ok"


def test_sdk_diagnostics_withhold_bodies_and_do_not_recommend_role_grants():
    record = logging.LogRecord(
        "sdk",
        logging.ERROR,
        "",
        1,
        "HTTP %d non-retryable error. Correlation ID: %s. Response: %s.",
        (403, "correlation-123", "PRIVATE BODY AND TOKEN"),
        None,
    )
    assert observability.ExportDiagnosticsFilter().filter(record)
    assert "403" in record.getMessage() and "correlation-123" in record.getMessage()
    assert "PRIVATE" not in record.getMessage()


def test_missing_exporters_cannot_silently_downgrade_enabled_configuration(monkeypatch):
    provider = SimpleNamespace(_active_span_processor=SimpleNamespace(_span_processors=[]))
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)
    with pytest.raises(ValueError, match="no destination downgrade"):
        observability.verify_pipeline(True)
