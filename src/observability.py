"""One dormant-by-default tracing pipeline, with explicit manual activity scopes."""

import asyncio
import logging
import os
import re
import threading
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

from src.telemetry_auth import ObservabilityTokenResolver, guid

logger = logging.getLogger(__name__)
_lock = threading.Lock()
_runtime = None


def flag(env, name, default=False):
    value = env.get(name, str(default)).lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false.")
    return value == "true"


def required(env, name):
    value = env.get(name, "").strip()
    if not value:
        raise ValueError(f"Set {name} before enabling telemetry.")
    return value


def project_endpoint(value):
    try:
        parsed = urlsplit(value)
        valid_port = parsed.port in (None, 443)
    except ValueError:
        raise ValueError("Use the commercial HTTPS Foundry project endpoint.") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not parsed.hostname.endswith(".services.ai.azure.com")
        or not re.fullmatch(r"/api/projects/[A-Za-z0-9._-]+", parsed.path.rstrip("/"))
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
        or not valid_port
    ):
        raise ValueError("Use the commercial HTTPS Foundry project endpoint.")
    return value.rstrip("/")


@dataclass(frozen=True)
class TelemetrySettings:
    tenant_id: str
    blueprint_id: str
    child_id: str
    managed_identity_id: str
    agent_name: str
    sponsor_id: str
    sponsor_name: str
    sponsor_upn: str
    export_to_foundry: bool
    connection_string: str
    capture_content: bool
    use_entra: bool

    @classmethod
    def from_env(cls, env):
        tenant = guid(env.get("ENTRA_TENANT_ID"), "ENTRA_TENANT_ID")
        blueprint = guid(env.get("ENTRA_BLUEPRINT_CLIENT_ID"), "ENTRA_BLUEPRINT_CLIENT_ID")
        child = guid(env.get("ENTRA_AGENT_IDENTITY_OBJECT_ID"), "ENTRA_AGENT_IDENTITY_OBJECT_ID")
        managed = guid(
            env.get("AZURE_MANAGED_IDENTITY_CLIENT_ID"), "AZURE_MANAGED_IDENTITY_CLIENT_ID"
        )
        if child == blueprint:
            raise ValueError("Telemetry requires the child identity, not the blueprint.")
        dual = flag(env, "A365_EXPORT_TO_FOUNDRY", True)
        connection = (
            required(env, "APPLICATIONINSIGHTS_CONNECTION_STRING").rstrip(";") if dual else ""
        )
        if dual:
            try:
                pairs = dict(part.split("=") for part in connection.split(";"))
                keys = {key.lower(): value for key, value in pairs.items()}
                guid(keys.get("instrumentationkey"), "instrumentation key")
                if keys.get("authorization", "ikey").lower() != "ikey":
                    raise ValueError
                for key in ("ingestionendpoint", "liveendpoint"):
                    if key in keys:
                        parsed = urlsplit(keys[key])
                        if (
                            parsed.scheme != "https"
                            or not parsed.hostname
                            or parsed.username
                            or parsed.password
                            or parsed.query
                            or parsed.fragment
                        ):
                            raise ValueError
            except Exception:
                raise ValueError(
                    "APPLICATIONINSIGHTS_CONNECTION_STRING must be a valid HTTPS connection string."
                ) from None
        return cls(
            tenant,
            blueprint,
            child,
            managed,
            required(env, "AGENT_IDENTITY_NAME"),
            guid(env.get("A365_SPONSOR_USER_ID"), "A365_SPONSOR_USER_ID"),
            required(env, "A365_SPONSOR_DISPLAY_NAME"),
            required(env, "A365_SPONSOR_UPN"),
            dual,
            connection,
            flag(env, "A365_CAPTURE_CONTENT"),
            flag(env, "A365_AZURE_MONITOR_USE_ENTRA", True),
        )


class ExportDiagnosticsFilter(logging.Filter):
    """Keep transport status visible without logging SDK response bodies or headers."""

    def filter(self, record):
        message = str(record.msg)
        if message.startswith("HTTP %d") and isinstance(record.args, tuple) and record.args:
            status = record.args[0]
            correlation = record.args[1] if len(record.args) > 1 else "unavailable"
            record.msg = (
                "Agent365 transport HTTP %s; correlation=%s. Response withheld; "
                "transport status does not establish full ingestion or portal visibility."
            )
            record.args = (status, correlation)
            record.levelno, record.levelname = logging.INFO, "INFO"
        elif record.levelno >= logging.WARNING:
            record.msg, record.args = (
                (
                    "Agent365 export warning/failure. Check token, registration, service policy "
                    "and connectivity; no automatic role grant or delegated fallback is performed."
                ),
                (),
            )
        else:
            return False
        record.exc_info, record.exc_text = None, None
        return True


def configure_observability(env=None):
    global _runtime
    env = os.environ if env is None else env
    if not flag(env, "ENABLE_AGENT365_TELEMETRY"):
        return None
    with _lock:
        if _runtime is not None:
            return _runtime
        settings = TelemetrySettings.from_env(env)
        if os.environ.get("OTEL_TRACES_EXPORTER", "").lower() == "none":
            raise ValueError("Remove OTEL_TRACES_EXPORTER=none before enabling activity tracing.")
        if any(key.startswith("OTEL_EXPORTER_OTLP") for key in os.environ):
            raise ValueError(
                "Remove OTEL_EXPORTER_OTLP settings; this starter selects destinations explicitly."
            )
        # Distro 1.6's Azure Monitor configuration reads these standard environment
        # controls instead of honoring the corresponding disable_* kwargs.
        os.environ["OTEL_LOGS_EXPORTER"] = "none"
        os.environ["OTEL_METRICS_EXPORTER"] = "none"
        if settings.export_to_foundry:
            os.environ["APPLICATIONINSIGHTS_CONNECTION_STRING"] = settings.connection_string
        from azure.identity import ManagedIdentityCredential
        from microsoft.opentelemetry import use_microsoft_opentelemetry
        from opentelemetry.sdk.resources import Resource

        resolver = ObservabilityTokenResolver(
            settings.tenant_id,
            settings.child_id,
            settings.blueprint_id,
            settings.managed_identity_id,
        )
        monitor_credential = (
            ManagedIdentityCredential()
            if settings.export_to_foundry and settings.use_entra
            else None
        )
        # Manual scopes own both operations; framework/OpenAI/HTTP instrumentation stays off.
        libraries = (
            "django",
            "fastapi",
            "flask",
            "httpx",
            "httpx2",
            "psycopg2",
            "requests",
            "urllib",
            "urllib3",
            "azure_sdk",
            "langchain",
            "openai",
            "openai_agents",
            "semantic_kernel",
            "agent_framework",
        )
        export_logger = logging.getLogger(
            "microsoft.opentelemetry.a365.core.exporters.agent365_exporter",
        )
        export_logger.addFilter(ExportDiagnosticsFilter())
        export_logger.setLevel(logging.DEBUG)
        use_microsoft_opentelemetry(
            enable_a365=True,
            a365_use_s2s_endpoint=True,
            a365_enable_observability_exporter=True,
            a365_token_resolver=resolver,
            a365_exporter_disable_offline_storage=True,
            enable_azure_monitor=settings.export_to_foundry,
            azure_monitor_connection_string=settings.connection_string or None,
            azure_monitor_exporter_credential=monitor_credential,
            azure_monitor_exporter_disable_offline_storage=True,
            azure_monitor_enable_live_metrics=False,
            azure_monitor_enable_performance_counters=False,
            disable_logging=True,
            disable_metrics=True,
            enable_sensitive_data=False,
            sampling_ratio=1.0,
            resource=Resource.create({"service.name": settings.agent_name}),
            instrumentation_options={name: {"enabled": False} for name in libraries},
        )
        verify_pipeline(settings.export_to_foundry)
        _runtime = TelemetryRuntime(settings, resolver, monitor_credential)
        return _runtime


def verify_pipeline(dual):
    from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter
    from microsoft.opentelemetry.a365.core.exporters.agent365_exporter import _Agent365Exporter
    from opentelemetry import trace

    # The pinned distro can swallow exporter initialization errors. Inspect the
    # pinned provider shape at startup rather than silently accepting a downgrade.
    try:
        processors = trace.get_tracer_provider()._active_span_processor._span_processors
        exporters = [
            getattr(processor, "span_exporter", getattr(processor, "_exporter", None))
            for processor in processors
        ]
        if sum(isinstance(e, _Agent365Exporter) for e in exporters) != 1:
            raise ValueError
        if sum(isinstance(e, AzureMonitorTraceExporter) for e in exporters) != int(dual):
            raise ValueError
    except Exception:
        raise ValueError(
            "Telemetry pipeline initialization failed. Check the pinned dependencies and "
            "Application Insights configuration; no destination downgrade is accepted."
        ) from None


def safe_record(scope, method, *args):
    if scope is not None:
        try:
            getattr(scope, method)(*args)
        except Exception:
            logger.warning("Activity metadata recording failed; response remains available.")


@contextmanager
def safe_scope(factory):
    stack = ExitStack()
    try:
        scope = stack.enter_context(factory())
    except Exception:
        stack.close()
        scope = None
        logger.warning("Activity scope initialization failed; response remains available.")
    try:
        yield scope
    finally:
        try:
            stack.close()
        except Exception:
            logger.warning("Activity scope finalization failed; delivery is unverified.")


class TelemetryRuntime:
    def __init__(self, settings, resolver, monitor_credential=None):
        self.settings = settings
        self.resolver = resolver
        self.monitor_credential = monitor_credential

    async def run(self, model_call, message, conversation_id, client_ip, model, endpoint):
        from microsoft.opentelemetry.a365.core import (
            AgentDetails,
            BaggageBuilder,
            CallerDetails,
            Channel,
            InferenceCallDetails,
            InferenceOperationType,
            InferenceScope,
            InvokeAgentScope,
            InvokeAgentScopeDetails,
            Request,
            UserDetails,
        )
        from microsoft.opentelemetry.a365.core.models.response import Response
        from microsoft.opentelemetry.a365.core.spans_scopes.output_scope import OutputScope
        from opentelemetry import trace

        s = self.settings
        try:
            await asyncio.to_thread(self.resolver, s.child_id, s.tenant_id)
        except Exception:
            logger.warning("Token unavailable; activity delivery is unverified.")
        agent = AgentDetails(
            agent_id=s.child_id,
            agent_name=s.agent_name,
            agent_blueprint_id=s.blueprint_id,
            tenant_id=s.tenant_id,
        )
        # This IP-restricted sample has no user authentication. The verified sponsor is
        # accountability metadata, explicitly NOT a claim that the caller signed in.
        user = UserDetails(user_id=s.sponsor_id, user_name=s.sponsor_name, user_email=s.sponsor_upn)
        request = Request(
            content=[message if s.capture_content else "[content redacted]"],
            conversation_id=conversation_id,
            session_id=conversation_id,
            channel=Channel(name="web"),
        )
        baggage = (
            BaggageBuilder()
            .tenant_id(s.tenant_id)
            .agent_id(s.child_id)
            .agent_blueprint_id(s.blueprint_id)
            .agent_name(s.agent_name)
            .conversation_id(conversation_id)
            .session_id(conversation_id)
            .channel_name("web")
            .user_id(s.sponsor_id)
            .user_name(s.sponsor_name)
            .user_email(s.sponsor_upn)
            .user_client_ip(client_ip)
            .build()
        )
        attributes = {
            "client.address": client_ip,
            "server.address": urlsplit(endpoint).hostname,
            "server.port": 443,
            "sample.attribution.source": "registered_sponsor_not_authenticated_caller",
        }
        with safe_scope(lambda: baggage):
            with safe_scope(
                lambda: InvokeAgentScope.start(
                    request,
                    InvokeAgentScopeDetails(),
                    agent,
                    CallerDetails(user_details=user),
                )
            ) as invocation:
                safe_record(invocation, "record_attributes", attributes)
                current = trace.get_current_span().get_span_context()
                trace_id = f"{current.trace_id:032x}" if current.is_valid else None
                with safe_scope(
                    lambda: InferenceScope.start(
                        request,
                        InferenceCallDetails(
                            operationName=InferenceOperationType.CHAT,
                            model=model,
                            providerName="azure.ai.foundry",
                        ),
                        agent,
                        user,
                    )
                ) as inference:
                    safe_record(inference, "record_attributes", attributes)
                    safe_record(inference, "record_input_messages", request.content)
                    try:
                        response = await model_call(message)
                    except Exception:
                        error = RuntimeError(
                            "Model request failed; check model access and service health."
                        )
                        safe_record(inference, "record_error", error)
                        safe_record(invocation, "record_error", error)
                        raise error from None
                    output = [response.text if s.capture_content else "[content redacted]"]
                    safe_record(inference, "record_output_messages", output)
                    usage = response.usage_details or {}
                    for key, method in (
                        ("input_token_count", "record_input_tokens"),
                        ("output_token_count", "record_output_tokens"),
                    ):
                        if isinstance(usage.get(key), int):
                            safe_record(inference, method, usage[key])
                safe_record(invocation, "record_output_messages", output)
                with safe_scope(
                    lambda: OutputScope.start(
                        request,
                        Response(messages=output),
                        agent,
                        user,
                    )
                ) as output_scope:
                    safe_record(output_scope, "record_attributes", attributes)
                return response, trace_id

    def shutdown(self):
        from opentelemetry import trace

        try:
            drained = trace.get_tracer_provider().force_flush(timeout_millis=30000)
            logger.info(
                "Telemetry queue drained=%s; portal visibility remains unverified.", drained
            )
            trace.get_tracer_provider().shutdown()
        finally:
            self.resolver.close()
            if self.monitor_credential:
                self.monitor_credential.close()
