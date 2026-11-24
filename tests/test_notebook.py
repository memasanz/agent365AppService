import json
from pathlib import Path
from types import SimpleNamespace

import azure.identity
import nbformat
import pytest
import requests
from azure.core.exceptions import ClientAuthenticationError
from azure.identity._credentials import azure_cli

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("filename", ["00_talk_to_agent.ipynb", "01_register_with_agent365.ipynb"])
def test_notebook_valid_compilable_and_no_execution_outputs(filename):
    path = ROOT / "notebooks" / filename
    notebook = nbformat.read(path, as_version=4)
    nbformat.validate(notebook)
    for cell in notebook.cells:
        if cell.cell_type == "code":
            assert cell.execution_count is None
            assert cell.outputs == []
            compile(cell.source, f"{path.name}:{cell.id}", "exec")


@pytest.fixture
def talk_notebook(tmp_path, monkeypatch, request):
    mode = getattr(request, "param", "deployment")
    client = "11111111-1111-1111-1111-111111111111"
    tenant = "22222222-2222-2222-2222-222222222222"
    site_id = (
        "/subscriptions/33333333-3333-3333-3333-333333333333"
        "/resourceGroups/rg-test/providers/Microsoft.Web/sites/test-agent"
    )
    state = tmp_path / ".local"
    state.mkdir()
    if mode == "deployment":
        artifact = {
            "tenantId": tenant,
            "webAppResourceId": site_id,
            "hostname": "test-agent.azurewebsites.net",
            "httpAuthClientId": client,
        }
        name = "deployment.json"
    else:
        artifact = {"tenantId": tenant, "webAppResourceId": site_id, "clientId": client}
        name = "http-auth.json"
    (state / name).write_text(json.dumps(artifact), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    scopes, posts, lookups = [], [], []

    class Credential:
        def __init__(self, *, tenant_id):
            assert tenant_id == tenant

        def get_token(self, scope):
            scopes.append(scope)
            return SimpleNamespace(token="PRIVATE_TEST_TOKEN")

    def get(url, **kwargs):
        lookups.append(url)
        assert url == f"https://management.azure.com{site_id}?api-version=2025-03-01"
        assert kwargs["allow_redirects"] is False
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"properties": {"defaultHostName": "test-agent.azurewebsites.net"}},
        )

    def post(url, **kwargs):
        posts.append((url, kwargs))
        return SimpleNamespace(status_code=200, json=lambda: {"reply": "Hello from the agent."})

    monkeypatch.setattr(azure.identity, "AzureCliCredential", Credential)
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(requests, "post", post)
    notebook = nbformat.read(ROOT / "notebooks" / "00_talk_to_agent.ipynb", as_version=4)
    namespace = {}
    for cell in notebook.cells:
        if cell.id in ("configure", "talk-function"):
            exec(compile(cell.source, f"00_talk_to_agent:{cell.id}", "exec"), namespace)
    return namespace, scopes, posts, lookups


@pytest.mark.parametrize("talk_notebook", ["deployment", "http-auth"], indirect=True)
def test_talk_notebook_uses_saved_target_and_api_token(talk_notebook, capsys):
    namespace, scopes, posts, lookups = talk_notebook
    assert namespace["talk"]("Hello") == "Hello from the agent."
    assert scopes[-1] == "api://11111111-1111-1111-1111-111111111111/.default"
    assert bool(lookups) == ("https://management.azure.com/.default" in scopes)
    url, options = posts[0]
    assert url == "https://test-agent.azurewebsites.net/chat"
    assert options["json"] == {"message": "Hello"}
    assert options["headers"] == {"Authorization": "Bearer PRIVATE_TEST_TOKEN"}
    assert options["allow_redirects"] is False
    assert "PRIVATE_TEST_TOKEN" not in capsys.readouterr().out


def test_talk_notebook_credential_requests_the_api_resource(talk_notebook, monkeypatch):
    namespace, *_ = talk_notebook
    commands = []

    def run_command(arguments, _timeout):
        commands.append(arguments)
        return json.dumps({"accessToken": "PRIVATE_TEST_TOKEN", "expires_on": 4102444800})

    monkeypatch.setattr(azure_cli, "_run_command", run_command)
    namespace["credential"] = azure_cli.AzureCliCredential(tenant_id=namespace["tenant_id"])
    assert namespace["talk"]("Hello") == "Hello from the agent."
    assert commands[0][commands[0].index("--resource") + 1] == (
        "api://11111111-1111-1111-1111-111111111111"
    )


@pytest.mark.parametrize(("status", "message"), [(401, "az login"), (502, "Model access")])
def test_talk_notebook_explains_errors_without_showing_response(
    talk_notebook, monkeypatch, status, message
):
    namespace, *_ = talk_notebook

    def private_body():
        raise AssertionError("Error-response bodies must not be read")

    monkeypatch.setattr(
        requests,
        "post",
        lambda *_args, **_kwargs: SimpleNamespace(status_code=status, json=private_body),
    )
    with pytest.raises(RuntimeError, match=message):
        namespace["talk"]("Hello")


def test_talk_notebook_hides_authentication_error_details(talk_notebook, monkeypatch):
    namespace, *_ = talk_notebook

    def unavailable(_scope):
        raise ClientAuthenticationError("PRIVATE_AUTH_RESPONSE")

    monkeypatch.setattr(namespace["credential"], "get_token", unavailable)
    with pytest.raises(RuntimeError, match="az login") as error:
        namespace["talk"]("Hello")
    assert "PRIVATE_AUTH_RESPONSE" not in str(error.value)
