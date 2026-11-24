# Source code (`src/`)

This is the code to read, copy, or re-implement. It has two halves that never import each other's concerns:

- **Runtime** (`app.py`, `observability.py`, `telemetry_auth.py`): deployed to App Service. Serves `/chat`, calls the model, and optionally emits Agent 365 / Foundry telemetry.
- **Administrative** (`onboarding.py`): run only from notebook 01 and `scripts\verify.ps1`/`enable-observability.ps1`. It creates and verifies the Entra and Agent 365 objects. It is **never deployed** (`scripts\deploy.ps1` zips only `requirements.txt` and `src\*.py`, and the app never imports `onboarding`).

| File | Lines | Role | Deployed |
| --- | --- | --- | --- |
| `app.py` | ~140 | FastAPI app, `/health`, `/chat`, model client lifecycle | Yes |
| `observability.py` | ~415 | Telemetry settings, pipeline setup, manual activity spans | Yes |
| `telemetry_auth.py` | ~200 | Managed identity to blueprint to child app-only token | Yes |
| `onboarding.py` | ~780 | Create/verify blueprint, child, federation, registration, Foundry External Agent | No |

> The deployment ZIP includes every `src\*.py`, so `onboarding.py` is physically on the host but is never imported. Keep it that way: it holds administrative logic and imports `msal`.

## Dependencies

Direct dependencies are in `requirements.in`; `requirements.txt` is the locked output of `uv pip compile` and is what the Oryx build installs. The versions are pinned deliberately. Several guards rely on private internals of these exact versions (see [Pinned-version assumptions](#pinned-version-assumptions)), so revalidate on any upgrade.

| Package | Used for |
| --- | --- |
| `agent-framework-core`, `agent-framework-foundry`, `agent-framework-openai` | `Agent`, `FoundryChatClient` |
| `azure-ai-projects` | Foundry `AIProjectClient`, `ExternalAgentDefinition` (preview) |
| `azure-identity` | `ManagedIdentityCredential`, `AzureCliCredential` |
| `microsoft-opentelemetry` | `use_microsoft_opentelemetry` distro and the Agent 365 scopes |
| `msal` | Interactive sign-in for the administrative client |
| `requests` | Token exchange and Graph calls |
| `fastapi`, `uvicorn` | HTTP server |
| `python-dotenv` | Local `.env` loading |

## Runtime flow

```text
process start
  load_dotenv()
  configure_observability()   -> None while ENABLE_AGENT365_TELEMETRY=false
  import agent_framework, ...  (must come AFTER telemetry setup)
  disable_instrumentation()    (the framework's own spans are turned off)
lifespan start
  validate FOUNDRY_PROJECT_ENDPOINT, FOUNDRY_MODEL
  credential = ManagedIdentityCredential() on App Service, else AzureCliCredential()
  Agent(FoundryChatClient(project_endpoint, model, credential))
POST /chat
  validate body -> run agent (via TelemetryRuntime.run when enabled) -> reply
lifespan stop
  close client and credential, flush and shut down telemetry
```

Key points when re-implementing:

1. **Telemetry is configured before the framework is imported.** `app.py` calls `configure_observability()` first and imports `agent_framework` afterward (hence the `# noqa: E402`). Reorder and the distro may not see the libraries it needs to patch or disable.
2. **Hosted detection is `WEBSITE_INSTANCE_ID`.** Present on App Service, so the app uses `ManagedIdentityCredential()` with **no client ID**, which selects the **system-assigned** identity. Locally it falls back to `AzureCliCredential()`. Never set a global `AZURE_CLIENT_ID`: it would silently redirect this credential to another identity. The deployment scripts reject it.
3. **The model identity and the telemetry identity are different.** The system-assigned identity calls the model and (optionally) Azure Monitor. The user-assigned identity (UAMI) is used only to start the Agent 365 token exchange.
4. **Model errors are intentionally opaque.** `/chat` catches every exception, logs only the conversation ID, and returns a fixed `502` body. This avoids leaking service details; it also means the real cause (usually missing model RBAC) must be found from Azure, not the response.

## `app.py`: HTTP API

| Endpoint | Behavior |
| --- | --- |
| `GET /health` | Returns `{"status":"ok"}`. Proves the process is up, **not** that the model works. |
| `POST /chat` | Body `{"message": str, "conversation_id"?: str}`. Returns `{"reply", "conversation_id", "trace_id"}`. |

Validation (all enforced; the contract tests check each):

- `message`: strict string, whitespace stripped, 1-8000 characters.
- `conversation_id`: optional, `^[A-Za-z0-9._:-]{1,128}$`. Generated (UUID4) when absent. It is correlation metadata only: **every call is single-turn**, the agent is not given prior messages.
- Extra fields are rejected (`extra="forbid"`).
- Body limit 40,000 bytes, enforced twice in the `limit_body` middleware: by `Content-Length` and by counting streamed chunks (a client can lie about the header). The body is then cached on `request._body` so FastAPI can parse it after the stream was consumed.
- Validation errors return a fixed `422` body. FastAPI's default echoes the rejected input, which could reflect secrets or content, so it is replaced.
- Docs (`/docs`, `/redoc`) are disabled.

Names and functions:

| Name | Purpose |
| --- | --- |
| `Message`, `Conversation` | Annotated string types holding the validation rules. |
| `ChatRequest`, `ChatResponse` | Pydantic request/response models. `trace_id` is `null` unless telemetry is on. |
| `lifespan` | Builds the credential, `FoundryChatClient` (60 s timeout) and `Agent` once and stores them on `app.state`; on shutdown closes them and flushes telemetry. Fails fast if `FOUNDRY_MODEL` is empty or the endpoint is invalid. |
| `invalid_request` | Sanitized `422` handler. |
| `limit_body` | The 40,000-byte limit described above. |
| `health`, `chat` | The two routes. `chat` calls `telemetry.run(...)` when telemetry is enabled, otherwise `agent.run(message)` directly. |

The agent is a plain `Agent` with one fixed instruction and no tools. To build a different agent, replace the `Agent(...)` construction in `lifespan`; everything else (validation, telemetry wrapping) is independent of it.

## Configuration reference

All runtime configuration is environment variables (App Service application settings in production, `.env` locally). `deploy.ps1` sets the model ones; `enable-observability.ps1` sets the telemetry ones.

| Variable | Required | Meaning |
| --- | --- | --- |
| `FOUNDRY_PROJECT_ENDPOINT` | Always | `https://<account>.services.ai.azure.com/api/projects/<project>`. Validated by `project_endpoint()`. |
| `FOUNDRY_MODEL` | Always | Responses-compatible **deployment name**. |
| `ENABLE_AGENT365_TELEMETRY` | No (default `false`) | Master switch. Must be exactly `true` or `false`. |
| `ENTRA_TENANT_ID` | Telemetry | Tenant GUID. |
| `ENTRA_BLUEPRINT_CLIENT_ID` | Telemetry | Blueprint app (client) ID. |
| `ENTRA_AGENT_IDENTITY_OBJECT_ID` | Telemetry | Child agent identity ID. Must differ from the blueprint ID. |
| `AZURE_MANAGED_IDENTITY_CLIENT_ID` | Telemetry | **UAMI** client ID (not the system-assigned identity). |
| `AGENT_IDENTITY_NAME` | Telemetry | Child display name; also the OpenTelemetry `service.name`. |
| `A365_SPONSOR_USER_ID`, `A365_SPONSOR_DISPLAY_NAME`, `A365_SPONSOR_UPN` | Telemetry | Accountability metadata attached to spans. |
| `A365_EXPORT_TO_FOUNDRY` | No (default `true`) | Also export to Application Insights for the Foundry External Agent view. |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | When exporting to Foundry | Validated: instrumentation key GUID, HTTPS endpoints, no credentials in URLs. |
| `A365_AZURE_MONITOR_USE_ENTRA` | No (default `true`) | Authenticate Azure Monitor ingestion with the system identity. |
| `A365_CAPTURE_CONTENT` | No (default `false`) | Record real message text instead of `[content redacted]`. Test data only. |

Rejected on purpose: `OTEL_TRACES_EXPORTER=none` and any `OTEL_EXPORTER_OTLP*` variable (they would silently change or disable destinations), and a global `AZURE_CLIENT_ID`.

## `observability.py`: telemetry pipeline

### Helpers

| Function / class | Purpose |
| --- | --- |
| `flag(env, name, default)` | Strict boolean: only `true`/`false`, else raises. |
| `required(env, name)` | Non-blank string or raises. |
| `project_endpoint(value)` | Accepts only a commercial HTTPS `*.services.ai.azure.com` project URL (port 443 or none, no query/fragment/userinfo). Returns it without a trailing slash. |
| `TelemetrySettings` | Frozen dataclass; `from_env(env)` validates every setting above, including the connection string. |
| `ExportDiagnosticsFilter` | Log filter on the Agent 365 exporter logger. Rewrites transport log lines to a status code plus correlation ID and drops response bodies; any warning or error becomes one generic message; everything else is dropped. |

### `configure_observability(env=None)`

Returns `None` when telemetry is off, otherwise a singleton `TelemetryRuntime` (guarded by a lock, so repeated calls return the same one). Steps:

1. Parse and validate `TelemetrySettings`; reject forbidden `OTEL_*` variables.
2. Set `OTEL_LOGS_EXPORTER=none` and `OTEL_METRICS_EXPORTER=none`. The pinned distro's Azure Monitor path reads these environment variables and ignores the corresponding `disable_*` arguments.
3. If exporting to Foundry, put the connection string in `APPLICATIONINSIGHTS_CONNECTION_STRING` for the distro.
4. Build an `ObservabilityTokenResolver` (below) and, when exporting to Foundry with Entra, a `ManagedIdentityCredential()` (system-assigned) for Azure Monitor.
5. Call `use_microsoft_opentelemetry(...)` once, with:
   - `enable_a365=True`, `a365_use_s2s_endpoint=True`, `a365_token_resolver=resolver`: the Agent 365 exporter, using the app-only (service-to-service) endpoint.
   - `enable_azure_monitor=<export_to_foundry>`, the connection string and credential.
   - `disable_logging=True`, `disable_metrics=True`: **traces only**.
   - `*_disable_offline_storage=True`: failed spans are dropped, not spooled to disk.
   - `enable_sensitive_data=False`, `sampling_ratio=1.0`.
   - `instrumentation_options`: **every** automatic instrumentation turned off (Django, FastAPI, Flask, httpx, requests, urllib, OpenAI, agent_framework, etc.). The code creates its own spans, so automatic ones would duplicate them or trace health probes.
6. `verify_pipeline(dual)`.

### `verify_pipeline(dual)`

The distro can swallow exporter initialization errors and still report success. This inspects the live tracer provider's span processors and requires **exactly one** `_Agent365Exporter` and `int(dual)` `AzureMonitorTraceExporter`. A mismatch aborts startup rather than silently running with one destination. It uses private attributes (`_active_span_processor._span_processors`), so it is version-bound.

### `TelemetryRuntime.run(...)`: the span structure

Called per `/chat` request. It never lets telemetry break a reply: every scope and record call is wrapped (`safe_scope`, `safe_record`) so a telemetry failure logs a warning and the response still returns.

```text
baggage (tenant, agent, blueprint, conversation, session, channel, sponsor user, client IP)
└── InvokeAgentScope            (the whole request, caller = sponsor)
    ├── InferenceScope          (the model call: operation CHAT, provider azure.ai.foundry, model name)
    │     records input messages, output messages, input/output token counts, errors
    └── OutputScope             (the reply)
```

Details to preserve:

- Before starting spans it calls the resolver once in a thread (`asyncio.to_thread`) so the first export has a token. Failure only logs.
- `trace_id` returned to the client is the trace ID of the **invocation** span (32 hex characters). It is `None` if no valid span context exists.
- Content is `[content redacted]` unless `A365_CAPTURE_CONTENT=true`.
- The user on the spans is the **registered sponsor**, labeled with the attribute `sample.attribution.source = registered_sponsor_not_authenticated_caller`. The sample has no end-user authentication in the telemetry path, so it must not claim the sponsor actually called.
- If the model call fails, the error is recorded on both inference and invocation spans, and a sanitized `RuntimeError` is re-raised.
- The same `child_id` is used as `agent_id` for Agent 365 and as the Foundry External Agent's `otel_agent_id`. That identity equality is how both portals show the same agent.

`shutdown()` flushes (30 s timeout), shuts down the tracer provider, then closes the resolver and the Azure Monitor credential. Delivery is best effort: spooling is off, so spans can be lost on a crash.

## `telemetry_auth.py`: the Agent 365 token exchange

The exporter needs a token that proves "I am this child agent". No secret is stored; it is derived from the managed identity in two exchanges.

```text
UAMI (ManagedIdentityCredential(client_id=UAMI))
  get_token("api://AzureADTokenExchange/.default")                          -> assertion A
        |
        v   POST https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token
  1) client_credentials
       client_id             = blueprint client ID
       client_assertion_type = urn:ietf:params:oauth:client-assertion-type:jwt-bearer
       client_assertion      = A
       scope                 = api://AzureADTokenExchange/.default
       fmi_path              = child ID
                                                                            -> parent token P
        |
        v   POST same endpoint
  2) client_credentials
       client_id             = child ID
       client_assertion      = P
       scope                 = api://9b975845-388f-4429-889e-eab1ef63949c/.default
                                                                            -> child app-only token
```

Step 1 only succeeds if the blueprint has a **federated identity credential** trusting this UAMI (issuer `https://login.microsoftonline.com/<tenant>/v2.0`, subject = UAMI principal ID, audience `api://AzureADTokenExchange`). Notebook 01 creates it. `9b975845-...` is the Agent 365 Observability resource ID.

`ObservabilityTokenResolver` is a **synchronous callable** `(agent_id, tenant_id) -> token` because the exporter calls it from its own thread.

| Member | Behavior |
| --- | --- |
| `__init__` | Validates all four GUIDs; refuses `child == blueprint`; creates the UAMI credential and a `requests.Session`. `credential`, `session`, `clock` are injectable for tests. |
| `__call__` | Refuses any `agent_id`/`tenant_id` other than the configured pair. Under a lock: returns the cached token if more than 60 s remain; otherwise, if inside a 5 s backoff window, raises; otherwise runs both exchanges, validates, caches. Any failure sets a 5 s backoff and raises a sanitized `TelemetryTokenError`. |
| `_exchange` | One POST (connect 5 s, read 25 s, no redirects). Requires HTTP 200, a JSON object, a non-empty `access_token`, and `token_type: Bearer`. Error text includes only the stage and HTTP status. |
| `_validate` | Decodes the JWT payload **without verifying the signature** (the service verifies it; TLS to Entra protects transport). Routing and type guards: must be app-only, no `scp` claim, `aud` is the Observability resource, `tid` equals the tenant, every `azp`/`appid` equals the child ID, `roles` (if present) is a list of non-empty strings, and a finite positive expiry. Expiry is the earlier of the `exp` claim and `requested_at + expires_in`; it must be more than 60 s away. |
| `close` | Clears the token and closes the session and credential. |
| `guid` | Module helper: returns a canonical lowercase GUID, rejecting empty/zero/invalid values. |

Why these choices: delegated tokens and consent fallbacks are never used (a wrong token type would attribute telemetry to a person); the lock serializes concurrent exporter threads so only one refresh happens; the 60 s early renewal avoids sending an almost-expired token.

## `onboarding.py`: administrative onboarding

Everything here runs on your machine with **delegated** Graph permissions from your sign-in. The hosted app has no Graph permissions.

### Object model it creates

```text
Blueprint (application)          AppServiceBlueprint-<10 hex of sha256(webAppResourceId)>
  ├─ Blueprint principal (SP)
  ├─ Federated credential        trusts the UAMI
  └─ Child agent identity (SP)   AppServiceLearningAgent-<same suffix>, sponsored by your chosen user
Agent 365 registration           for the child, managed by the provisioning client
Foundry External Agent           otel_agent_id = child ID (Foundry project, preview)
```

The deterministic name suffix (from the web app resource ID) is what lets a rerun **find** its own objects instead of creating duplicates.

### Constants

| Name | Value |
| --- | --- |
| `GRAPH` | `https://graph.microsoft.com` |
| `REGISTRATIONS` | `/beta/copilot/agentRegistrations` (undocumented preview) |
| `SCOPES` | The eight delegated permissions. `scripts\New-AgentIdentityProvisioningClient.ps1` must request the same set, and a test enforces that. |

### Sign-in and Graph access

| Function / class | Purpose |
| --- | --- |
| `sign_in(tenant, client_id, read_only=False)` | MSAL `PublicClientApplication`, interactive PKCE, loopback port **8400**, 180 s timeout. `read_only=True` requests only the read scopes (plus `AgentRegistration.ReadWrite.All`, which the registration read needs). Returns a `GraphClient`; failures give one generic message. |
| `GraphClient.request(method, path, body, missing=False)` | Calls Graph with `OData-Version: 4.0`. Paths must start with `/v1.0/` or `/beta/`. No redirects. `missing=True` turns a 404 into `None`. Non-2xx raises `GraphError(status)` with no body. |
| `GraphClient.items(path)` | Reads all pages; refuses repeated pages or a next link to another host. |
| `GraphClient.close()` | Drops the token and closes the session. |
| `one_or_none(items)` | Zero or one result, else raises (never picks one of several). |

### State file and the idempotency pattern

State is a private JSON file (`.local\onboarding-state.json`), written by `save_private` (icacls on Windows or `chmod`, atomic temp-file replace).

| Function | Purpose |
| --- | --- |
| `save_private(path, value)` | Writes JSON with owner-only access, atomically. |
| `load_state(path, deployment, client_id)` | Loads or creates state (`schemaVersion: 1`, `registrationRequestId` UUID). Refuses state from another deployment or client. |
| `validate_deployment(deployment)` | Checks the deployment artifact: canonical GUIDs, required strings, valid Foundry endpoint, web app resource ID matching its subscription/group/name. |

Every non-idempotent create follows the same pattern, which you should keep if you re-implement:

1. **Look up** the object by saved ID, else by deterministic name/shape. Several matches means stop.
2. If missing: if a `...Pending` flag is already set, **stop**: a previous create may have succeeded without us seeing the result. Otherwise set the flag, **save state**, then create.
3. After the create, read back and verify it belongs to the expected parent, save IDs, clear the flag.

A timeout after a committed write therefore leads to reconciliation, not a duplicate.

### Ensure functions (called by the notebook in this order)

| Function | What it does |
| --- | --- |
| `resolve_sponsor(graph, child_id, sponsor_id)` | Requires the chosen user to already be a sponsor of the child, and reads their display name and UPN. Never guesses a sponsor. |
| `ensure_blueprint(...)` | Creates/reuses the blueprint via `POST /v1.0/applications/microsoft.graph.agentIdentityBlueprint` (sponsor and owner = your user, `managerApplications` = the provisioning client), then the blueprint principal via `POST /v1.0/servicePrincipals/microsoft.graph.agentIdentityBlueprintPrincipal`. Principals are **not** created automatically with the blueprint. Refuses a blueprint that the provisioning client does not manage. Explicit `blueprint_id`/`child_id` allow reusing existing objects. |
| `ensure_child(...)` | Creates/reuses the child via `POST /v1.0/servicePrincipals/microsoft.graph.agentIdentity` with `agentIdentityBlueprintId` and the sponsor. Requires the child to belong to this blueprint and, if `appId` is returned, to equal `id` (otherwise telemetry would be attributed to the wrong GUID). |
| `ensure_federation(...)` | Adds the blueprint federated credential via `/beta/applications/{id}/microsoft.graph.agentIdentityBlueprint/federatedIdentityCredentials` with issuer `https://login.microsoftonline.com/<tenant>/v2.0`, subject = the UAMI **principal** ID, audience `api://AzureADTokenExchange`. Reuses an exact match; refuses a same-named credential with different trust. Reads it back. |
| `ensure_registration(...)` | `POST /beta/copilot/agentRegistrations` with the child, blueprint, sponsor as owner and creator, and `managedByAppId` = the provisioning client. After any error, probes the **same request ID** (also trying a `T_` prefix) before deciding. Never creates a second registration. |
| `registration_probe` / `verify_registration` | Read the registration back and require `sourceAgentId`, `agentIdentityId`, `agentIdentityBlueprintId`, and `managedByAppId` to match. |
| `ensure_external(...)` | Creates the Foundry External Agent (`ExternalAgentDefinition(otel_agent_id=child_id)`) in the Foundry project, or reuses one whose kind and telemetry ID match. Records its ID and version. |
| `external_record` | Reads the latest version and refuses a different agent kind or telemetry ID. |

The registration endpoint is **undocumented preview**. The code uses it because the Agent 365 CLI source does; there is no fallback to another API when it fails.

### Verification and handoff

| Function | What it does |
| --- | --- |
| `az_json(*args)` | Runs Azure CLI with JSON output, 120 s timeout. |
| `verify_azure_deployment(deployment)` | CLI tenant/subscription matches; the live app's system principal and attached UAMI match the deployment artifact. |
| `verify_insights(project, resource_id, connection_string)` | Confirms the Application Insights connection string, the Foundry project's linked connection, and the ARM resource all share one instrumentation key. |
| `prepare_runtime_settings(state, path, ...)` | Builds `.local\runtime-settings.json`: the exact environment variables the runtime needs (see Configuration reference) plus the IDs used for revalidation. Refuses until registration and federation exist, and, for dual export, until the External Agent and Insights link are verified. Round-trips the settings through `TelemetrySettings.from_env` so a bad file is never written. |
| `validate_runtime(artifact, graph, project=None)` | Read-only re-check of the whole chain: settings match identities, live blueprint/principal/child relationships, sponsor metadata unchanged, exact federated credential exists, registration matches, and (dual) the External Agent and Insights link still match. |
| `main()` | CLI used by the scripts: `python -m src.onboarding validate-runtime --settings-file <path>`. Signs in read-only, runs `validate-runtime`. Prints only a success line. |

## Re-implementation checklist

To build the same thing in another language or framework, these are the load-bearing requirements:

1. **Two identities with separate jobs.** System-assigned identity for model/Monitor access; a user-assigned identity only as the federation source for the Agent 365 token.
2. **Entra objects in order:** blueprint, blueprint principal, federated credential (UAMI to blueprint), child identity. Create principals explicitly; they are not automatic.
3. **Token exchange exactly as above:** UAMI assertion, blueprint `fmi_path=<child>` exchange, child app-only token for the Observability audience. No client secrets, no delegated/OBO tokens.
4. **Register the child with Agent 365** (preview endpoint) and create the Foundry External Agent with `otel_agent_id` equal to the child ID.
5. **Emit three spans** per request (invoke, inference, output) carrying tenant, agent, blueprint, conversation, and model attributes, with content redacted by default.
6. **Fail closed on destinations:** verify the exporters actually initialized; refuse to run with a missing or duplicate one.
7. **Idempotent, recoverable writes:** deterministic names, saved intent before create, reconcile (never blindly retry) after an ambiguous result.
8. **Never let telemetry affect a reply:** wrap all telemetry calls so failures only log.
9. **Sanitize everything outward:** no response bodies, tokens, or SDK error text in logs or HTTP replies.

## Pinned-version assumptions

These code paths depend on internals of the locked package versions. Revalidate (the tests in `tests\test_distro_integration.py` and `tests\test_observability.py` cover them) after upgrading `microsoft-opentelemetry`, `azure-monitor-opentelemetry-exporter`, or `agent-framework-*`:

- `verify_pipeline` reads `trace.get_tracer_provider()._active_span_processor._span_processors` and the private `_Agent365Exporter` class.
- The distro honors `OTEL_LOGS_EXPORTER`/`OTEL_METRICS_EXPORTER` and not the matching `disable_*` arguments.
- The Agent 365 scope classes (`InvokeAgentScope`, `InferenceScope`, `OutputScope`) and `a365_token_resolver` callback signature `(agent_id, tenant_id)`.
- The distro can swallow exporter initialization errors, which is why `verify_pipeline` exists.

## Tests

`tests\` has one file per module (`test_app.py`, `test_observability.py`, `test_telemetry_auth.py`, `test_onboarding.py`), plus `test_distro_integration.py` (real distro wiring), `test_deployment_scripts.py` (PowerShell scripts with mocked Azure and Graph), and `test_notebook.py` (notebook structure and behavior). Run them with `.\.venv\Scripts\python.exe -m pytest -q`.
