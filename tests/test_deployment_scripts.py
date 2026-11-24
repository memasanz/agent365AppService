import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def powershell(script):
    executable = shutil.which("pwsh")
    if not executable:
        pytest.skip("PowerShell 7 is needed for deployment script checks")
    result = subprocess.run(
        [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


@pytest.mark.parametrize("custom_name", [False, True])
def test_provisioning_client_creation_reuse_and_consent_are_explicit(custom_name):
    from src.onboarding import SCOPES

    root = str(ROOT).replace("'", "''")
    name = "CustomAgentProvisioner" if custom_name else "Agent365IdentityProvisioner"
    name_argument = f"-Name '{name}'" if custom_name else ""
    permissions = [
        {"value": scope, "id": f"10000000-0000-0000-0000-{i:012d}", "isEnabled": True}
        for i, scope in enumerate(SCOPES, start=1)
    ]
    metadata = json.dumps(permissions)
    expected = json.dumps(list(SCOPES))
    powershell(f"""
        $ErrorActionPreference='Stop'
        $scriptPath='{root}\\scripts\\New-AgentIdentityProvisioningClient.ps1'
        $tenant='22222222-2222-2222-2222-222222222222'
        $customName=${str(custom_name).lower()}
        $global:Metadata='{metadata}' | ConvertFrom-Json -AsHashtable
        $expected='{expected}' | ConvertFrom-Json
        $global:App=$null; $global:ClientSp=$null; $global:Grant=$null
        $global:AppCreates=0; $global:SpCreates=0; $global:GrantCreates=0
        $global:Writes=0; $global:Connections=0; $global:CallerScopes=@()
        $global:DuplicateNames=$false; $global:HideRegistration=$false
        $global:WrongTenant=$false
        function Get-Module {{ param($ListAvailable) return [pscustomobject]@{{Name='mock'}} }}
        function Import-Module {{ param($Name) }}
        function Connect-MgGraph {{
            param($TenantId,$Scopes,$ContextScope,[switch]$NoWelcome)
            $global:Connections++
            $global:CallerScopes=$Scopes
        }}
        function Get-MgContext {{
            $id=if ($global:WrongTenant) {{
                '55555555-5555-5555-5555-555555555555'
            }} else {{ $tenant }}
            return [pscustomobject]@{{TenantId=$id;Scopes=$global:CallerScopes}}
        }}
        function Invoke-MgGraphRequest {{
            param($Method,$Uri,$Body,$OutputType,$ContentType,$ErrorAction)
            if (!$Uri.StartsWith('https://graph.microsoft.com/v1.0/')) {{ throw 'Unexpected API' }}
            if ($Method -eq 'GET') {{
                if ($Uri -match '/servicePrincipals\\?') {{
                    if ($Uri.Contains('00000003-0000-0000-c000-000000000000')) {{
                        $items=@($global:Metadata | Where-Object {{
                            !$global:HideRegistration -or
                                $_.value -ne 'AgentRegistration.ReadWrite.All'
                        }})
                        return @{{value=@(@{{id='graph-sp';oauth2PermissionScopes=$items}})}}
                    }}
                    $items=if ($global:ClientSp) {{ @($global:ClientSp) }} else {{ @() }}
                    return @{{value=$items}}
                }}
                if ($Uri -match '/applications\\?') {{
                    $items=if ($global:App) {{ @($global:App) }} else {{ @() }}
                    if ($global:DuplicateNames) {{ $items=@($global:App,$global:App) }}
                    return @{{value=$items}}
                }}
                if ($Uri -match '/applications/') {{ return $global:App }}
                if ($Uri -match '/servicePrincipals/') {{ return $global:ClientSp }}
                if ($Uri -match '/oauth2PermissionGrants\\?') {{
                    $items=if ($global:Grant) {{ @($global:Grant) }} else {{ @() }}
                    return @{{value=$items}}
                }}
                throw 'Unexpected read'
            }}
            $global:Writes++
            $payload=$Body | ConvertFrom-Json -AsHashtable
            if ($payload.ContainsKey('passwordCredentials') -or
                $payload.ContainsKey('keyCredentials')) {{
                throw 'Client credentials must not be created'
            }}
            if ($Uri -match '/applications') {{
                if ($Method -eq 'POST') {{
                    if ($payload.displayName -ne '{name}') {{
                        throw 'Name override/default failed'
                    }}
                    $global:AppCreates++
                    $global:App=@{{id='44444444-4444-4444-4444-444444444444'
                        appId='11111111-1111-1111-1111-111111111111'}}
                }}
                foreach ($key in $payload.Keys) {{ $global:App[$key]=$payload[$key] }}
                return $global:App
            }}
            if ($Uri -match '/servicePrincipals') {{
                $global:SpCreates++
                $global:ClientSp=@{{id='client-sp';appId=$payload.appId}}
                return $global:ClientSp
            }}
            if ($Uri -match '/oauth2PermissionGrants') {{
                if ($Method -eq 'POST') {{
                    $global:GrantCreates++
                    $global:Grant=@{{id='consent';consentType='AllPrincipals'}}
                }}
                foreach ($key in $payload.Keys) {{ $global:Grant[$key]=$payload[$key] }}
                return $global:Grant
            }}
            throw 'Unexpected write'
        }}
        try {{ & $scriptPath -TenantId $tenant {name_argument}; throw 'Missing approval accepted' }}
        catch {{ if ($_.Exception.Message -notlike 'Preview only*') {{ throw }} }}
        if ($global:Writes -or $global:Connections) {{ throw 'Unapproved setup touched Graph' }}
        & $scriptPath -TenantId $tenant {name_argument} -WhatIf
        if ($global:Writes) {{ throw 'Preview changed the directory' }}
        $first=& $scriptPath -TenantId $tenant {name_argument} -Approve
        if ($global:GrantCreates -or
            $global:CallerScopes -contains 'DelegatedPermissionGrant.ReadWrite.All') {{
            throw 'Consent was not separately authorized'
        }}
        $graphAccess=$global:App.requiredResourceAccess[0].resourceAccess
        if ($graphAccess.Count -ne $expected.Count -or
            @($graphAccess | Where-Object {{ $_.type -ne 'Scope' }}).Count) {{
            throw 'Wrong delegated permissions configured'
        }}
        foreach ($scope in $global:Metadata) {{
            if ($scope.id -notin @($graphAccess.id)) {{ throw 'Required notebook scope missing' }}
        }}
        $global:App.publicClient.redirectUris+=@('http://localhost:9600')
        $global:App.requiredResourceAccess[0].resourceAccess+=@(@{{id='extra-scope';type='Scope'}})
        $global:App.requiredResourceAccess+=@(@{{resourceAppId='unrelated-api'
            resourceAccess=@(@{{id='unrelated-role';type='Role'}})}})
        if ($customName) {{
            $global:Grant=@{{id='existing-consent';consentType='AllPrincipals';scope='Mail.Read'}}
        }}
        $second=& $scriptPath -TenantId $tenant {name_argument} `
            -ApplicationObjectId $first.applicationObjectId -Approve -GrantAdminConsent
        $third=& $scriptPath -TenantId $tenant {name_argument} -Approve -GrantAdminConsent
        if ($global:AppCreates -ne 1 -or $global:SpCreates -ne 1 -or
            $first.clientId -ne $second.clientId -or $first.clientId -ne $third.clientId) {{
            throw 'Registration was duplicated on retry'
        }}
        if ($global:GrantCreates -ne $(if ($customName) {{0}} else {{1}})) {{
            throw 'Consent grant was duplicated'
        }}
        $retainedGraph=@($global:App.requiredResourceAccess | Where-Object {{
            $_.resourceAppId -eq '00000003-0000-0000-c000-000000000000'
        }})[0]
        if ('http://localhost:8400' -notin $global:App.publicClient.redirectUris -or
            'http://localhost:9600' -notin $global:App.publicClient.redirectUris -or
            'extra-scope' -notin $retainedGraph.resourceAccess.id -or
            'unrelated-api' -notin $global:App.requiredResourceAccess.resourceAppId) {{
            throw 'Existing redirects or permissions were overwritten'
        }}
        foreach ($scope in $expected) {{
            if ($scope -notin @($global:Grant.scope -split ' ')) {{
                throw 'Consent is missing a scope'
            }}
        }}
        if ($customName -and 'Mail.Read' -notin @($global:Grant.scope -split ' ')) {{
            throw 'Existing consent scopes were removed'
        }}
        $before=$global:Writes
        $global:DuplicateNames=$true
        try {{
            & $scriptPath -TenantId $tenant {name_argument} -Approve
            throw 'Ambiguous name accepted'
        }}
        catch {{ if ($_.Exception.Message -notlike 'Multiple applications*') {{ throw }} }}
        $global:DuplicateNames=$false
        $global:HideRegistration=$true
        try {{
            & $scriptPath -TenantId $tenant {name_argument} -Approve
            throw 'Missing scope accepted'
        }}
        catch {{ if ($_.Exception.Message -notlike '*unavailable*') {{ throw }} }}
        $global:HideRegistration=$false
        $global:WrongTenant=$true
        try {{
            & $scriptPath -TenantId $tenant {name_argument} -Approve
            throw 'Wrong tenant accepted'
        }}
        catch {{ if ($_.Exception.Message -notlike 'Graph sign-in tenant*') {{ throw }} }}
        if ($global:Writes -ne $before) {{ throw 'A failed preflight changed directory objects' }}
        Write-Output 'Provisioning regressions passed'
    """)


def test_powershell_scripts_parse_and_parameter_example_is_valid():
    root = str(ROOT).replace("'", "''")
    powershell(f"""
        $ErrorActionPreference = 'Stop'
        Get-ChildItem '{root}\\scripts' -Filter '*.ps1' | ForEach-Object {{
            $tokens=$null; $errors=$null
            [System.Management.Automation.Language.Parser]::ParseFile(
                $_.FullName,[ref]$tokens,[ref]$errors) | Out-Null
            if ($errors.Count) {{ throw 'PowerShell syntax error' }}
        }}
        Get-Content '{root}\\infra\\main.parameters.example.json' -Raw | ConvertFrom-Json | Out-Null
    """)


def test_artifacts_resolve_relative_to_powershell_location(tmp_path):
    root = str(ROOT).replace("'", "''")
    temp = str(tmp_path).replace("'", "''")
    powershell(f"""
        . '{root}\\scripts\\common.ps1'
        Set-Location '{temp}'
        $expected=Join-Path '{temp}' '.local\\artifact.json'
        if ((Get-LocalPath '.local\\artifact.json') -ne $expected) {{
            throw 'Artifact resolved against process cwd rather than PowerShell location'
        }}
        Write-ProtectedJson @{{verified=$true}} '.local\\artifact.json'
        if (!(Read-JsonFile $expected).verified) {{ throw 'Relative artifact write failed' }}
    """)


@pytest.mark.parametrize("final_status", [3, 4])
def test_source_build_waits_for_exact_deployment_and_detects_failure(deployment, final_status):
    root = str(ROOT).replace("'", "''")
    powershell(f"""
        . '{root}\\scripts\\common.ps1'
        $deployment='{json.dumps(deployment)}' | ConvertFrom-Json -AsHashtable
        $script:Reads=0
        $sourceId='{deployment["managedIdentityClientId"]}'
        function Invoke-Az {{
            param($Arguments,$Operation)
            $script:Reads++
            $status=if ($script:Reads -eq 1) {{ 1 }} else {{ {final_status} }}
            return @(
                @{{id='older-unrelated-deployment';status=4}}
                @{{id='{deployment["managedIdentityClientId"]}';status=$status}}
            )
        }}
        $failed=$false
        try {{ Wait-SourceDeployment $deployment $sourceId -Attempts 2 -PollSeconds 0 }}
        catch {{
            if ($_.Exception.Message -notlike '*failed*') {{ throw }}
            $failed=$true
        }}
        if ($script:Reads -ne 2 -or $failed -ne ${str(final_status == 3).lower()}) {{
            throw 'Source build success/failure was not tied to the exact accepted deployment'
        }}
    """)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("InternalSubscriptionIsOverQuotaForSku", "App Service quota is insufficient"),
        ("AuthorizationFailed", "Check sign-in, permissions"),
    ],
)
def test_cli_failure_explains_quota_without_exposing_payloads(code, expected):
    root = str(ROOT).replace("'", "''")
    powershell(f"""
        . '{root}\\scripts\\common.ps1'
        function az {{
            $global:LASTEXITCODE=1
            '{{"code":"{code}","message":"PRIVATE RESPONSE BODY AND TOKEN"}}'
        }}
        $message=$null
        try {{
            Invoke-Az -Arguments @('deployment','group','create') -Operation 'Infrastructure'
        }} catch {{ $message=$_.Exception.Message }}
        if (!$message -or !$message.Contains('{expected}')) {{
            throw 'Missing safe failure explanation'
        }}
        if ($message.Contains('PRIVATE') -or $message.Contains('TOKEN')) {{
            throw 'CLI response payload leaked'
        }}
    """)


@pytest.mark.parametrize("matching_identity", [True, False])
def test_identity_attachment_ignores_case_but_rejects_another_identity(
    deployment, matching_identity
):
    root = str(ROOT).replace("'", "''")
    attached = deployment["managedIdentityResourceId"].lower()
    if not matching_identity:
        attached += "-unrelated"
    app = {
        "identity": {
            "principalId": deployment["systemAssignedPrincipalId"],
            "tenantId": deployment["tenantId"],
            "userAssignedIdentities": {attached: {}},
        },
        "properties": {"defaultHostName": deployment["hostname"]},
    }
    managed = {
        "properties": {
            "clientId": deployment["managedIdentityClientId"],
            "principalId": deployment["managedIdentityPrincipalId"],
            "tenantId": deployment["tenantId"],
        }
    }
    expected_rejection = str(not matching_identity).lower()
    powershell(f"""
        . '{root}\\scripts\\common.ps1'
        $script:Deployment='{json.dumps(deployment)}' | ConvertFrom-Json -AsHashtable
        $script:App='{json.dumps(app)}' | ConvertFrom-Json -AsHashtable
        $script:Managed='{json.dumps(managed)}' | ConvertFrom-Json -AsHashtable
        function Assert-AzureContext {{}}
        function Invoke-Az {{
            param($Arguments, $Operation)
            if ($Arguments[3] -eq $script:Deployment.webAppResourceId) {{ return $script:App }}
            if ($Arguments[3] -eq $script:Deployment.managedIdentityResourceId) {{
                return $script:Managed
            }}
            throw 'Unexpected mocked CLI operation'
        }}
        $rejected=$false
        try {{ $null=Assert-Deployment $script:Deployment }}
        catch {{
            if ($_.Exception.Message -notlike '*does not match*') {{ throw }}
            $rejected=$true
        }}
        if ($rejected -ne ${expected_rejection}) {{ throw 'Incorrect identity validation result' }}
    """)


def test_ip_role_and_settings_guards_without_cloud_calls(tmp_path):
    root = str(ROOT).replace("'", "''")
    temp = str(tmp_path).replace("'", "''")
    powershell(f"""
        . '{root}\\scripts\\common.ps1'
        $script:ProjectRoot = '{temp}'
        Assert-Cidrs @('203.0.113.5/32') 'caller'
        $rejected=$false
        try {{ Assert-Cidrs @('0.0.0.0/0') 'caller' }} catch {{ $rejected=$true }}
        if (!$rejected) {{ throw 'Unrestricted caller allowed' }}
        $script:FakeAssignments = @(@{{
            roleDefinitionId='/providers/Microsoft.Authorization/roleDefinitions/test-role'
            principalId='principal'; scope='/subscriptions/test'; condition=$null
        }})
        function Invoke-Az {{
            param($Arguments, [switch]$NoOutput, $Operation)
            if ($Arguments[0] -eq 'role') {{
                if ('--scope' -in $Arguments -and '--all' -in $Arguments) {{
                    throw 'Azure CLI rejects --all with an explicit scope'
                }}
                return $script:FakeAssignments
            }}
            if ($Arguments[0] -ne 'webapp' -or 'set' -notin $Arguments) {{
                throw 'Unexpected mocked CLI operation'
            }}
            $file = $Arguments[-1].Substring(1)
            $script:SettingsPath=$file
            $payload=Get-Content $file -Raw | ConvertFrom-Json -AsHashtable
            if ($payload.Count -ne 1 -or $payload['ENABLE_AGENT365_TELEMETRY'] -ne 'true') {{
                throw 'Additive update payload changed unrelated settings'
            }}
        }}
        $roleArgs=@('principal', '/subscriptions/test/resourceGroups/rg', 'test-role', 'test')
        if (!(Test-RoleAssignment @roleArgs)) {{
            throw 'Inherited role rejected'
        }}
        $script:FakeAssignments[0].scope='/subscriptions/unrelated'
        if (Test-RoleAssignment @roleArgs) {{
            throw 'Unrelated role accepted'
        }}
        $script:FakeAssignments[0].scope='/subscriptions/test'
        $script:FakeAssignments[0].condition='conditional'
        if (Test-RoleAssignment @roleArgs) {{
            throw 'Conditional role accepted as unconditional'
        }}
        Set-MergedAppSettings @{{resourceGroup='rg';webAppName='web';subscriptionId='test'}} @{{
            ENABLE_AGENT365_TELEMETRY='true'
        }}
        if (Test-Path -LiteralPath $script:SettingsPath) {{
            throw 'Temporary settings not cleaned up'
        }}
    """)


@pytest.mark.parametrize("valid_readback", [True, False])
def test_public_network_opens_only_after_tenant_authentication_readback(
    tmp_path, deployment, valid_readback
):
    root = str(ROOT).replace("'", "''")
    temp = str(tmp_path).replace("'", "''")
    deployment["httpAuthClientId"] = deployment["managedIdentityClientId"]
    valid = str(valid_readback).lower()
    powershell(f"""
        . '{root}\\scripts\\common.ps1'
        $script:ProjectRoot='{temp}'
        $deployment='{json.dumps(deployment)}' | ConvertFrom-Json -AsHashtable
        $script:Opened=$false
        function Invoke-Az {{
            param($Arguments, [switch]$NoOutput, $Operation)
            if ($Arguments[0] -eq 'rest' -and $Arguments[2] -eq 'put') {{
                $index=[array]::IndexOf($Arguments,'--body')+1
                $script:Payload=Get-Content $Arguments[$index].Substring(1) -Raw |
                    ConvertFrom-Json -AsHashtable
                return
            }}
            if ($Arguments[0] -eq 'rest' -and $Arguments[2] -eq 'get') {{
                if (!${valid}) {{
                    $script:Payload.properties.identityProviders.azureActiveDirectory.registration.
                        openIdIssuer='https://login.microsoftonline.com/other-tenant/v2.0'
                }}
                return $script:Payload
            }}
            if ($Arguments[0] -ne 'webapp' -or 'Allow' -notin $Arguments) {{
                throw 'Unexpected mocked CLI operation'
            }}
            $script:Opened=$true
        }}
        $failed=$false
        try {{ Enable-PublicEntraAccess $deployment }} catch {{ $failed=$true }}
        if ($script:Opened -ne ${valid} -or $failed -eq ${valid}) {{
            throw 'Public networking was not gated on authentication validation'
        }}
        $policy=$script:Payload.properties.identityProviders.azureActiveDirectory.validation.
            defaultAuthorizationPolicy
        if ($policy.ContainsKey('allowedPrincipals')) {{ throw 'Unexpected per-user restriction' }}
        if (Get-ChildItem '{temp}\\.local' -Filter '*.json') {{
            throw 'Temporary auth settings were not removed'
        }}
    """)


@pytest.mark.parametrize("authenticated", [True, False])
def test_verification_uses_api_token_only_in_entra_mode(deployment, authenticated):
    root = str(ROOT).replace("'", "''")
    if authenticated:
        deployment["httpAuthClientId"] = deployment["managedIdentityClientId"]
    enabled = str(authenticated).lower()
    scope = f"api://{deployment['managedIdentityClientId']}/user_impersonation"
    stdout = powershell(f"""
        . '{root}\\scripts\\common.ps1'
        $deployment='{json.dumps(deployment)}' | ConvertFrom-Json -AsHashtable
        function Invoke-Az {{
            param($Arguments, $Operation)
            if (!${enabled} -or $Arguments[0] -ne 'account' -or
                $Arguments -notcontains '{scope}' -or
                $Arguments -notcontains '{deployment["subscriptionId"]}' -or
                $Arguments -contains '--tenant') {{
                throw 'Unexpected token acquisition/scope'
            }}
            return @{{accessToken='PRIVATE_FAKE_TOKEN'}}
        }}
        function Invoke-WebRequest {{
            param($Uri,$Headers,$Method,$ContentType,$Body,$TimeoutSec,$MaximumRedirection,
                [switch]$SkipHttpErrorCheck)
            if ($Uri.EndsWith('/chat')) {{
                if ($Body -ne '{{}}' -or
                    $Headers['Authorization'] -eq 'Bearer PRIVATE_FAKE_TOKEN') {{
                    throw 'Anonymous rejection probe could invoke the model or send a real token'
                }}
                return [pscustomobject]@{{StatusCode=401}}
            }}
            if (${enabled} -and $Headers['Authorization'] -ne 'Bearer PRIVATE_FAKE_TOKEN') {{
                throw 'Missing authenticated health header'
            }}
            if (!${enabled} -and $Headers.Count) {{ throw 'CIDR mode acquired credentials' }}
            return [pscustomobject]@{{StatusCode=200}}
        }}
        function Invoke-RestMethod {{
            param($Uri,$Headers,$Method,$ContentType,$Body,$TimeoutSec,$MaximumRedirection)
            if (${enabled} -and $Headers['Authorization'] -ne 'Bearer PRIVATE_FAKE_TOKEN') {{
                throw 'Missing authenticated chat header'
            }}
            $request=$Body | ConvertFrom-Json
            return [pscustomobject]@{{
                reply='ok';conversation_id=$request.conversation_id;trace_id=$null
            }}
        }}
        Wait-AppHealth $deployment -Attempts 1
        Assert-AnonymousChatBlocked $deployment
        $null=Invoke-BaselineChat $deployment
    """)
    assert "PRIVATE_FAKE_TOKEN" not in stdout


def test_http_api_setup_requires_approval_and_reuses_registration(tmp_path, deployment):
    root = str(ROOT).replace("'", "''")
    artifact = str(tmp_path / "http-auth.json").replace("'", "''")
    account = json.dumps(
        {"tenantId": deployment["tenantId"], "id": deployment["subscriptionId"], "state": "Enabled"}
    )
    powershell(f"""
        $ErrorActionPreference='Stop'
        $global:ApiApp=$null; $global:ApiPrincipal=$null; $global:CreateCount=0
        function az {{
            $global:LASTEXITCODE=0
            if ($args[0] -eq 'account') {{
                return '{account}'
            }}
            if ($args[0] -ne 'rest') {{ throw 'Unexpected mocked CLI command' }}
            $url=$args[[array]::IndexOf($args,'--url')+1]
            if ($args[2] -eq 'get') {{
                if ($url -match '/applications/') {{
                    return ($global:ApiApp | ConvertTo-Json -Depth 20 -Compress)
                }}
                $items=@()
                if ($url -match '/applications\\?' -and $global:ApiApp) {{
                    $items=@($global:ApiApp)
                }}
                if ($url -match '/servicePrincipals\\?' -and $global:ApiPrincipal) {{
                    $items=@($global:ApiPrincipal)
                }}
                return (ConvertTo-Json -InputObject @{{value=$items}} -Depth 20 -Compress)
            }}
            $bodyPath=$args[[array]::IndexOf($args,'--body')+1].Substring(1)
            $body=Get-Content $bodyPath -Raw | ConvertFrom-Json -AsHashtable
            if ($args[2] -eq 'post') {{
                if ($url.EndsWith('/servicePrincipals')) {{
                    $global:ApiPrincipal=@{{id='{deployment["systemAssignedPrincipalId"]}'}}
                    return ($global:ApiPrincipal | ConvertTo-Json -Compress)
                }}
                $global:CreateCount++
                $global:ApiApp=$body
                $global:ApiApp.id='{deployment["systemAssignedPrincipalId"]}'
                $global:ApiApp.appId='{deployment["managedIdentityClientId"]}'
                $global:ApiApp.api=$null
                return ($global:ApiApp | ConvertTo-Json -Depth 20 -Compress)
            }}
            if ($args[2] -ne 'patch') {{ throw 'Unexpected Graph write' }}
            if ($body.ContainsKey('requiredResourceAccess') -or
                $body.ContainsKey('passwordCredentials')) {{
                throw 'Unexpected Graph workload permissions or credentials'
            }}
            if ($body.api.ContainsKey('preAuthorizedApplications') -and !$global:ApiApp.api) {{
                throw 'Scope must be persisted before it can be preauthorized'
            }}
            $global:ApiApp.api=$body.api
            $global:ApiApp.identifierUris=$body.identifierUris
        }}
        $setup=@{{
            TenantId='{deployment["tenantId"]}'; SubscriptionId='{deployment["subscriptionId"]}'
            ResourceGroup='{deployment["resourceGroup"]}'; WebAppName='{deployment["webAppName"]}'
            AuthFile='{artifact}'
        }}
        try {{ & '{root}\\scripts\\configure-http-auth.ps1' @setup }}
        catch {{ if ($_.Exception.Message -notlike '*Preview only*') {{ throw }} }}
        if ($global:CreateCount -or (Test-Path '{artifact}')) {{
            throw 'Preview changed directory/state'
        }}
        & '{root}\\scripts\\configure-http-auth.ps1' @setup -Approve
        $first=Get-Content '{artifact}' -Raw | ConvertFrom-Json
        & '{root}\\scripts\\configure-http-auth.ps1' @setup -Approve
        $second=Get-Content '{artifact}' -Raw | ConvertFrom-Json
        if ($global:CreateCount -ne 1 -or $first.clientId -ne $second.clientId -or
            $first.scopeId -ne $second.scopeId -or !$second.configured) {{
            throw 'HTTP API registration was duplicated or not verified'
        }}
    """)
