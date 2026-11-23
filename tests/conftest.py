import pytest

TENANT = "11111111-1111-4111-8111-111111111111"
BLUEPRINT = "22222222-2222-4222-8222-222222222222"
CHILD = "33333333-3333-4333-8333-333333333333"
MANAGED = "44444444-4444-4444-8444-444444444444"
SPONSOR = "55555555-5555-4555-8555-555555555555"
PROJECT = "https://sample.services.ai.azure.com/api/projects/sample"


@pytest.fixture
def telemetry_env():
    return {
        "ENABLE_AGENT365_TELEMETRY": "true",
        "ENTRA_TENANT_ID": TENANT,
        "ENTRA_BLUEPRINT_CLIENT_ID": BLUEPRINT,
        "ENTRA_AGENT_IDENTITY_OBJECT_ID": CHILD,
        "AZURE_MANAGED_IDENTITY_CLIENT_ID": MANAGED,
        "AGENT_IDENTITY_NAME": "TestAgent",
        "A365_SPONSOR_USER_ID": SPONSOR,
        "A365_SPONSOR_DISPLAY_NAME": "Test Sponsor",
        "A365_SPONSOR_UPN": "sponsor@example.invalid",
        "APPLICATIONINSIGHTS_CONNECTION_STRING": f"InstrumentationKey={TENANT};",
    }


@pytest.fixture
def deployment():
    return {
        "tenantId": TENANT,
        "subscriptionId": TENANT,
        "resourceGroup": "sample-rg",
        "webAppName": "sample-web",
        "webAppResourceId": (
            f"/subscriptions/{TENANT}/resourceGroups/sample-rg/providers/Microsoft.Web/sites/sample-web"
        ),
        "hostname": "sample-web.azurewebsites.net",
        "systemAssignedPrincipalId": SPONSOR,
        "managedIdentityResourceId": (
            f"/subscriptions/{TENANT}/resourceGroups/sample-rg/"
            "providers/Microsoft.ManagedIdentity/userAssignedIdentities/sample-mi"
        ),
        "managedIdentityClientId": MANAGED,
        "managedIdentityPrincipalId": BLUEPRINT,
        "foundryProjectEndpoint": PROJECT,
        "modelDeployment": "test-model",
        "modelAccessScope": f"/subscriptions/{TENANT}/resourceGroups/sample-rg",
    }
