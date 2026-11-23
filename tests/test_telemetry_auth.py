import base64
import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.telemetry_auth import (
    FMI_SCOPE,
    OBSERVABILITY_RESOURCE,
    OBSERVABILITY_SCOPE,
    ObservabilityTokenResolver,
    TelemetryTokenError,
)
from tests.conftest import BLUEPRINT, CHILD, MANAGED, TENANT


def token_result(**changes):
    claims = {
        "tid": TENANT,
        "appid": CHILD,
        "aud": OBSERVABILITY_RESOURCE,
        "idtyp": "app",
        "exp": 4600,
        **changes,
    }
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return {
        "access_token": f"header.{encoded}.signature",
        "token_type": "Bearer",
        "expires_in": 3600,
    }


def resolver(session=None, credential=None):
    credential = credential or Mock(
        get_token=Mock(return_value=SimpleNamespace(token="mi-assertion"))
    )
    session = session or Mock()
    session.post.side_effect = [
        SimpleNamespace(
            status_code=200,
            json=lambda: {
                "access_token": "blueprint-assertion",
                "token_type": "bearer",
            },
        ),
        SimpleNamespace(status_code=200, json=token_result),
    ]
    return ObservabilityTokenResolver(
        TENANT,
        CHILD,
        BLUEPRINT,
        MANAGED,
        credential=credential,
        session=session,
        clock=lambda: 1000,
    )


def test_managed_identity_selection_and_fmi_exchange(monkeypatch):
    factory = Mock()
    monkeypatch.setattr("src.telemetry_auth.ManagedIdentityCredential", factory)
    r = resolver()
    r.credential = factory.return_value
    r.credential.get_token.return_value = SimpleNamespace(token="mi-assertion")
    assert r(CHILD, TENANT)
    r.credential.get_token.assert_called_once_with(FMI_SCOPE)
    forms = [c.kwargs["data"] for c in r.session.post.call_args_list]
    assert forms[0]["client_id"] == BLUEPRINT
    assert forms[0]["fmi_path"] == CHILD
    assert forms[0]["client_assertion"] == "mi-assertion"
    assert forms[1]["client_id"] == CHILD
    assert forms[1]["client_assertion"] == "blueprint-assertion"
    assert forms[1]["scope"] == OBSERVABILITY_SCOPE
    for call in r.session.post.call_args_list:
        assert call.kwargs["allow_redirects"] is False
        assert "client_secret" not in call.kwargs["data"]
    ObservabilityTokenResolver(TENANT, CHILD, BLUEPRINT, MANAGED).close()
    factory.assert_called_with(client_id=MANAGED)


def test_cache_renews_before_expiry():
    r = resolver()
    assert r(CHILD, TENANT) == r(CHILD, TENANT)
    assert r.session.post.call_count == 2
    r.clock = lambda: 4541
    r.session.post.side_effect = [
        SimpleNamespace(
            status_code=200,
            json=lambda: {
                "access_token": "new-parent",
                "token_type": "bearer",
            },
        ),
        SimpleNamespace(status_code=200, json=lambda: token_result(exp=9000)),
    ]
    assert r(CHILD, TENANT)
    assert r.session.post.call_count == 4


def test_concurrent_refresh_is_serialized():
    r = resolver()
    original = r._exchange

    def slow(*args):
        time.sleep(0.01)
        return original(*args)

    r._exchange = slow
    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(lambda _: r(CHILD, TENANT), range(24)))
    assert len(set(results)) == 1
    assert r.session.post.call_count == 2


def test_failure_never_serves_expired_token_and_recovers(caplog):
    r = resolver()
    r(CHILD, TENANT)
    r.clock = lambda: 5000
    r.credential.get_token.side_effect = RuntimeError("SECRET ASSERTION")
    with pytest.raises(TelemetryTokenError, match="acquisition failed"):
        r(CHILD, TENANT)
    assert r.token is None
    with pytest.raises(TelemetryTokenError, match="backed off"):
        r(CHILD, TENANT)
    r.clock = lambda: 5006
    r.credential.get_token.side_effect = None
    r.session.post.side_effect = [
        SimpleNamespace(
            status_code=200,
            json=lambda: {
                "access_token": "parent",
                "token_type": "bearer",
            },
        ),
        SimpleNamespace(status_code=200, json=lambda: token_result(exp=9000)),
    ]
    assert r(CHILD, TENANT)
    assert "SECRET ASSERTION" not in caplog.text


@pytest.mark.parametrize(
    "changes",
    [
        {"scp": "user.read"},
        {"idtyp": "user"},
        {"appid": BLUEPRINT},
        {"tid": BLUEPRINT},
        {"aud": "https://graph.microsoft.com"},
        {"exp": 1001},
        {"exp": float("inf")},
        {"roles": "invalid"},
        {"azp": BLUEPRINT},
        {"idtyp": None, "oid": None},
    ],
)
def test_rejects_wrong_or_unusable_token(changes):
    r = resolver()
    with pytest.raises(TelemetryTokenError):
        r._validate(token_result(**changes), 1000)


def test_cross_identity_refused_before_network():
    r = resolver()
    with pytest.raises(TelemetryTokenError):
        r(BLUEPRINT, TENANT)
    r.session.post.assert_not_called()
