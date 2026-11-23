import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from azure.ai.projects.models import ExternalAgentDefinition, PromptAgentDefinition
from azure.core.exceptions import ResourceNotFoundError, ServiceRequestError

from src import onboarding as setup
from tests.conftest import BLUEPRINT, CHILD, MANAGED, PROJECT, SPONSOR, TENANT

CLIENT = "66666666-6666-4666-8666-666666666666"
OBJECT = "77777777-7777-4777-8777-777777777777"
PRINCIPAL = "88888888-8888-4888-8888-888888888888"


@pytest.fixture
def state(deployment):
    return {
        "schemaVersion": 1,
        "deployment": deployment,
        "provisioningClientId": CLIENT,
        "blueprintObjectId": OBJECT,
        "blueprintClientId": BLUEPRINT,
        "blueprintPrincipalId": PRINCIPAL,
        "childAgentId": CHILD,
        "agentName": "TestAgent",
        "registrationRequestId": MANAGED,
        "created": {},
        "sponsor": {"id": SPONSOR, "displayName": "Test Sponsor", "upn": "sponsor@example.invalid"},
        "federatedCredentialId": "fic-id",
    }


def registration(state):
    return {
        "id": f"T_{MANAGED}",
        "sourceAgentId": CHILD,
        "agentIdentityId": CHILD,
        "agentIdentityBlueprintId": BLUEPRINT,
        "managedByAppId": CLIENT,
    }


def external(definition=None):
    return SimpleNamespace(
        id="external-id",
        name="External",
        versions=SimpleNamespace(
            latest=SimpleNamespace(
                version="1",
                definition=definition or ExternalAgentDefinition(otel_agent_id=CHILD),
            )
        ),
    )


def test_state_binds_deployment_client_and_reuses_request_id(tmp_path, deployment):
    path = tmp_path / ".local" / "state.json"
    first = setup.load_state(path, deployment, CLIENT)
    setup.save_private(path, first)
    assert setup.load_state(path, deployment, CLIENT) == first
    with pytest.raises(ValueError, match="another deployment"):
        setup.load_state(path, deployment, TENANT)
    with pytest.raises(ValueError, match="another deployment"):
        setup.load_state(path, {**deployment, "modelDeployment": "other-model"}, CLIENT)


def test_existing_identities_are_verified_without_writes(tmp_path, state):
    graph = Mock()

    def request(method, path, body=None, **_):
        assert method == "GET"
        if "/users/" in path:
            return {
                "id": SPONSOR,
                "displayName": "Test Sponsor",
                "userPrincipalName": "sponsor@example.invalid",
            }
        if "/applications/" in path:
            return {"id": OBJECT, "appId": BLUEPRINT, "managerApplications": [CLIENT]}
        if "agentIdentityBlueprintPrincipal" in path:
            return {"id": PRINCIPAL, "appId": BLUEPRINT}
        return {"id": CHILD, "displayName": "TestAgent", "agentIdentityBlueprintId": BLUEPRINT}

    graph.request.side_effect = request
    graph.items.return_value = [{"id": SPONSOR}]
    path = tmp_path / ".local" / "state.json"
    setup.ensure_blueprint(graph, state, path, SPONSOR)
    setup.ensure_child(graph, state, path, SPONSOR)
    assert state["childAgentId"] == CHILD
    assert not state["created"]


def test_child_relationship_mismatch_refused(tmp_path, state):
    graph = Mock()
    graph.request.return_value = {"id": CHILD, "agentIdentityBlueprintId": TENANT}
    with pytest.raises(ValueError, match="another blueprint"):
        setup.ensure_child(graph, state, tmp_path / ".local" / "state.json", SPONSOR)


def test_blueprint_state_cannot_silently_rebind(tmp_path, state):
    graph = Mock()
    graph.request.side_effect = [
        {"id": SPONSOR},
        {"id": OBJECT, "appId": TENANT, "managerApplications": [CLIENT]},
    ]
    with pytest.raises(ValueError, match="conflicts with saved state"):
        setup.ensure_blueprint(graph, state, tmp_path / ".local" / "state.json", SPONSOR)
    assert state["blueprintClientId"] == BLUEPRINT


def test_missing_saved_principal_is_not_silently_recreated(tmp_path, state):
    graph = Mock()
    graph.request.side_effect = [
        {"id": SPONSOR},
        {"id": OBJECT, "appId": BLUEPRINT, "managerApplications": [CLIENT]},
        None,
    ]
    with pytest.raises(ValueError, match="reconcile before recreating"):
        setup.ensure_blueprint(graph, state, tmp_path / ".local" / "state.json", SPONSOR)
    assert all(call.args[0] == "GET" for call in graph.request.call_args_list)


def test_pending_blueprint_never_blindly_recreates(tmp_path, state):
    state.pop("blueprintObjectId")
    state.pop("childAgentId")
    state["blueprintCreatePending"] = True
    graph = Mock()
    graph.request.return_value = {"id": SPONSOR}
    graph.items.return_value = []
    with pytest.raises(RuntimeError, match="unresolved"):
        setup.ensure_blueprint(graph, state, tmp_path / ".local" / "state.json", SPONSOR)
    assert all(c.args[0] == "GET" for c in graph.request.call_args_list)


def test_federation_exact_reuse_and_name_collision(tmp_path, state):
    graph = Mock()
    expected = {
        "id": "fic-id",
        "issuer": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "subject": BLUEPRINT,
        "audiences": ["api://AzureADTokenExchange"],
        "name": f"appservice-telemetry-{BLUEPRINT[:8]}",
    }
    graph.items.return_value = [expected]
    graph.request.return_value = expected
    assert setup.ensure_federation(graph, state, tmp_path / ".local" / "state.json") == "fic-id"
    assert graph.request.call_args.args[0] == "GET"
    graph.items.return_value = [{**expected, "subject": "wrong"}]
    with pytest.raises(ValueError, match="different trust"):
        setup.ensure_federation(graph, state, tmp_path / ".local" / "state.json")


def test_registration_exact_reuse(tmp_path, state):
    graph = Mock()
    graph.request.return_value = registration(state)
    result = setup.ensure_registration(graph, state, tmp_path / ".local" / "state.json")
    assert result["id"] == state["registrationId"]
    assert all(c.args[0] == "GET" for c in graph.request.call_args_list)


def test_registration_mismatch_stops_without_post(tmp_path, state):
    graph = Mock()
    graph.request.return_value = {**registration(state), "agentIdentityId": TENANT}
    with pytest.raises(ValueError, match="mismatch"):
        setup.ensure_registration(graph, state, tmp_path / ".local" / "state.json")
    assert all(c.args[0] == "GET" for c in graph.request.call_args_list)


def test_registration_ambiguous_post_reconciles_same_request(tmp_path, state):
    graph = Mock()
    graph.request.side_effect = [None, None, setup.GraphError(503), None, registration(state)]
    path = tmp_path / ".local" / "state.json"
    result = setup.ensure_registration(graph, state, path)
    assert result["id"] == f"T_{MANAGED}"
    calls = graph.request.call_args_list
    assert len([c for c in calls if c.args[0] == "POST"]) == 1
    assert calls[2].args[2]["id"] == MANAGED
    assert json.loads(path.read_text())["registrationPending"] is False


@pytest.mark.parametrize("status", [401, 403, 500])
def test_registration_errors_are_not_absence(tmp_path, state, status):
    graph = Mock()
    graph.request.side_effect = setup.GraphError(status)
    with pytest.raises(setup.GraphError):
        setup.ensure_registration(graph, state, tmp_path / ".local" / "state.json")
    graph.request.assert_called_once()


def test_unresolved_registration_never_blindly_posts(tmp_path, state):
    state["registrationPending"] = True
    graph = Mock()
    graph.request.return_value = None
    with pytest.raises(RuntimeError, match="Previous registration unresolved"):
        setup.ensure_registration(graph, state, tmp_path / ".local" / "state.json")
    assert all(c.args[0] == "GET" for c in graph.request.call_args_list)


def test_external_creates_only_after_404_then_reuses(tmp_path, state):
    project = Mock()
    record = external()
    project.agents.get.side_effect = [ResourceNotFoundError(), record, record]
    path = tmp_path / ".local" / "state.json"
    setup.ensure_external(project, "External", CHILD, state, path, PROJECT)
    setup.ensure_external(project, "External", CHILD, state, path, PROJECT)
    project.agents.create_version.assert_called_once()
    assert project.agents.create_version.call_args.kwargs["definition"].otel_agent_id == CHILD
    assert state["foundry"]["otelAgentId"] == CHILD
    assert state["foundry"]["version"] == "1"


@pytest.mark.parametrize(
    "definition",
    [
        ExternalAgentDefinition(otel_agent_id=TENANT),
        PromptAgentDefinition(model="test-model"),
    ],
)
def test_external_kind_or_id_mismatch_never_overwrites(tmp_path, state, definition):
    project = Mock()
    project.agents.get.return_value = external(definition)
    with pytest.raises(ValueError, match="differs"):
        setup.ensure_external(
            project,
            "External",
            CHILD,
            state,
            tmp_path / ".local" / "state.json",
            PROJECT,
        )
    project.agents.create_version.assert_not_called()


def test_external_network_error_read_is_not_absence(tmp_path, state):
    project = Mock()
    project.agents.get.side_effect = ServiceRequestError("unavailable")
    with pytest.raises(ServiceRequestError):
        setup.ensure_external(
            project,
            "External",
            CHILD,
            state,
            tmp_path / ".local" / "state.json",
            PROJECT,
        )
    project.agents.create_version.assert_not_called()


def test_external_ambiguous_create_reconciles_and_does_not_duplicate(tmp_path, state):
    project = Mock()
    project.agents.get.side_effect = [ResourceNotFoundError(), external(), external()]
    project.agents.create_version.side_effect = ServiceRequestError("ambiguous")
    path = tmp_path / ".local" / "state.json"
    setup.ensure_external(project, "External", CHILD, state, path, PROJECT)
    setup.ensure_external(project, "External", CHILD, state, path, PROJECT)
    project.agents.create_version.assert_called_once()


def test_external_unresolved_create_stops_future_versions(tmp_path, state):
    project = Mock()
    project.agents.get.side_effect = ResourceNotFoundError()
    project.agents.create_version.side_effect = ServiceRequestError("ambiguous")
    path = tmp_path / ".local" / "state.json"
    with pytest.raises(ResourceNotFoundError):
        setup.ensure_external(project, "External", CHILD, state, path, PROJECT)
    with pytest.raises(RuntimeError, match="unresolved"):
        setup.ensure_external(project, "External", CHILD, state, path, PROJECT)
    project.agents.create_version.assert_called_once()


def test_dual_artifact_requires_verified_link_and_only_mode_does_not(tmp_path, state):
    state["registrationId"] = f"T_{MANAGED}"
    path = tmp_path / ".local" / "runtime-settings.json"
    with pytest.raises(ValueError, match="Dual export requires"):
        setup.prepare_runtime_settings(state, path)
    artifact = setup.prepare_runtime_settings(state, path, agent365_only=True)
    assert artifact["foundry"] is None
    assert artifact["settings"]["A365_EXPORT_TO_FOUNDRY"] == "false"
    assert "APPLICATIONINSIGHTS_CONNECTION_STRING" not in artifact["settings"]


def test_link_verification_checks_project_and_actual_resource(monkeypatch):
    connection = f"InstrumentationKey={TENANT};"
    project = Mock()
    project.telemetry.get_application_insights_connection_string.return_value = connection
    resource_id = (
        f"/subscriptions/{TENANT}/resourceGroups/sample/"
        "providers/Microsoft.Insights/components/test"
    )
    monkeypatch.setattr(
        setup,
        "az_json",
        Mock(
            return_value={
                "properties": {"InstrumentationKey": TENANT},
            }
        ),
    )
    assert setup.verify_insights(project, resource_id, connection)
    with pytest.raises(ValueError, match="does not match"):
        setup.verify_insights(
            project,
            resource_id,
            connection + "IngestionEndpoint=https://wrong.example.invalid",
        )
    project.telemetry.get_application_insights_connection_string.return_value = (
        f"InstrumentationKey={BLUEPRINT};"
    )
    with pytest.raises(ValueError, match="does not match"):
        setup.verify_insights(project, resource_id, connection)
