"""Administrative notebook helpers. Never imported by the hosted application."""

import argparse
import getpass
import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlencode
from uuid import uuid4

import msal
import requests
from azure.ai.projects.models import ExternalAgentDefinition
from azure.core.exceptions import AzureError, ResourceNotFoundError

from src.observability import TelemetrySettings, project_endpoint
from src.telemetry_auth import guid

GRAPH = "https://graph.microsoft.com"
REGISTRATIONS = "/beta/copilot/agentRegistrations"
SCOPES = (
    "AgentIdentityBlueprint.Create",
    "AgentIdentityBlueprint.Read.All",
    "AgentIdentityBlueprintPrincipal.Create",
    "AgentIdentityBlueprintPrincipal.Read.All",
    "AgentIdentity.Create.All",
    "AgentIdentity.Read.All",
    "AgentRegistration.ReadWrite.All",
    "User.ReadBasic.All",
)


def save_private(path, value):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        account = f"{os.environ['USERDOMAIN']}\\{getpass.getuser()}"
        subprocess.run(
            ["icacls", str(path.parent), "/inheritance:r", "/grant:r", f"{account}:(OI)(CI)F"],
            check=True,
            capture_output=True,
        )
    else:
        path.parent.chmod(0o700)
    temp = path.with_suffix(".tmp")
    descriptor = os.open(temp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        json.dump(value, file, indent=2)
    temp.replace(path)


def load_state(path, deployment, provisioning_client_id):
    deployment = validate_deployment(deployment)
    client_id = guid(provisioning_client_id, "ENTRA_PROVISIONING_CLIENT_ID")
    state = (
        json.loads(Path(path).read_text(encoding="utf-8"))
        if Path(path).exists()
        else {
            "schemaVersion": 1,
            "deployment": deployment,
            "provisioningClientId": client_id,
            "registrationRequestId": str(uuid4()),
            "created": {},
        }
    )
    if (
        state.get("schemaVersion") != 1
        or state.get("deployment") != deployment
        or state.get("provisioningClientId") != client_id
    ):
        raise ValueError(
            "Onboarding state belongs to another deployment/client; do not overwrite it."
        )
    return state


def validate_deployment(deployment):
    for key in (
        "tenantId",
        "subscriptionId",
        "systemAssignedPrincipalId",
        "managedIdentityClientId",
        "managedIdentityPrincipalId",
    ):
        if deployment.get(key) != guid(deployment.get(key), key):
            raise ValueError(f"{key} must be a canonical GUID.")
    for key in (
        "resourceGroup",
        "webAppName",
        "webAppResourceId",
        "hostname",
        "managedIdentityResourceId",
        "foundryProjectEndpoint",
        "modelDeployment",
        "modelAccessScope",
    ):
        if not isinstance(deployment.get(key), str) or not deployment[key].strip():
            raise ValueError(f"Deployment output missing {key}.")
    project_endpoint(deployment["foundryProjectEndpoint"])
    expected = (
        f"/subscriptions/{deployment['subscriptionId']}/resourceGroups/"
        f"{deployment['resourceGroup']}/providers/Microsoft.Web/sites/{deployment['webAppName']}"
    )
    if deployment["webAppResourceId"].lower() != expected.lower():
        raise ValueError("Web app resource ID does not match deployment scope.")
    return deployment


def sign_in(tenant_id, provisioning_client_id, *, read_only=False):
    permissions = (
        (
            "AgentIdentityBlueprint.Read.All",
            "AgentIdentityBlueprintPrincipal.Read.All",
            "AgentIdentity.Read.All",
            "AgentRegistration.ReadWrite.All",
            "User.ReadBasic.All",
        )
        if read_only
        else SCOPES
    )
    app = msal.PublicClientApplication(
        guid(provisioning_client_id, "provisioning client"),
        authority=f"https://login.microsoftonline.com/{guid(tenant_id, 'tenant')}",
        timeout=(10, 30),
    )
    try:
        result = app.acquire_token_interactive(
            scopes=[f"{GRAPH}/{p}" for p in permissions],
            port=8400,
            timeout=180,
            prompt="select_account",
            welcome_template=(
                '<html><body>Agent onboarding: <a href="$auth_uri">Continue sign-in</a>. '
                "Keep this page private.</body></html>"
            ),
            auth_uri_callback=lambda _: print(
                "If needed, open http://localhost:8400?welcome=true on the kernel machine.",
            ),
        )
        if not result.get("access_token"):
            raise RuntimeError(
                "Administrative sign-in failed; check consent, roles and redirect URI."
            )
        return GraphClient(result["access_token"])
    except Exception:
        raise RuntimeError(
            "Administrative sign-in failed or timed out (180s). Configure the dedicated "
            "public client's desktop redirect http://localhost:8400 and retry."
        ) from None


class GraphError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__(
            f"Graph HTTP {status}; check permissions, tenant eligibility and preview contract. "
            "Response withheld. Unsupported contracts are not replaced with another API."
        )


class GraphClient:
    def __init__(self, token):
        self.token = token
        self.session = requests.Session()

    def request(self, method, path, body=None, *, missing=False):
        if not path.startswith(("/v1.0/", "/beta/")) or path.startswith("//"):
            raise ValueError("Use an explicit relative Graph versioned path.")
        try:
            response = self.session.request(
                method,
                GRAPH + path,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                    "OData-Version": "4.0",
                },
                json=body,
                timeout=(5, 30),
                allow_redirects=False,
            )
        except requests.RequestException:
            raise RuntimeError(
                "Graph request interrupted. Preserve state and reconcile before retrying writes."
            ) from None
        if response.status_code == 404 and missing:
            return None
        if not 200 <= response.status_code < 300:
            raise GraphError(response.status_code)
        try:
            return response.json() if response.content else {}
        except ValueError:
            raise RuntimeError(
                "Graph response contract was not JSON; preserve retry state."
            ) from None

    def items(self, path):
        values = []
        seen = set()
        while path:
            if path in seen:
                raise RuntimeError("Graph returned repeated pagination.")
            seen.add(path)
            page = self.request("GET", path)
            values.extend(page["value"])
            link = page.get("@odata.nextLink")
            if link and not link.startswith(GRAPH + "/"):
                raise RuntimeError("Unexpected pagination host; no credentials forwarded.")
            path = link[len(GRAPH) :] if link else None
        return values

    def close(self):
        self.token = None
        self.session.close()


def one_or_none(items):
    if len(items) > 1:
        raise ValueError("Ambiguous directory matches. Supply explicit IDs; nothing overwritten.")
    return items[0] if items else None


def resolve_sponsor(graph, child_id, sponsor_id):
    sponsor_id = guid(sponsor_id, "ONBOARDING_SPONSOR_USER_ID")
    sponsors = graph.items(
        f"/v1.0/servicePrincipals/{child_id}/microsoft.graph.agentIdentity/sponsors?$select=id",
    )
    if sponsor_id not in {s["id"] for s in sponsors}:
        raise ValueError("Selected user is not an existing child sponsor; no identity inferred.")
    user = graph.request(
        "GET",
        f"/v1.0/users/{sponsor_id}?$select=id,displayName,userPrincipalName",
    )
    if (
        user.get("id") != sponsor_id
        or not user.get("displayName")
        or not user.get("userPrincipalName")
    ):
        raise ValueError(
            "Existing sponsor must resolve to a real user with basic profile metadata."
        )
    return {"id": sponsor_id, "displayName": user["displayName"], "upn": user["userPrincipalName"]}


def ensure_blueprint(graph, state, state_path, sponsor_id, *, blueprint_id=None, child_id=None):
    """SETUP-confirmed notebook section. Save intent before every non-idempotent create."""
    sponsor_id = guid(sponsor_id, "ONBOARDING_SPONSOR_USER_ID")
    sponsor = graph.request("GET", f"/v1.0/users/{sponsor_id}?$select=id")
    if sponsor.get("id") != sponsor_id:
        raise ValueError("Sponsor readback mismatch.")
    suffix = hashlib.sha256(state["deployment"]["webAppResourceId"].lower().encode()).hexdigest()[
        :10
    ]
    blueprint_name = f"AppServiceBlueprint-{suffix}"
    sponsor_ref = f"{GRAPH}/v1.0/users/{sponsor_id}"
    supplied = {"blueprintObjectId": blueprint_id, "childAgentId": child_id}
    for key, value in supplied.items():
        if value:
            value = guid(value, key)
            if state.get(key) and state[key] != value:
                raise ValueError("Explicit identity ID conflicts with saved state.")
            state[key] = value
    if state.get("childAgentId") and not state.get("blueprintObjectId"):
        raise ValueError("Explicit child reuse requires its blueprint object ID.")
    path = "/v1.0/applications/microsoft.graph.agentIdentityBlueprint"
    if state.get("blueprintObjectId"):
        blueprint = graph.request(
            "GET",
            f"/v1.0/applications/{state['blueprintObjectId']}/microsoft.graph.agentIdentityBlueprint",
        )
    else:
        query = urlencode({"$filter": f"displayName eq '{blueprint_name}'"})
        blueprint = one_or_none(graph.items(f"{path}?{query}"))
        if blueprint is None:
            if state.get("blueprintCreatePending"):
                raise RuntimeError(
                    "Blueprint create is unresolved. Reconcile in Entra; do not recreate."
                )
            state["blueprintCreatePending"] = True
            save_private(state_path, state)
            blueprint = graph.request(
                "POST",
                path,
                {
                    "displayName": blueprint_name,
                    "sponsors@odata.bind": [sponsor_ref],
                    "owners@odata.bind": [sponsor_ref],
                    "managerApplications": [state["provisioningClientId"]],
                },
            )
            state["created"]["blueprint"] = True
    blueprint_client = guid(blueprint["appId"], "blueprint client")
    if state.get("blueprintClientId") and state["blueprintClientId"] != blueprint_client:
        raise ValueError("Blueprint client ID conflicts with saved state; no state rebound.")
    state["blueprintObjectId"] = guid(blueprint["id"], "blueprint object")
    state["blueprintClientId"] = blueprint_client
    state["blueprintCreatePending"] = False
    save_private(state_path, state)
    blueprint_client = state["blueprintClientId"]
    if state["provisioningClientId"] not in blueprint.get("managerApplications", []):
        raise ValueError(
            "Blueprint managerApplications must include this provisioning client. "
            "Have its owner explicitly configure manageability; no unrelated blueprint is patched."
        )
    principal_path = (
        f"/v1.0/servicePrincipals(appId='{blueprint_client}')/"
        "microsoft.graph.agentIdentityBlueprintPrincipal"
    )
    principal = graph.request("GET", principal_path, missing=True)
    if principal is None:
        if state.get("blueprintPrincipalId"):
            raise ValueError("Saved blueprint principal is missing; reconcile before recreating.")
        principal = graph.request(
            "POST",
            "/v1.0/servicePrincipals/microsoft.graph.agentIdentityBlueprintPrincipal",
            {"appId": blueprint_client},
        )
        state["created"]["blueprintPrincipal"] = True
    if principal.get("appId") != blueprint_client:
        raise ValueError("Blueprint principal relationship mismatch.")
    if state.get("blueprintPrincipalId") and state["blueprintPrincipalId"] != principal["id"]:
        raise ValueError("Blueprint principal ID conflicts with saved state.")
    state["blueprintPrincipalId"] = guid(principal["id"], "blueprint principal")
    save_private(state_path, state)
    return state


def ensure_child(graph, state, state_path, sponsor_id):
    blueprint_client = state["blueprintClientId"]
    suffix = hashlib.sha256(state["deployment"]["webAppResourceId"].lower().encode()).hexdigest()[
        :10
    ]
    child_name = f"AppServiceLearningAgent-{suffix}"
    sponsor_id = guid(sponsor_id, "ONBOARDING_SPONSOR_USER_ID")
    sponsor_ref = f"{GRAPH}/v1.0/users/{sponsor_id}"
    child_path = "/v1.0/servicePrincipals/microsoft.graph.agentIdentity"
    if state.get("childAgentId"):
        child = graph.request(
            "GET",
            f"/v1.0/servicePrincipals/{state['childAgentId']}/microsoft.graph.agentIdentity",
        )
    else:
        query = urlencode(
            {
                "$filter": f"agentIdentityBlueprintId eq '{blueprint_client}'",
            }
        )
        child = one_or_none(
            [c for c in graph.items(f"{child_path}?{query}") if c.get("displayName") == child_name]
        )
        if child is None:
            if state.get("childCreatePending"):
                raise RuntimeError(
                    "Child create is unresolved. Reconcile in Entra; do not recreate."
                )
            state["childCreatePending"] = True
            save_private(state_path, state)
            child = graph.request(
                "POST",
                child_path,
                {
                    "displayName": child_name,
                    "agentIdentityBlueprintId": blueprint_client,
                    "sponsors@odata.bind": [sponsor_ref],
                },
            )
            state["created"]["child"] = True
    if child.get("agentIdentityBlueprintId") != blueprint_client:
        raise ValueError("Child identity belongs to another blueprint.")
    state["childAgentId"] = guid(child["id"], "child identity")
    # Agent identities are service-principal-only. The ID returned by this typed
    # contract is the child GUID used by FMI/Agent365; refuse divergent appId contracts.
    if child.get("appId") and child["appId"] != state["childAgentId"]:
        raise ValueError("Unsupported child ID/appId contract; do not misattribute telemetry.")
    state["childCreatePending"] = False
    state["agentName"] = child["displayName"]
    state["sponsor"] = resolve_sponsor(graph, state["childAgentId"], sponsor_id)
    save_private(state_path, state)
    return state


def ensure_federation(graph, state, state_path):
    path = (
        f"/beta/applications/{state['blueprintObjectId']}/"
        "microsoft.graph.agentIdentityBlueprint/federatedIdentityCredentials"
    )
    expected = {
        "issuer": f"https://login.microsoftonline.com/{state['deployment']['tenantId']}/v2.0",
        "subject": state["deployment"]["managedIdentityPrincipalId"],
        "audiences": ["api://AzureADTokenExchange"],
    }
    credentials = graph.items(path)
    matches = [
        c
        for c in credentials
        if all(c.get(key) == value for key, value in expected.items())
        and not c.get("claimsMatchingExpression")
    ]
    match = one_or_none(matches)
    name = "appservice-telemetry-" + expected["subject"][:8]
    if match is None:
        if any(c.get("name") == name for c in credentials):
            raise ValueError("FIC name exists with different trust; nothing overwritten.")
        match = graph.request("POST", path, {"name": name, **expected})
        state["created"]["federation"] = True
    verified = graph.request("GET", path + "/" + quote(match["id"], safe=""))
    if any(verified.get(k) != v for k, v in expected.items()):
        raise ValueError("FIC readback mismatch.")
    state["federatedCredentialId"] = verified["id"]
    save_private(state_path, state)
    return verified["id"]


def verify_registration(record, state):
    for key, value in {
        "sourceAgentId": state["childAgentId"],
        "agentIdentityId": state["childAgentId"],
        "agentIdentityBlueprintId": state["blueprintClientId"],
        "managedByAppId": state["provisioningClientId"],
    }.items():
        if str(record.get(key, "")).lower() != value.lower():
            raise ValueError("Agent365 registration readback mismatch; no success claimed.")
    return record


def registration_probe(graph, state):
    identifier = state.get("registrationId", state["registrationRequestId"])
    record = graph.request("GET", REGISTRATIONS + "/" + quote(identifier, safe=""), missing=True)
    if record is None and not identifier.startswith("T_"):
        record = graph.request(
            "GET",
            REGISTRATIONS + "/T_" + quote(identifier, safe=""),
            missing=True,
        )
    return verify_registration(record, state) if record is not None else None


def ensure_registration(graph, state, state_path):
    existing = registration_probe(graph, state)
    if existing is None:
        if state.get("registrationPending"):
            raise RuntimeError(
                "Previous registration unresolved. Keep request ID/state; reconcile in Agent365 "
                "before retrying. No new registration is created.",
            )
        state["registrationPending"] = True
        save_private(state_path, state)
        now = datetime.now(UTC).isoformat()
        try:
            result = graph.request(
                "POST",
                REGISTRATIONS,
                {
                    "id": state["registrationRequestId"],
                    "displayName": state["agentName"],
                    "ownerIds": [state["sponsor"]["id"]],
                    "createdBy": state["sponsor"]["id"],
                    "managedByAppId": state["provisioningClientId"],
                    "sourceAgentId": state["childAgentId"],
                    "agentIdentityId": state["childAgentId"],
                    "agentIdentityBlueprintId": state["blueprintClientId"],
                    "sourceCreatedDateTime": now,
                    "sourceLastModifiedDateTime": now,
                },
            )
            if result.get("id"):
                state["registrationId"] = result["id"]
                save_private(state_path, state)
                state["created"]["registration"] = True
        except (RuntimeError, ValueError):
            # A timeout/conflict can follow a committed write. Probe the SAME request.
            pass
        existing = registration_probe(graph, state)
        if existing is None:
            raise RuntimeError(
                "Registration write is unresolved. Preserve state; no success claimed."
            )
    state["registrationId"] = existing["id"]
    state["registrationPending"] = False
    save_private(state_path, state)
    return existing


def external_record(project, name, child_id):
    record = project.agents.get(agent_name=name)
    definition = record.versions.latest.definition
    if not isinstance(definition, ExternalAgentDefinition) or definition.otel_agent_id != child_id:
        raise ValueError("Existing Foundry agent kind/telemetry ID differs; nothing overwritten.")
    return record


def ensure_external(project, name, child_id, state, state_path, endpoint):
    endpoint = project_endpoint(endpoint)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,63}", name):
        raise ValueError("External name must contain 1-63 letters, digits, underscores or hyphens.")
    previous = state.get("foundry")
    if previous and (
        previous["projectEndpoint"] != endpoint
        or previous["externalAgentName"] != name
        or previous["otelAgentId"] != child_id
    ):
        raise ValueError("Foundry target conflicts with saved onboarding state.")
    target = previous or {
        "projectEndpoint": endpoint,
        "externalAgentName": name,
        "otelAgentId": child_id,
    }
    state["foundry"] = target
    try:
        record = external_record(project, name, child_id)
    except ResourceNotFoundError:
        if target.get("createPending"):
            raise RuntimeError(
                "External create unresolved; reconcile before another version is made."
            ) from None
        target["createPending"] = True
        save_private(state_path, state)
        try:
            project.agents.create_version(
                agent_name=name,
                definition=ExternalAgentDefinition(otel_agent_id=child_id),
                description="App Service learning agent; runtime remains outside Foundry.",
            )
            state["created"]["external"] = True
        except (AzureError, requests.RequestException):
            pass
        record = external_record(project, name, child_id)
    target.update(
        {
            "registrationId": record.id,
            "version": record.versions.latest.version,
            "createPending": False,
        }
    )
    save_private(state_path, state)
    return target


def az_json(*arguments):
    executable = shutil.which("az")
    if not executable:
        raise RuntimeError("Azure CLI is required; run az login for the target tenant.")
    result = subprocess.run(
        [executable, *arguments, "--only-show-errors", "-o", "json"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode:
        raise RuntimeError("Azure CLI read failed; check sign-in and resource read access.")
    return json.loads(result.stdout)


def verify_azure_deployment(deployment):
    account = az_json("account", "show")
    if (
        account["tenantId"] != deployment["tenantId"]
        or account["id"] != deployment["subscriptionId"]
    ):
        raise ValueError("Azure CLI tenant/subscription does not match the deployment.")
    web = az_json("webapp", "show", "--ids", deployment["webAppResourceId"])
    identity = web["identity"]
    managed = az_json("identity", "show", "--ids", deployment["managedIdentityResourceId"])
    if (
        identity["principalId"] != deployment["systemAssignedPrincipalId"]
        or identity["tenantId"] != deployment["tenantId"]
        or managed["clientId"] != deployment["managedIdentityClientId"]
        or managed["principalId"] != deployment["managedIdentityPrincipalId"]
        or deployment["managedIdentityResourceId"].lower()
        not in {key.lower() for key in identity.get("userAssignedIdentities", {})}
    ):
        raise ValueError(
            "Live App Service/managed identity bindings do not match deployment state."
        )


def verify_insights(project, resource_id, connection_string):
    if not re.fullmatch(
        r"/subscriptions/[0-9a-fA-F-]{36}/resourceGroups/[^/]+/"
        r"providers/Microsoft.Insights/components/[^/]+",
        resource_id,
        re.IGNORECASE,
    ):
        raise ValueError("Set APPLICATIONINSIGHTS_RESOURCE_ID to the linked resource's ARM ID.")
    # This API returns the connection from THIS project, not from an arbitrary resource.
    linked = project.telemetry.get_application_insights_connection_string()
    resource = az_json(
        "resource",
        "show",
        "--ids",
        resource_id,
        "--api-version",
        "2020-02-02",
    )

    def instrumentation_key(value):
        pairs = dict(part.split("=", 1) for part in value.split(";") if "=" in part)
        return guid(pairs.get("InstrumentationKey"), "Application Insights instrumentation key")

    key = instrumentation_key(connection_string)

    def connection_parts(value):
        return {
            key.strip().lower(): part.strip().rstrip("/")
            for entry in value.split(";")
            if "=" in entry
            for key, part in [entry.split("=", 1)]
        }

    if (
        instrumentation_key(linked) != key
        or connection_parts(linked) != connection_parts(connection_string)
        or guid(resource["properties"]["InstrumentationKey"], "resource instrumentation key") != key
    ):
        raise ValueError("Connection string/resource does not match the selected Foundry project.")
    return True


def prepare_runtime_settings(state, path, *, agent365_only=False, connection_string=""):
    d = state["deployment"]
    if not state.get("registrationId") or not state.get("federatedCredentialId"):
        raise ValueError("Complete Agent365 readback and federation before preparing settings.")
    foundry = None if agent365_only else state.get("foundry")
    if not agent365_only and (
        not foundry or not foundry.get("registrationId") or not foundry.get("connectionVerified")
    ):
        raise ValueError(
            "Dual export requires verified external registration AND linked Application Insights."
        )
    sponsor = state["sponsor"]
    settings = {
        "ENABLE_AGENT365_TELEMETRY": "true",
        "ENTRA_TENANT_ID": d["tenantId"],
        "ENTRA_BLUEPRINT_CLIENT_ID": state["blueprintClientId"],
        "ENTRA_AGENT_IDENTITY_OBJECT_ID": state["childAgentId"],
        "AZURE_MANAGED_IDENTITY_CLIENT_ID": d["managedIdentityClientId"],
        "AGENT_IDENTITY_NAME": state["agentName"],
        "A365_SPONSOR_USER_ID": sponsor["id"],
        "A365_SPONSOR_DISPLAY_NAME": sponsor["displayName"],
        "A365_SPONSOR_UPN": sponsor["upn"],
        "A365_CAPTURE_CONTENT": "false",
        "A365_EXPORT_TO_FOUNDRY": str(not agent365_only).lower(),
        "A365_AZURE_MONITOR_USE_ENTRA": "true",
    }
    if not agent365_only:
        settings["APPLICATIONINSIGHTS_CONNECTION_STRING"] = connection_string
    TelemetrySettings.from_env(settings)
    artifact = {
        "schemaVersion": 1,
        "deployment": d,
        **{
            key: state[key]
            for key in (
                "blueprintObjectId",
                "blueprintClientId",
                "blueprintPrincipalId",
                "childAgentId",
                "registrationId",
                "provisioningClientId",
            )
        },
        "foundry": foundry,
        "settings": settings,
    }
    save_private(path, artifact)
    return artifact


def validate_runtime(artifact, graph, project=None):
    if artifact.get("schemaVersion") != 1:
        raise ValueError("Unsupported settings artifact schema.")
    d = validate_deployment(artifact["deployment"])
    s = TelemetrySettings.from_env(artifact["settings"])
    if (
        s.tenant_id != d["tenantId"]
        or s.child_id != artifact["childAgentId"]
        or s.blueprint_id != artifact["blueprintClientId"]
        or s.managed_identity_id != d["managedIdentityClientId"]
        or artifact["settings"].get("ENABLE_AGENT365_TELEMETRY") != "true"
    ):
        raise ValueError("Runtime settings do not match onboarding identities.")
    verify_azure_deployment(d)
    blueprint = graph.request(
        "GET",
        f"/v1.0/applications/{artifact['blueprintObjectId']}/microsoft.graph.agentIdentityBlueprint",
    )
    principal = graph.request(
        "GET",
        f"/v1.0/servicePrincipals/{artifact['blueprintPrincipalId']}/"
        "microsoft.graph.agentIdentityBlueprintPrincipal",
    )
    child = graph.request(
        "GET",
        f"/v1.0/servicePrincipals/{s.child_id}/microsoft.graph.agentIdentity",
    )
    if (
        blueprint.get("appId") != s.blueprint_id
        or principal.get("appId") != s.blueprint_id
        or child.get("agentIdentityBlueprintId") != s.blueprint_id
        or child.get("appId") not in (None, s.child_id)
    ):
        raise ValueError("Live blueprint/principal/child relationships do not match settings.")
    profile = resolve_sponsor(graph, s.child_id, s.sponsor_id)
    if profile["displayName"] != s.sponsor_name or profile["upn"] != s.sponsor_upn:
        raise ValueError("Sponsor metadata changed. Rerun onboarding; no profile is printed.")
    credentials = graph.items(
        f"/beta/applications/{artifact['blueprintObjectId']}/"
        "microsoft.graph.agentIdentityBlueprint/federatedIdentityCredentials",
    )
    if not any(
        c.get("issuer") == f"https://login.microsoftonline.com/{s.tenant_id}/v2.0"
        and c.get("subject") == d["managedIdentityPrincipalId"]
        and c.get("audiences") == ["api://AzureADTokenExchange"]
        and not c.get("claimsMatchingExpression")
        for c in credentials
    ):
        raise ValueError("Exact blueprint federated trust is missing.")
    record = graph.request(
        "GET",
        REGISTRATIONS + "/" + quote(artifact["registrationId"], safe=""),
    )
    verify_registration(record, artifact)
    if s.export_to_foundry:
        f = artifact.get("foundry")
        if not f or f.get("otelAgentId") != s.child_id or not project:
            raise ValueError("Dual export requires a matching Foundry External Agent.")
        record = external_record(project, f["externalAgentName"], s.child_id)
        if record.id != f["registrationId"] or record.versions.latest.version != f["version"]:
            raise ValueError("External registration/version changed; rerun onboarding.")
        verify_insights(project, f["applicationInsightsResourceId"], s.connection_string)
    elif artifact.get("foundry") is not None:
        raise ValueError("Agent365-only artifact must explicitly omit Foundry configuration.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["validate-runtime"])
    parser.add_argument("--settings-file", required=True)
    args = parser.parse_args()
    artifact = json.loads(Path(args.settings_file).read_text(encoding="utf-8-sig"))
    graph = sign_in(
        artifact["deployment"]["tenantId"],
        artifact["provisioningClientId"],
        read_only=True,
    )
    try:
        if artifact["settings"].get("A365_EXPORT_TO_FOUNDRY") == "true":
            from azure.ai.projects import AIProjectClient
            from azure.identity import AzureCliCredential

            with AzureCliCredential(tenant_id=artifact["deployment"]["tenantId"]) as credential:
                with AIProjectClient(
                    endpoint=project_endpoint(artifact["foundry"]["projectEndpoint"]),
                    credential=credential,
                    allow_preview=True,
                ) as project:
                    validate_runtime(artifact, graph, project)
        else:
            validate_runtime(artifact, graph)
        print(
            "Setup readback verified. Runtime token, export and portal visibility "
            "remain unverified."
        )
    finally:
        graph.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit(
            "Validation failed. Check identity bindings, admin consent, resource read access, "
            "linked Application Insights and preview availability. No settings applied."
        ) from None
