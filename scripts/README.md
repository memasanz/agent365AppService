# Scripts

PowerShell 7+ scripts for provisioning, deploying, and verifying the sample. Run them from the repository root, in the order below. The main [README](../README.md) has the full walkthrough and parameter values.

| Order | Script | Purpose | Touches |
| --- | --- | --- | --- |
| 1 | `New-AgentIdentityProvisioningClient.ps1` | Create the app registration notebook 01 signs in with | Entra (Microsoft Graph), `.env` |
| 2a | `configure-http-auth.ps1` | Optional: create the Entra API registration for public mode | Entra (Azure CLI + Graph), `.local\http-auth.json` |
| 2b | `deploy.ps1` | Deploy infrastructure and source, then verify | Azure, `.local\deployment.json` |
| 3 | `verify.ps1` | Send one model request and compare readbacks | Read-only (one billable model call) |
| 5 | `enable-observability.ps1` | Turn on telemetry after notebook 01 | App Service settings, restart |
| - | `common.ps1` | Shared helpers dot-sourced by the scripts above | Not run directly |

Safe-by-default conventions used by every script that changes something:

- No `-Approve` (or `-WhatIf`) means a **preview only**; nothing is written.
- Role assignments need their own switch (`-AssignModelRole`, `-AssignMonitoringRole`); `-Approve` never grants roles.
- No secrets, passwords, or tokens are created, printed, or saved. Failure messages never include response bodies.
- Local state is saved in the protected `.local\` directory (current user only).

## `New-AgentIdentityProvisioningClient.ps1`

Creates (or reuses) an ordinary single-tenant **public client** app registration named `Agent365IdentityProvisioner` (override with `-Name`/`-DisplayName`). Notebook 01 uses it to sign in. It is not a managed identity and is separate from the HTTP API registration.

What it does, in order:

1. Prints a preview. Without `-Approve` or `-WhatIf` it stops before connecting to Graph.
2. Signs in to Microsoft Graph with `Application.ReadWrite.All` (plus `DelegatedPermissionGrant.ReadWrite.All` only with `-GrantAdminConsent`) and checks the tenant matches. These bootstrap permissions are not added to the new app.
3. Looks up the eight delegated Graph permissions the notebook needs and stops if any is unavailable.
4. Finds an existing app by name (or `-ApplicationObjectId`) and refuses ambiguous or non-single-tenant matches.
5. Creates or updates the app with redirect `http://localhost:8400`, keeping existing redirects and permissions.
6. Creates the service principal if missing.
7. With `-GrantAdminConsent`, creates or extends the tenant-wide consent grant, keeping existing scopes.
8. Reads everything back, then writes `ENTRA_PROVISIONING_CLIENT_ID` to `.env` (`-EnvFile` to change the path).

Functions:

| Function | What it does |
| --- | --- |
| `Invoke-ProvisioningGraph` | Calls Microsoft Graph with the signed-in session; on failure throws a generic message without the response body. |
| `Get-GraphCollection` | Reads every page of a Graph list, rejecting pagination links that leave `graph.microsoft.com`. |

## `configure-http-auth.ps1`

Public (Entra-authenticated) mode only. Creates or reuses a single-tenant API app registration named `<WebAppName>-http-api`, exposes a `user_impersonation` scope, and preauthorizes Azure CLI for it. It requests no Graph workload permissions and creates no secret. It does **not** change the web app; `deploy.ps1 -HttpAuthFile` applies it.

Needs `-TenantId`, `-SubscriptionId`, `-ResourceGroup`, `-WebAppName`, and `-Approve` to write. Progress and retry state go to `.local\http-auth.json`, so a rerun reuses the same registration.

## `deploy.ps1`

Deploys the App Service and the agent source, then proves it works. Uses your existing Foundry project and model; it never creates or changes them.

Steps:

1. **Validate** the Azure CLI version, tenant/subscription, Foundry endpoint, model account scope and model deployment, and either the CIDR lists or the `-HttpAuthFile` artifact.
2. **Inspect** any existing app (must be Linux, no global `AZURE_CLIENT_ID`, no `WEBSITE_RUN_FROM_PACKAGE`) and any existing managed identity (must be dedicated to this app).
3. **Preview** the plan, resources, costs, and an ARM what-if. Stops here without `-Approve`.
4. **Deploy infrastructure** from `infra\main.bicep` (plan, web app, dedicated managed identity), creating the resource group if needed.
5. **Public mode only:** configure single-tenant bearer authentication, verify it, then open networking.
6. **Apply app settings** additively, with telemetry off.
7. **Check the model role** for the app's system-assigned identity; assign it only with `-AssignModelRole`, otherwise print the administrator command and stop.
8. **Zip** `requirements.txt` and `src\*.py` only (no `.env`, notebooks, or `.venv`), wait 60 seconds for configuration restarts to settle, and upload asynchronously.
9. **Poll** that exact build until it succeeds or fails.
10. **Verify:** settings readback, `/health`, anonymous and invalid-token `/chat` return 401 (public mode), and one real model reply.
11. **Save** `.local\deployment.json`, the artifact the later scripts and notebooks read.

No readiness artifact is written if any check fails.

## `verify.ps1`

Read-only, apart from one billable model request. Confirms the live deployment matches `.local\deployment.json`, checks the app's model settings, sends one message to `/chat`, and with `-SettingsFile` also validates the Agent 365/Foundry relationships. It states plainly which results are readbacks and not proof that telemetry reached the portals.

## `enable-observability.ps1`

Run after notebook 01. Validates `.local\runtime-settings.json` against the live deployment and the Agent 365/Foundry relationships, then (with `-Approve`) merges the telemetry settings into the App Service and restarts it. `-AssignMonitoringRole` separately authorizes the Monitoring Metrics Publisher role on the linked Application Insights; `-Agent365Only` is the explicit opt-out of Foundry export. Redeploying with `deploy.ps1` turns telemetry off again.

## `common.ps1`

Dot-sourced (`. .\scripts\common.ps1`) by the scripts above. It sets strict mode, stops on errors, and defines the role IDs and the Azure CLI client ID.

### Running Azure CLI and local files

| Function | What it does |
| --- | --- |
| `Invoke-Az` | Runs `az` and returns parsed JSON. Captures all output so errors can't leak request bodies, and gives a hint for known failures such as App Service quota. |
| `Get-LocalPath` | Resolves a path against the current PowerShell location, not the process directory. |
| `Read-JsonFile` | Reads a JSON artifact into a hashtable, without logging its contents on error. |
| `Protect-LocalDirectory` | Creates a directory only the current user can access (ACL on Windows, `chmod 700` elsewhere). |
| `Write-ProtectedJson` | Writes JSON to a file only the current user can read (ACL on Windows, `chmod 600` elsewhere). |

### Input validation

| Function | What it does |
| --- | --- |
| `Assert-Guid` | Requires a valid, non-empty GUID. |
| `Assert-ProjectEndpoint` | Requires a commercial-cloud HTTPS Foundry project endpoint (`https://ACCOUNT.services.ai.azure.com/api/projects/PROJECT`). |
| `Assert-Cidrs` | Requires 1-100 valid CIDRs and rejects `/0` and IPv4-mapped IPv6 ranges. |
| `Assert-AzureContext` | Confirms the Azure CLI is signed in to the intended tenant and an enabled subscription. |

### Checking the deployment

| Function | What it does |
| --- | --- |
| `Assert-Deployment` | Checks the deployment artifact has every required field, then compares it with the live app, managed identity, and hostname. Also runs the authentication check. |
| `Assert-HttpAuthRegistration` | Checks the HTTP API registration still matches the saved IDs: single tenant, `user_impersonation` scope, Azure CLI preauthorized. |
| `Assert-HttpAuthentication` | Checks the web app requires HTTPS bearer authentication on every route, with the expected issuer, audiences, and client. Skipped when not in public mode. |
| `Enable-PublicEntraAccess` | Applies that authentication configuration, reads it back, and only then opens site and SCM networking. |
| `Assert-AnonymousChatBlocked` | Confirms `/chat` returns 401 with no token and with an invalid token. |

### Calling the app

| Function | What it does |
| --- | --- |
| `Get-CallerHeaders` | Gets an Azure CLI token for the HTTP API and returns the `Authorization` header (empty when not in public mode). Prints the exact `az login` command if sign-in is needed. |
| `Wait-AppHealth` | Retries `/health` for up to about six minutes. |
| `Invoke-BaselineChat` | Sends one `/chat` request and checks the reply and conversation ID. |

### Roles and settings

| Function | What it does |
| --- | --- |
| `Test-RoleAssignment` | Returns whether a principal has an unconditional role at, or inherited into, a scope. |
| `Get-RoleAdminAction` | Builds the `az role assignment create` command an administrator can run. |
| `Get-AppSettings` | Reads the web app's settings into a hashtable. |
| `Set-MergedAppSettings` | Adds or updates the given settings and leaves all others unchanged. |

### Build and runtime checks

| Function | What it does |
| --- | --- |
| `Wait-SourceDeployment` | Polls one specific source deployment ID until it succeeds, fails, or times out, ignoring other deployments. |
| `Invoke-RuntimeReadback` | Runs `python -m src.onboarding validate-runtime` from the repository `.venv` to check the identity, registration, and federation settings without changing anything. |
