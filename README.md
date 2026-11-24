# Agent 365 App Service starter

A Python 3.12, Microsoft Agent Framework agent running in Linux Azure App Service. First deploy and call it with telemetry **disabled**. Then onboard the existing agent to **Agent 365 and Foundry as an External Agent**, activate the unified Microsoft OpenTelemetry pipeline, and verify the same request in both products.

This is a single-turn educational sample, not a production public API. No frontend, tools, Graph/Blob workload access, OBO, client secrets, agent users, hosted Foundry runtime, AI Gateway, streaming, or durable conversation storage is involved. External registration stores metadata; it neither hosts nor proxies `/chat`.

## Prerequisites and costs

- PowerShell **7+**, Git, Azure CLI with Bicep, `uv`, Python 3.12, and VS Code Python/Jupyter support.
- An Azure subscription and permission to create a Linux App Service plan, web app and user-assigned managed identity (UAMI).
- An existing commercial-cloud Foundry project and **Responses-compatible** model deployment. `FOUNDRY_MODEL` is the deployment name. Identify its Cognitive Services account ARM scope.
- Either explicit caller/deployment CIDRs, or the **public Entra-authenticated mode** below. Default deployment restricts site and SCM independently. An IP allowlist is **network restriction, not user authentication**.
- For the default dual-destination walkthrough: an existing **Application Insights resource linked to the selected Foundry project**. This starter does not create shared Foundry/model/Insights resources.

**Charges:** App Service B1 is billable while provisioned, including idle time. Model calls, Application Insights ingestion/retention, applicable Agent 365 licensing, and network traffic can incur charges. Prices and capacity depend on region/subscription. Deployment describes changes and requires `-Approve`; role creation requires its own explicit switch. Nothing in local setup/tests provisions Azure resources.

### Agent 365 notebook administrative permissions (not HTTP Easy Auth)

The following provisioning client is only for the later Agent 365 onboarding notebook. It is separate from the HTTP API registration used by Easy Auth; deploying and calling the app does not require this desktop redirect.

For notebook onboarding, ask an authorized tenant administrator to configure a dedicated **single-tenant public provisioning client**, with Mobile and desktop redirect **`http://localhost:8400`**, appropriate public-client configuration, and explicit admin consent to these **delegated Microsoft Graph** permissions. Consent and caller authority are separate prerequisites; neither is granted by the notebook.

| Operation | Requested least-privilege delegated permissions |
| --- | --- |
| Create/read blueprint | `AgentIdentityBlueprint.Create`, `AgentIdentityBlueprint.Read.All` |
| Create/read blueprint principal | `AgentIdentityBlueprintPrincipal.Create`, `AgentIdentityBlueprintPrincipal.Read.All` |
| Create/read child and its sponsors | `AgentIdentity.Create.All`, `AgentIdentity.Read.All` |
| Create/read FIC on a blueprint the caller owns | `AgentIdentityBlueprint.Create`, `AgentIdentityBlueprint.Read.All` under the selected beta FIC contract |
| Register/read Agent 365 child | `AgentRegistration.ReadWrite.All`, with `AgentIdentity.Read.All` for relationship checks |
| Resolve the selected existing human sponsor | `User.ReadBasic.All` |

These are operation-specific, not one blanket permission. The default path creates a caller-owned blueprint/principal: owners can manage children of their blueprint. Nonowners need **Agent ID Administrator** where documented. Agent ID Developer is supported for blueprint/principal creation; it is **not** blanket authorization for child/credential management. Reusing an existing blueprint requires ownership or appropriate administrator authority. Broader alternatives, credential-management permissions, resource consent and role elevation are never silently requested.

Agent 365 tenant eligibility, required Agent 365 licensing/preview access, and accepted product terms must already be in place. Registration policy may still deny an otherwise authorized request. The registration API here is **undocumented preview**, so its exact delegated permission is grounded in the current Microsoft CLI source, not a claim of a stable public Graph contract.

Foundry setup caller needs **Foundry User** on the target project (formerly Azure AI User), read access to its telemetry connection, and read access to the selected Application Insights ARM resource. Trace viewers need **Reader or Monitoring Reader** on that Insights resource. Azure role assignments require an authorized RBAC administrator; Contributor alone cannot grant roles.

Browser sign-in uses MSAL authorization-code/PKCE, a 180-second timeout, an in-memory token cache, and the explicitly configured provisioning client. Finish sign-in on the **machine running the notebook kernel**, with port 8400 available. A remote/headless kernel needs a separately secured loopback/browser arrangement. Sign-in URLs, tokens and response bodies are never printed. Azure CLI credentials are used for Azure/Foundry administration, **not** for Agent ID Graph requests.

### Runtime access

| Principal | Access |
| --- | --- |
| App Service **system-assigned identity** | Cognitive Services OpenAI User (`5e0bd9bd-7b93-4f28-af87-19fc36ad61bd`) at the model account scope; inherited unconditional assignments are accepted |
| Same system identity, default dual mode | Monitoring Metrics Publisher (`3913510d-42f4-4e42-8a64-420c390055eb`) on linked Application Insights for Entra-authenticated ingestion |
| Dedicated attached UAMI | Managed identity assertion accepted by an exact FIC on the blueprint |
| Blueprint -> existing child | FMI/app-only Observability exchange, authorized by eligible Agent 365 registration/service policy |

Do **not** put provisioning Graph permissions on the child or host. The registered child uses the S2S path without an extra Observability permission/admin-consent step where service policy allows it. On rejection, diagnose registration/policy; do not automatically add `OtelWrite` or switch to delegated authentication.

## Architecture and ID map

```text
Authorized setup caller + public provisioning client
  -> Entra blueprint -> blueprint principal -> child identity
  -> Agent 365 registration of that child
  -> Foundry External Agent (otel_agent_id = child GUID)
  -> protected .local runtime settings

App Service
  system-assigned MI -> Foundry model
                     -> Azure Monitor -> project-linked App Insights -> External Agent Traces
  dedicated UAMI -> blueprint FIC -> blueprint FMI -> child app-only token -> Agent 365
                        same invocation/inference/output trace IDs at both destinations
```

| Value | Purpose / source |
| --- | --- |
| `TenantId` / `ENTRA_TENANT_ID` | Deployment's Entra tenant GUID |
| `SubscriptionId`, `ResourceGroup`, `webAppResourceId` | Azure deployment scope; bind local state to the exact host |
| `systemAssignedPrincipalId` | Host's system identity **object ID** for model/Monitor RBAC |
| `managedIdentityResourceId` | ARM ID used to attach the dedicated UAMI |
| `AZURE_MANAGED_IDENTITY_CLIENT_ID` | Dedicated UAMI **client ID**, selects runtime blueprint credential |
| `managedIdentityPrincipalId` | Dedicated UAMI **object/principal ID**, used as FIC subject |
| `ENTRA_PROVISIONING_CLIENT_ID` | Administrative public client; notebook only, never runtime |
| `ONBOARDING_SPONSOR_USER_ID` | Explicit real human sponsor; resolved and checked against the child's sponsors |
| `blueprintObjectId` / optional `ENTRA_BLUEPRINT_OBJECT_ID` | Blueprint directory object; setup FIC operations only |
| `ENTRA_BLUEPRINT_CLIENT_ID` | Blueprint **app/client ID**, FMI authentication and telemetry blueprint attribution |
| `blueprintPrincipalId` | Blueprint service principal object; verified separately |
| `ENTRA_AGENT_IDENTITY_OBJECT_ID` / `childAgentId` | Typed Agent ID child GUID; FMI child, Agent 365 attribution and root `gen_ai.agent.id` |
| `registrationRequestId`, `registrationId` | Stable Agent 365 request GUID and service-returned registration ID, often `T_...` |
| `FOUNDRY_PROJECT_ENDPOINT`, `FOUNDRY_MODEL` | Model project endpoint and existing deployment |
| `FOUNDRY_TELEMETRY_PROJECT_ENDPOINT` | Optional **explicitly different** registration/trace project; otherwise model project |
| `FOUNDRY_EXTERNAL_AGENT_NAME` | Stable Foundry metadata record name, **not** telemetry identity |
| Foundry registration ID/version | Read back and persisted; reruns do not create new revisions |
| `otel_agent_id` | Exactly the same child GUID as `gen_ai.agent.id` and Agent 365 |
| `APPLICATIONINSIGHTS_RESOURCE_ID` | ARM ID of the resource connected to the telemetry project |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | Export destination configuration, never a bypass of ingestion policy |
| `conversation_id`, `trace_id` | Correlation only; neither authorizes a caller |

Agent identities are service-principal-only objects; the typed contract returns a child `id`. This sample follows that contract and refuses a response with a divergent `appId` rather than guessing between identities. Final Observability JWT routing guards verify tenant, child client ID, audience, app-only type and lifetime.

The existing system identity remains useful and is explicitly selected with `ManagedIdentityCredential()` for hosted model access and Azure Monitor. The UAMI is the **documented blueprint-federation path chosen here**, not evidence that Agent 365 universally prohibits system identities. Do not configure global `AZURE_CLIENT_ID`; these scripts reject it to avoid credential ambiguity.

## A. Run locally, then deploy a working agent

From this repository root:

```powershell
uv venv .venv --python 3.12
uv pip install --python .venv\Scripts\python.exe `
  --index-url https://packagefeedproxy.microsoft.io/pypi/simple -r requirements-dev.txt
Copy-Item .env.example .env
az login --tenant '<TENANT-GUID>'
```

Edit `.env`: set `FOUNDRY_PROJECT_ENDPOINT` and `FOUNDRY_MODEL`. Keep `ENABLE_AGENT365_TELEMETRY=false`; leave telemetry-only credentials blank. Locally the model uses your Azure CLI identity, so that user needs model access.

```powershell
.\.venv\Scripts\python.exe -m uvicorn src.app:app --host 127.0.0.1 --port 8000
```

In another PowerShell terminal:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
$body = @{ message = 'Explain what this sample does.' } | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8000/chat -Method Post -ContentType application/json -Body $body
```

`GET /health` returns only `{"status":"ok"}`. `POST /chat` accepts a nonblank string up to 8,000 characters, and an optional 1-128 character correlation ID using letters/digits/`._:-`. Bodies over 40,000 bytes are rejected. The response has `reply`, `conversation_id`, and `trace_id` (`null` while tracing is disabled). Each call is **single turn**, even when a conversation ID is reused.

Set real deployment values before running these commands:

```powershell
$deploy = @{
    TenantId               = '<TENANT-GUID>'
    SubscriptionId         = '<SUBSCRIPTION-GUID>'
    ResourceGroup          = 'rg-agent365-learning'
    Location               = 'westeurope'
    WebAppName             = '<GLOBALLY-UNIQUE-APP-NAME>'
    FoundryProjectEndpoint = 'https://<ACCOUNT>.services.ai.azure.com/api/projects/<PROJECT>'
    ModelDeployment        = '<MODEL-DEPLOYMENT-NAME>'
    ModelAccessScope       = '/subscriptions/<SUBSCRIPTION-GUID>/resourceGroups/<MODEL-RG>/providers/Microsoft.CognitiveServices/accounts/<ACCOUNT>'
    CallerCidrs            = @('<CALLER-PUBLIC-IP>/32')
    ScmCallerCidrs         = @('<DEPLOYMENT-MACHINE-PUBLIC-IP>/32')
    Sku                    = 'B1'
}

# Read-only Azure preview. Review resource changes and regional charges.
.\scripts\deploy.ps1 @deploy -WhatIf

# Explicit approval for billable resources/source deployment and the narrow model role.
# Omit -AssignModelRole if an administrator already granted the required access.
.\scripts\deploy.ps1 @deploy -Approve -AssignModelRole
.\scripts\verify.ps1 -DeploymentFile .\.local\deployment.json
```

Missing model access without the role switch prints a precise administrator command and stops. Deployment uses Bicep plus supported App Service ZIP/OneDeploy and remote Oryx build; the archive contains runtime source and locked requirements, not `.env`, notebooks, `.venv` or setup state. Source upload is asynchronous, followed by bounded polling of the exact accepted deployment ID, so a long Oryx build does not depend on one long-lived HTTP response. Existing system principal, role assignments, attached identities and unrelated app settings are preserved. Infrastructure-owned startup/runtime/access settings are intentionally reapplied. Redeploying deliberately disables telemetry again; reactivate after onboarding.

**Do not proceed to onboarding until the hosted agent answers a real prompt.** Health alone does not test model authorization. If your IP changes, rerun with the revised explicit CIDR arrays, reviewing restrictions before approval. Update SCM separately when the deployment source changes. Do not use `0.0.0.0/0` or copy site restrictions blindly onto SCM.

### Alternative: public networking with Microsoft Entra authentication

Use this mode when your network has rotating or destination-specific outbound IPs. A generic IP lookup can report a different address from the one Azure sees; a blocked App Service request reports its observed address in `x-ms-forbidden-ip`. Do not keep adding rotating proxy IPs or use an unrestricted anonymous `/chat`.

This mode uses **App Service built-in authentication**, not custom JWT validation. Every authenticated user in the deployment's tenant may call the API through Azure CLI, subject to tenant policy; there is no individual-user allowlist. Anonymous requests and other issuers/audiences are rejected. Azure CLI is the configured public client; other client applications require a reviewed configuration change.

The setup caller must be permitted to create/manage an ordinary single-tenant app registration and its service principal. This registration is **separate from the Agent 365 blueprint/provisioning client**. It exposes only this API's `user_impersonation` scope and preauthorizes Azure CLI for that scope; it requests no Microsoft Graph workload permissions and creates no client secret. This is a bearer-token API: it does not add a browser sign-in frontend or an implicit-flow login.

After defining `$deploy` above:

```powershell
.\scripts\configure-http-auth.ps1 `
  -TenantId $deploy.TenantId -SubscriptionId $deploy.SubscriptionId `
  -ResourceGroup $deploy.ResourceGroup -WebAppName $deploy.WebAppName -WhatIf

# Explicit approval for the dedicated API application, principal, and own-API preauthorization.
.\scripts\configure-http-auth.ps1 `
  -TenantId $deploy.TenantId -SubscriptionId $deploy.SubscriptionId `
  -ResourceGroup $deploy.ResourceGroup -WebAppName $deploy.WebAppName -Approve

$deploy.HttpAuthFile = '.\.local\http-auth.json'
.\scripts\deploy.ps1 @deploy -Approve -AssignModelRole
.\scripts\verify.ps1 -DeploymentFile .\.local\deployment.json
```

With `HttpAuthFile`, supplied CIDRs are intentionally ignored. Infrastructure first denies network access; the script configures and reads back mandatory tenant-specific HTTPS authentication, then opens site/SCM networking. SCM still requires Entra deployment RBAC and keeps basic publishing disabled. Use `HttpAuthFile` on every redeployment; otherwise the original CIDR requirements apply.

For repeat deployments, keep the same parameters and protected `.local` state, and rerun `deploy.ps1` with the same switches. API setup reuses its saved registration. If you delete only the App Service and recreate it with the same name, its system-assigned principal changes: include `-AssignModelRole` so the new principal receives model access (the caller must have permission to grant it). Deleting the app is not necessary for normal redeployment. The script waits for configuration-triggered restarts before uploading source and does not report readiness until build and request checks succeed.

Deployment saves the API client ID in its protected artifact. Verification and activation automatically acquire caller tokens for that API; those tokens are unrelated to the managed-identity model/telemetry tokens. No bearer values are printed or saved. If token acquisition needs fresh consent/sign-in, the script prints the exact `az login --tenant ... --scope ...` command.

Public-mode deployment checks that a caller token can be acquired before changing Azure resources, so a missing initial API-scoped sign-in stops early rather than after a build.

For a manual authenticated call:

```powershell
$d = Get-Content .\.local\deployment.json -Raw | ConvertFrom-Json
$token = az account get-access-token --tenant $d.tenantId `
  --scope "api://$($d.httpAuthClientId)/user_impersonation" --query accessToken -o tsv
$headers = @{ Authorization = "Bearer $token" }
Invoke-RestMethod "https://$($d.hostname)/chat" -Method Post `
  -Headers $headers -ContentType application/json `
  -Body (@{ message = 'Hello' } | ConvertTo-Json)
$token = $null
$headers.Clear()
```

Hosted `/health` is protected too. Deployment/verification check that anonymous and invalid-token `POST /chat` requests return **401**, then make one authenticated model request. Local development remains unchanged and is loopback-only by default.

Entra authentication is not rate limiting or a cost cap: every permitted tenant user can incur model charges. Production/multi-user deployments still need appropriate authorization, rate limiting, cost controls and abuse protection.

### Talk to the deployed agent

Open [00_talk_to_agent.ipynb](notebooks/00_talk_to_agent.ipynb), select this repository's `.venv` kernel, and run the cells. It uses saved deployment/authentication state and your Azure CLI identity to check health and send messages. It also works before a readiness artifact exists by reading `.local\http-auth.json` and looking up the app hostname; set `AGENT_URL` in the notebook to avoid that ARM lookup. Edit the message cell for each single-turn conversation. Health success does not prove model access; chat errors are reported separately. Model calls may incur charges. Clear conversation outputs before saving or sharing.

## B. Register through the notebook

Open [01_register_with_agent365.ipynb](notebooks/01_register_with_agent365.ipynb), select `.venv` and run sections in order. Add the notebook-only values to `.env` first:

```dotenv
ENTRA_PROVISIONING_CLIENT_ID=<CONSENTED-PUBLIC-CLIENT-GUID>
ONBOARDING_SPONSOR_USER_ID=<EXISTING-HUMAN-SPONSOR-GUID>
FOUNDRY_EXTERNAL_AGENT_NAME=AppServiceLearningAgentExternal
APPLICATIONINSIGHTS_RESOURCE_ID=/subscriptions/<SUBSCRIPTION>/resourceGroups/<RG>/providers/Microsoft.Insights/components/<RESOURCE>
APPLICATIONINSIGHTS_CONNECTION_STRING=<COPY-FROM-LINKED-RESOURCE>
```

Obtain the connection string from Foundry **Manage > Project details > Connected resources > Application Insights**, or the selected Insights resource's **Overview** in Azure Portal. Keep it in ignored `.env`; do not paste it into a notebook cell/output. If registering in another project, set `FOUNDRY_TELEMETRY_PROJECT_ENDPOINT` explicitly; that choice is persisted separately from the model project.

`SETUP` creates/reuses identities and exact FIC; `REGISTER` registers the existing child and verifies the returned binding; `LINK` creates/reuses the exact External Agent via `ExternalAgentDefinition(otel_agent_id=child_guid)`. Setup checks the linked project's connection against the supplied Insights ARM resource and connection string. A missing read permission is **unverified**, not success; the notebook gives the manual portal check and blocks activation until an authorized reader completes verification.

Ignored, access-restricted artifacts:

- `.local\deployment.json`: deployed host/identity/model outputs.
- `.local\http-auth.json`: optional HTTP API application/principal IDs, scope and retry state; no credentials.
- `.local\onboarding-state.json`: identity IDs, ownership/creation flags, retry intent, sponsor metadata, registration and Foundry ID/version.
- `.local\runtime-settings.json`: activation settings, sponsor metadata and Insights destination configuration.

Back these up securely for retry/cleanup; do not share or commit them. State must match tenant/deployment/client. Ambiguous writes reconcile the same objects/request; if still unresolved, stop and inspect the service before changing anything. To resume an existing registration whose service-generated ID was not recovered, have an authorized administrator locate it and set **only** the correct `registrationId` in the protected state; subsequent readback must match the child, blueprint and managing client. Never delete state or clear pending flags merely to force another create.

Rerun the notebook to demonstrate reuse of the same blueprint, principal, child, FIC and external version. Identity/registration readback is not proof of telemetry delivery.

## C. Activate both destinations

Instrumentation is already in source; activation updates settings and restarts the app, not source files on the host. It rereads Azure identity attachments, Graph relationships/FIC/sponsor/registration, Foundry kind/version/telemetry ID, and the project-linked Insights destination. Settings are merged additively without printing values.

```powershell
.\scripts\enable-observability.ps1 `
  -DeploymentFile .\.local\deployment.json `
  -SettingsFile .\.local\runtime-settings.json -WhatIf

# Explicitly authorize activation; authorize the narrow ingestion role only if needed.
.\scripts\enable-observability.ps1 `
  -DeploymentFile .\.local\deployment.json `
  -SettingsFile .\.local\runtime-settings.json -Approve -AssignMonitoringRole
```

If Monitoring Metrics Publisher is missing and its switch is omitted, activation prints the administrator action and stops before applying settings. `A365_AZURE_MONITOR_USE_ENTRA=true` passes the authorized system identity credential explicitly to the distro's `azure_monitor_exporter_credential`; connection strings alone do not authorize ingestion when local authentication is disabled. This learning activation intentionally requires Entra-authenticated ingestion.

`ENABLE_AGENT365_TELEMETRY=true` activates one pipeline. `A365_EXPORT_TO_FOUNDRY=true` is the default and requires the verified external registration and Insights connection. For an **explicit Agent 365-only opt-out**, select `AGENT365_ONLY=True` in the notebook and regenerate settings; add `-Agent365Only` to activation/verification, and omit `-AssignMonitoringRole`. Foundry External Agent observability is then **not configured**, never silently downgraded.

The runtime uses a synchronous, thread-safe resolver (no `asyncio.run` inside an event loop): UAMI token -> blueprint client-credentials `fmi_path=child` assertion -> child app-only token scoped to `api://9b975845-388f-4429-889e-eab1ef63949c/.default`. Tokens are keyed/bound to one tenant/child, renewed 60 seconds early, and concurrent refreshes serialized. Failures clear the cached token and use a bounded five-second retry backoff. No expired/delegated token is returned.

Manual distro scopes own invocation, child inference and output spans; framework/OpenAI/HTTP automatic instrumentation is disabled to avoid duplicates and health/setup traces. Tenant/blueprint/child/conversation context is applied before spans; async requests isolate context. **Content capture is off**: schema-valid redacted message structures are emitted. `A365_CAPTURE_CONTENT=true` is an explicit opt-in for non-sensitive test data only. The real sponsor is labeled as accountability metadata, not a signed-in caller; no agent user or authenticated end user is fabricated.

Operational logs stay with App Service logging; the pipeline exports activity spans, not application logs. Both exporters' offline spooling is disabled: failed spans can be lost, and shutdown flush is best-effort, not a delivery guarantee. Invalid enabled settings fail startup; temporary token/export failures produce sanitized diagnostics without blocking an otherwise valid reply.

The pinned distro's Azure Monitor path requires `OTEL_LOGS_EXPORTER=none` and `OTEL_METRICS_EXPORTER=none` to keep those signals off; initialization sets them explicitly. A small version-bound startup check inspects the installed provider's exporter shape because this distro can swallow exporter initialization errors. It refuses a missing/duplicate destination instead of silently downgrading dual export. Revalidate this check when updating dependencies. Conflicting OTLP settings or disabled trace export are rejected.

## D. Invoke and verify each outcome

```powershell
.\scripts\verify.ps1 `
  -DeploymentFile .\.local\deployment.json `
  -SettingsFile .\.local\runtime-settings.json
```

The script sends one permitted request with a fresh correlation ID and prints the reply and trace ID. It can separately read back setup with the administrative helper. Export delivery and portals cannot be established from the HTTP reply.

| Outcome | Evidence / status |
| --- | --- |
| Deployment/model | `/chat` reply from hosted app |
| Agent 365 registration | Live readback matches child, blueprint and managing client |
| Runtime app-only token | `Observability app-only token acquired` operational diagnostic |
| Agent 365 transport | Sanitized exporter `Agent365 transport HTTP ...` diagnostic; retain its correlation ID |
| Agent 365 Activity | **Pending** until a human finds this child's activity/correlation in the eligible Agent 365 management/Activity surface |
| Foundry registration | Live External Agent kind, project, version and `otel_agent_id` readback |
| Foundry ingestion + agent-scoped visibility | **Pending** until the same trace appears under **Build > Agents > external name > Traces**, with matching child GUID |

Enable/inspect App Service filesystem application logs with the Azure Portal **Log stream**, or an explicitly configured Azure logging destination. The app logs neither bearer/assertion values nor token endpoint bodies. The Agent 365 exporter diagnostic filter withholds response bodies/headers while preserving HTTP status/correlation and warnings. **SDK limitation:** pinned distro 1.6.0 classifies 2xx as transport delivery; it does not expose structured partial-success/rejected-span counts. Therefore a 2xx, `force_flush()`, or successful token acquisition is not a full ingestion/portal claim. Investigate service-side diagnostics and portal results; never infer complete acceptance solely from these signals.

In the linked Application Insights **Logs** (resource-context table names):

```kusto
dependencies
| union requests
| where timestamp > ago(1h)
| where operation_Id == "<TRACE-ID-FROM-VERIFY>"
| project timestamp, operation_Id, name,
          agentId=tostring(customDimensions["gen_ai.agent.id"]),
          conversation=tostring(customDimensions["gen_ai.conversation.id"])
| order by timestamp asc
```

Workspace-context equivalents are `AppDependencies`/`AppRequests`, `TimeGenerated`, `OperationId` and `Properties`. Confirm the root `invoke_agent` has `gen_ai.agent.id = childAgentId = registered otel_agent_id`. Raw Insights ingestion alone does **not** satisfy the Foundry External Agent criterion. Open Foundry **Build > Agents > the registered external agent > Traces** and confirm the same trace. Indexing is typically 2-5 minutes and can take longer. Record the trace ID and child GUID privately, and mark portal outcomes verified only after observing them.

## Troubleshooting

| Symptom | Check / action |
| --- | --- |
| Infrastructure rejected with insufficient App Service quota | Azure Portal **Quotas > App Service**: request instance quota for the selected SKU/region (for B1, at least 1). Region/SKU changes require a deliberate choice; the script never upgrades or moves the app automatically. |
| Hosted `/chat` fails, health succeeds | System identity model role/account scope, model deployment/Responses support, endpoint/network; not blueprint permissions |
| Caller/SCM gets 403 | Correct public IP/CIDR and separate site/SCM restrictions; allowlisting is not sign-in |
| IP lookup matches allowlist but Azure denies access | Inspect `x-ms-forbidden-ip`; corporate routing can use rotating/destination-specific egress. Use approved fixed egress/ranges, or explicitly select public Entra mode above. |
| Public Entra API returns 401/403 | Obtain a token for the saved **HTTP API audience**, from the configured tenant through Azure CLI. ARM/Graph/model tokens are not interchangeable. Follow the printed scoped-login command when needed. |
| ZIP upload reports 502 or a build wait expires | Inspect `az webapp log deployment list/show` first: the server may still be building. Reconcile/wait on the accepted deployment ID rather than uploading a duplicate. |
| FIC mismatch / blueprint FMI failure | Issuer tenant, exact UAMI **principal ID** subject, audience; runtime selects UAMI **client ID** |
| Graph 401/403 or unsupported preview | Provisioning client consent, caller ownership/role, tenant eligibility and current contract; do not treat as not-found or elevate automatically |
| Registration ambiguous/conflict | Preserve request ID/state; read back the existing registration and reconcile, never create another child |
| Observability token rejected | Child/tenant/audience/app-only claims, exact trust, eligible registration and service policy; no `OtelWrite` auto-grant or OBO fallback |
| Export HTTP rejection/throttling | Separate token success from delivery; use sanitized HTTP/correlation diagnostics; retry with another non-sensitive request after resolving policy/connectivity |
| Agent 365 Activity delayed | Correct child and time window, licensing/policy and indexing; flush or HTTP 200 is not visibility |
| Raw Insights traces, no external agent | External registration missing/wrong project, kind or `otel_agent_id` mismatch |
| Foundry external record, no traces | Wrong linked resource/connection, root ID mismatch, ingestion authorization/network, or indexing delay |
| Viewer cannot see traces | Foundry project access plus Reader/Monitoring Reader on linked Insights; raw resource access and project access are separate |
| Startup fails only after enabling | Complete GUID/settings validation, explicit dual-mode connection/registration, sponsor metadata; disable telemetry to restore baseline while fixing |
| Local admin browser times out | Port 8400, exact desktop redirect, browser on kernel machine, Conditional Access; retry bounded sign-in, never copy sensitive auth URLs |

## Disable, adapt, clean up

Disable exporters without deleting identities/registrations:

```powershell
$d = Get-Content .\.local\deployment.json -Raw | ConvertFrom-Json
az webapp config appsettings set --ids $d.webAppResourceId `
  --settings ENABLE_AGENT365_TELEMETRY=false --output none
az webapp restart --ids $d.webAppResourceId --output none
```

For another repository, copy/review the minimal token resolver and instrumentation into **source**, initialize before framework imports, retain its existing host system identity and model credential selection, configure exact UAMI blueprint federation and the existing child registration, then deploy through your normal pipeline. Add only scopes relevant to your actual operations; do not invent tools. Preserve the shared child GUID for both products. Source changes always need deployment; activation alone only changes settings. Never patch files inside a running App Service or copy notebook administrative authentication into it.

Cleanup is deliberate and targeted. Inspect `.local\onboarding-state.json`'s `created` flags and the actual resources before deleting anything. Stop/delete **only this sample's web app**; delete its named plan only if unused, and the dedicated UAMI only if no other resource/trust uses it. Do not delete the whole resource group unless you have independently established it contains only disposable sample resources.

```powershell
$d = Get-Content .\.local\deployment.json -Raw | ConvertFrom-Json
# Explicit targeted examples; review IDs first.
az webapp delete --ids $d.webAppResourceId
# Remove sample-created role assignments at the exact scopes using their assignment IDs.
# Delete the named sample plan/UAMI separately only after checking no shared usage.
```

An authorized administrator can remove a **sample-created** Agent 365 record using `DELETE /beta/copilot/agentRegistrations/{registrationId}`, and a sample-created Foundry external record with `project.agents.delete(agent_name=external_name)` in the recorded project. Remove only this sample's FIC by its saved ID; remove the child service principal and then blueprint/principal **only when sample-created and unused**. For optional HTTP authentication, inspect `.local\http-auth.json` and remove its dedicated API application/principal only when sample-created and unused; never delete a shared API registration. Deletion requires separately consented operation-specific delete/credential permissions (for example `AgentIdentity.DeleteRestore.All` for child deletion and appropriate blueprint delete rights); the default setup client is intentionally not an automatic cleanup superuser. Never delete reused/shared identities, Foundry projects/models or Application Insights. Delete protected local artifacts only after service-side cleanup/reconciliation is complete.

## Local validation and API references

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check src tests
.\.venv\Scripts\ruff.exe format --check src tests
az bicep build --file .\infra\main.bicep --stdout | Out-Null
```

`requirements.in` holds direct pins; generated `requirements.txt` locks compatible runtime transitive dependencies for Windows/Linux. When deliberately updating:

```powershell
uv pip compile requirements.in --python .venv\Scripts\python.exe --universal `
  --index-url https://packagefeedproxy.microsoft.io/pypi/simple --output-file requirements.txt
uv pip install --python .venv\Scripts\python.exe `
  --index-url https://packagefeedproxy.microsoft.io/pypi/simple -r requirements-dev.txt
```

Checked on **2026-11-23**: Agent Framework core 1.26.0 / Foundry 1.16.2 / OpenAI 1.18.2, Projects 2.11.0, Microsoft OpenTelemetry 1.6.0. This resolves the older Foundry adapter's Projects `<2.8` incompatibility. Python tests use mock model/identity/transport services and real SDK activity scopes; they do not prove live delivery. Notebook validation requires valid JSON/schema, compilable Python, null execution counts and empty outputs. No live tenant deployment, directory writes, licensing checks or portal acceptance is implied by local validation.

- [Microsoft OpenTelemetry distro](https://learn.microsoft.com/microsoft-agent-365/developer/microsoft-opentelemetry)
- [Observability S2S authentication and token guards](https://learn.microsoft.com/microsoft-agent-365/developer/observability-authentication-setup)
- [Required activity attributes](https://learn.microsoft.com/microsoft-agent-365/developer/observability-attribute-reference)
- [Create blueprint](https://learn.microsoft.com/graph/api/agentidentityblueprint-post?view=graph-rest-1.0), [principal](https://learn.microsoft.com/graph/api/agentidentityblueprintprincipal-post?view=graph-rest-1.0), [child](https://learn.microsoft.com/graph/api/agentidentity-post?view=graph-rest-1.0)
- [Typed federated credential preview and permissions](https://learn.microsoft.com/graph/api/federatedidentitycredential-post?view=graph-rest-beta)
- [Microsoft registration contract source](https://github.com/microsoft/Agent365-devTools/blob/main/scripts/bulk-agent-registration/New-A365AgentRegistration.ps1)
- [Foundry External Agents, roles and trace binding](https://learn.microsoft.com/azure/foundry/agents/how-to/register-external-agent)
- [App Service agent identity context](https://learn.microsoft.com/azure/app-service/overview-agent-identity)
- [App Service Entra authentication and built-in issuer/audience/client checks](https://learn.microsoft.com/azure/app-service/configure-authentication-provider-aad)
- [Managed identity as federated credential](https://learn.microsoft.com/entra/workload-id/workload-identity-federation-config-app-trust-managed-identity)

The sibling notebook examples in `agent_365_sdk` informed the identity/FMI pattern. This starter intentionally omits their secret/OBO/tools paths, makes Foundry External Agent registration mandatory for default dual export, and separates deployment, onboarding and runtime activation.
