# Approved public Entra deployment

Status: Validated

## 1. Scope and approval

The user approved single-tenant public-network authentication, creation of a dedicated
HTTP API registration/principal, Azure CLI preauthorization for that API only, source
deployment, anonymous rejection checks, and one authenticated model request.
All tenant users are permitted; there is no individual-user allowlist.
The user separately approved this local deployment record.

## 2. Azure context

- Tenant: `7b3b2559-0c78-44e7-8c57-8455603aca36`
- Subscription: `7ee2b43a-eaea-4259-be7b-c8c220bfbcf9`
- Resource group: `rg-agent365-learning-eastus`
- Existing app: `agent365-memasanz-eastus`
- Actual existing app/plan region: **East US 2**
- Existing plan: `agent365-memasanz-eastus-plan`, **B1**
- Existing Foundry account: `admin-5729-resource`, model `gpt-6.1-sol`

Keep the current region, SKU, identities, unrelated settings, and model resources.
Do not perform Agent 365 or Foundry External Agent registration in this deployment.

## 3. Recipe and artifacts

recipe.type: bicep

- Infrastructure: `infra\main.bicep`
- API registration: `scripts\configure-http-auth.ps1`
- Configuration/source deployment: `scripts\deploy.ps1`
- Post-deployment verification: `scripts\verify.ps1`
- Ignored state: `.local\http-auth.json`, `.local\deployment.json`
- Runtime: FastAPI/Python 3.12, remote Oryx ZIP build, Uvicorn on port 8000

## 4. Authentication and networking

Create/reuse one dedicated, single-tenant HTTP API application and service principal.
Expose `user_impersonation` and preauthorize Azure CLI for this API only.
Do not create a client secret or request Microsoft Graph workload permissions.

Infrastructure denies site/SCM traffic during reconfiguration. Configure and read back
mandatory App Service authentication before opening public networking. Require HTTPS,
the exact tenant issuer/API audience, and the Azure CLI public client. Return HTTP 401
for anonymous requests; do not exclude `/health` or `/chat`.
SCM retains Entra deployment RBAC and disabled basic publishing.

## 5. Execution and costs

Run API setup and deployment with explicit `-Approve`; no new plan/model is provisioned.
Existing B1 charges continue. One successful authenticated model request incurs model
usage charges. Leave Agent 365 and Azure Monitor activity export disabled.
Failed or mismatched auth readback must stop before networking is opened.

## 6. Verification and recovery

- Require API registration and App Service auth readbacks.
- Confirm authenticated health succeeds.
- Confirm anonymous and invalid-token `POST /chat` return 401.
- Confirm one authenticated model request and write the readiness artifact.
- Preserve state on interrupted operations; reuse the same application/scope.
- If auth cannot be verified, keep/reinstate network denial rather than expose anonymous chat.
- No shared resources or registrations are deleted.

## 7. Validation proof

- [x] All validation checks pass
  - [x] Core validation: CLI, authentication, Bicep build, ARM validation, what-if
  - [x] Linting
  - [x] Azure Policy validation
  - [x] Static role verification and supported runtime check

Validation completed on 2026-11-23 at 23:45 UTC:

- Shared Bicep `validate-deployment.ps1`, resource-group scope, explicit subscription,
  `infra\main.bicep`, `.local\validation-params.json`: **OVERALL PASS**.
  CLI/auth, Bicep compilation, ARM validation and what-if passed. No deletions.
  The helper initially inherited strict mode and failed while counting empty what-if
  results; running it in its own clean PowerShell process completed successfully.
- `.venv\Scripts\python.exe -m pytest -q`: **81 passed**, one upstream deprecation warning.
- Ruff lint/format: **passed**. Tests parse all five PowerShell scripts.
- `python -m compileall -q src`: **passed**.
- `uv pip check --python .venv\Scripts\python.exe`: **126 packages compatible**.
- Azure Policy assignment lookup returned inherited governance/security assignments;
  ARM validation accepted the target template. Public authentication settings remain
  subject to enforced policy during their separate configuration PUT; no bypass is used.
- Static RBAC review: only Cognitive Services OpenAI User at the exact model account,
  explicitly gated. Telemetry remains disabled. No runtime Graph permissions are added.
- Runtime: existing Linux App Service **Python 3.12** and pinned Python dependencies.
  Node.js validation is not applicable; this project has no Node.js runtime.
- Live read-only checks confirmed existing app/plan region, SKU, identity attachments,
  model deployment, and the rotating-egress cause of the original SCM 403.

## 8. Live deployment recovery

- HTTP API registration and mandatory Easy Auth were configured and read back before
  public site/SCM networking was opened.
- Source deployment `0bfb76c2-5bb0-44d0-bf52-4b29e2ae3d3a` ultimately failed with
  Kudu status 3, complete true. Its status explicitly reports an SCM container restart
  following management operations and recommends delaying source upload.
- Captured startup output reports missing Oryx build manifest/environment and
  `No module named uvicorn`: dependencies were not installed by the interrupted build.
- Deployment now allows 60 seconds after configuration changes before source upload,
  submits asynchronously, and polls the exact accepted deployment ID. No additional
  infrastructure, registrations, role grants, or authentication exceptions are required.
- Source-only recovery `41978ef2-60e2-4d3c-9df7-6ceb88f03e66` **succeeded**.
  Its following verification exposed a separate CLI argument bug: the installed
  `Profile.get_raw_token` rejects subscription and tenant specified together.
  Caller-token acquisition now uses the explicitly validated subscription alone;
  its regression mock rejects combining both arguments.
- Public deployment now preflights caller-token acquisition before Azure writes.
  Repeat/recreate instructions retain local state and require `-AssignModelRole`
  when a recreated App Service has a new system-assigned principal.
- The user deferred live checks. Hosted health, anonymous/invalid-token rejection,
  and the authenticated model request remain unverified; no readiness artifact
  has been written. The anonymous probe timed out rather than proving rejection.
- Final local suite: **84 passed**. Deployment-script tests: **14 passed**.
  `ruff check src tests`, `ruff format --check src tests`, and `git diff --check` pass.
  Whole-repository Ruff additionally reports existing notebook style issues; the
  notebook was not changed during deployment recovery.
