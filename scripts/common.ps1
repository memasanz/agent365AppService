#requires -Version 7.0
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:ProjectRoot = Split-Path $PSScriptRoot -Parent
$script:ModelRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
$script:MonitoringRoleId = '3913510d-42f4-4e42-8a64-420c390055eb'
$script:AzureCliClientId = '04b07795-8ddb-461a-bbee-02f9e1bf7b46'

function Invoke-Az {
    param([Parameter(Mandatory)][string[]]$Arguments, [switch]$NoOutput, [string]$Operation = 'Azure CLI operation')
    $PSNativeCommandUseErrorActionPreference = $false
    $format = if ($NoOutput) { 'none' } else { 'json' }
    # Capture both streams: CLI errors can contain complete settings/request bodies.
    $result = & az @Arguments --only-show-errors --output $format 2>&1
    if ($LASTEXITCODE -ne 0) {
        $hint = 'Check sign-in, permissions, network access, and the resource IDs.'
        if ($Arguments[0] -eq 'deployment' -and
            ($result -join "`n") -match 'InternalSubscriptionIsOverQuotaForSku') {
            $hint = 'App Service quota is insufficient for the requested SKU/region. In Azure Portal > Quotas, select App Service and request enough instance quota for this SKU/region. No SKU or region was changed automatically.'
        }
        throw "$Operation failed (Azure CLI exit $LASTEXITCODE). $hint No response payload was logged."
    }
    if (!$NoOutput -and $result) {
        try { return ($result -join "`n" | ConvertFrom-Json -AsHashtable) }
        catch { throw "$Operation returned an unexpected response; payload suppressed." }
    }
}

function Get-LocalPath {
    param([string]$Path)
    return $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
}

function Read-JsonFile {
    param([string]$Path)
    if (!(Test-Path -LiteralPath $Path -PathType Leaf)) { throw "Required local artifact not found: $Path" }
    try { return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json -AsHashtable }
    catch { throw 'Invalid JSON artifact; its contents were not logged.' }
}

function Assert-Guid {
    param([object]$Value, [string]$Name)
    $parsed = [guid]::Empty
    if (![guid]::TryParse([string]$Value, [ref]$parsed) -or $parsed -eq [guid]::Empty) { throw "Invalid or missing $Name." }
}

function Assert-ProjectEndpoint {
    param([string]$Value)
    $uri = $null
    if (![uri]::TryCreate($Value, [UriKind]::Absolute, [ref]$uri) -or $uri.Scheme -ne 'https' -or
        $uri.Host -notmatch '^[a-z0-9-]+\.services\.ai\.azure\.com$' -or
        $uri.AbsolutePath -notmatch '^/api/projects/[^/]+/?$' -or $uri.Query -or $uri.Fragment -or $uri.UserInfo -or !$uri.IsDefaultPort) {
        throw 'Use a commercial-cloud HTTPS Foundry project endpoint: https://ACCOUNT.services.ai.azure.com/api/projects/PROJECT.'
    }
}

function Assert-Cidrs {
    param([string[]]$Cidrs, [string]$Name)
    if (!$Cidrs -or $Cidrs.Count -gt 100) { throw "$Name requires 1-100 explicit caller CIDRs." }
    foreach ($cidr in $Cidrs) {
        $parts = $cidr.Split('/')
        $address = $null
        $prefix = 0
        if ($parts.Count -ne 2 -or ![Net.IPAddress]::TryParse($parts[0], [ref]$address) -or
            ![int]::TryParse($parts[1], [ref]$prefix)) { throw "Invalid CIDR in $Name." }
        $bits = if ($address.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetwork) { 32 } else { 128 }
        if ($prefix -le 0 -or $prefix -gt $bits) { throw "$Name must not contain unrestricted /0 networks or invalid prefixes." }
        if ($address.IsIPv4MappedToIPv6) { throw "Use an ordinary IPv4 or IPv6 CIDR in $Name." }
    }
}

function Protect-LocalDirectory {
    param([string]$Path)
    $null = New-Item -ItemType Directory -Path $Path -Force -WhatIf:$false
    if ($IsWindows) {
        $acl = [Security.AccessControl.DirectorySecurity]::new()
        $acl.SetAccessRuleProtection($true, $false)
        $user = [Security.Principal.WindowsIdentity]::GetCurrent().User
        $rule = [Security.AccessControl.FileSystemAccessRule]::new(
            $user, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        $acl.AddAccessRule($rule)
        [IO.FileSystemAclExtensions]::SetAccessControl([IO.DirectoryInfo]::new($Path), $acl)
    } else {
        & chmod 700 -- $Path
        if ($LASTEXITCODE -ne 0) { throw 'Could not protect local artifact directory.' }
    }
}

function Write-ProtectedJson {
    param([object]$Value, [string]$Path)
    $Path = Get-LocalPath $Path
    $directory = Split-Path $Path -Parent
    Protect-LocalDirectory $directory
    $Value | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath $Path -Encoding utf8 -NoNewline -WhatIf:$false
    if ($IsWindows) {
        $acl = [Security.AccessControl.FileSecurity]::new()
        $acl.SetAccessRuleProtection($true, $false)
        $user = [Security.Principal.WindowsIdentity]::GetCurrent().User
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($user, 'FullControl', 'Allow'))
        [IO.FileSystemAclExtensions]::SetAccessControl([IO.FileInfo]::new($Path), $acl)
    }
    if (!$IsWindows) {
        & chmod 600 -- $Path
        if ($LASTEXITCODE -ne 0) { throw 'Could not protect local artifact file.' }
    }
}

function Assert-AzureContext {
    param([string]$TenantId, [string]$SubscriptionId)
    Assert-Guid $TenantId 'tenantId'
    Assert-Guid $SubscriptionId 'subscriptionId'
    $account = Invoke-Az -Arguments @('account', 'show', '--subscription', $SubscriptionId) -Operation 'Subscription readback'
    if ($account.tenantId -ne $TenantId -or $account.id -ne $SubscriptionId -or $account.state -ne 'Enabled') {
        throw 'Azure CLI subscription/tenant does not match the intended enabled deployment. Sign in to the correct tenant.'
    }
}

function Assert-Deployment {
    param([hashtable]$Deployment)
    $required = @('tenantId', 'subscriptionId', 'resourceGroup', 'webAppName', 'webAppResourceId', 'hostname',
        'systemAssignedPrincipalId', 'managedIdentityResourceId', 'managedIdentityClientId', 'managedIdentityPrincipalId',
        'foundryProjectEndpoint', 'modelDeployment', 'modelAccessScope')
    foreach ($name in $required) { if (!$Deployment[$name]) { throw "Deployment artifact is missing $name." } }
    foreach ($name in @('systemAssignedPrincipalId', 'managedIdentityClientId', 'managedIdentityPrincipalId')) { Assert-Guid $Deployment[$name] $name }
    Assert-AzureContext $Deployment.tenantId $Deployment.subscriptionId
    Assert-ProjectEndpoint $Deployment.foundryProjectEndpoint
    $expectedId = "/subscriptions/$($Deployment.subscriptionId)/resourceGroups/$($Deployment.resourceGroup)/providers/Microsoft.Web/sites/$($Deployment.webAppName)"
    if ($Deployment.webAppResourceId -ne $expectedId) { throw 'Deployment web app resource ID does not match its subscription/group/name.' }
    if ($Deployment.managedIdentityResourceId -notmatch "^/subscriptions/$([regex]::Escape($Deployment.subscriptionId))/resourceGroups/[^/]+/providers/Microsoft.ManagedIdentity/userAssignedIdentities/[^/]+$") {
        throw 'Managed identity must be a resource in the intended subscription.'
    }
    $app = Invoke-Az -Arguments @('resource', 'show', '--ids', $expectedId, '--api-version', '2025-03-01', '--subscription', $Deployment.subscriptionId) -Operation 'Web app identity readback'
    $mi = Invoke-Az -Arguments @('resource', 'show', '--ids', $Deployment.managedIdentityResourceId, '--api-version', '2023-01-31', '--subscription', $Deployment.subscriptionId) -Operation 'Managed identity readback'
    if ($app.identity.principalId -ne $Deployment.systemAssignedPrincipalId -or $app.identity.tenantId -ne $Deployment.tenantId -or
        $app.properties.defaultHostName -ne $Deployment.hostname -or $Deployment.hostname -notmatch '^[a-z0-9.-]+\.azurewebsites\.net$' -or
        ($app.identity.userAssignedIdentities.Keys -notcontains $Deployment.managedIdentityResourceId) -or
        $mi.properties.clientId -ne $Deployment.managedIdentityClientId -or
        $mi.properties.principalId -ne $Deployment.managedIdentityPrincipalId -or $mi.properties.tenantId -ne $Deployment.tenantId) {
        throw 'Live App Service/managed identity/hostname does not match the intended deployment artifact.'
    }
    Assert-HttpAuthentication $Deployment
    return $app
}

function Assert-HttpAuthRegistration {
    param([hashtable]$Artifact)
    foreach ($name in @('tenantId', 'subscriptionId', 'applicationObjectId', 'clientId', 'scopeId')) {
        Assert-Guid $Artifact[$name] $name
    }
    $app = Invoke-Az -Arguments @('rest', '--method', 'get', '--url', "https://graph.microsoft.com/v1.0/applications/$($Artifact.applicationObjectId)",
        '--subscription', $Artifact.subscriptionId) -Operation 'HTTP API registration readback'
    $scopes = @($app.api.oauth2PermissionScopes | Where-Object { $_.id -eq $Artifact.scopeId -and $_.value -eq 'user_impersonation' -and $_.isEnabled })
    $clients = @($app.api.preAuthorizedApplications | Where-Object {
        $_.appId -eq $script:AzureCliClientId -and $_.delegatedPermissionIds -contains $Artifact.scopeId
    })
    if ($app.appId -ne $Artifact.clientId -or $app.signInAudience -ne 'AzureADMyOrg' -or
        $app.description -ne $Artifact.webAppResourceId -or $app.api.requestedAccessTokenVersion -ne 2 -or
        $app.identifierUris -notcontains "api://$($Artifact.clientId)" -or $scopes.Count -ne 1 -or $clients.Count -ne 1) {
        throw 'HTTP API registration does not match the saved single-tenant API/client/scope configuration.'
    }
}

function Assert-HttpAuthentication {
    param([hashtable]$Deployment)
    if (!$Deployment['httpAuthClientId']) { return }
    Assert-Guid $Deployment.httpAuthClientId 'HTTP API client ID'
    $url = "https://management.azure.com$($Deployment.webAppResourceId)/config/authsettingsV2/list?api-version=2025-03-01"
    $response = Invoke-Az -Arguments @('rest', '--method', 'get', '--url', $url, '--subscription', $Deployment.subscriptionId) -Operation 'App Service authentication readback'
    $auth = $response.properties
    $provider = $auth.identityProviders.azureActiveDirectory
    $audiences = @($provider.validation.allowedAudiences)
    $clients = @($provider.validation.defaultAuthorizationPolicy.allowedApplications)
    if (!$auth.platform.enabled -or !$auth.globalValidation.requireAuthentication -or
        $auth.globalValidation.unauthenticatedClientAction -ne 'Return401' -or
        $auth.globalValidation['excludedPaths'] -or !$auth.httpSettings.requireHttps -or
        !$provider.enabled -or $provider.registration.clientId -ne $Deployment.httpAuthClientId -or
        $provider.registration.openIdIssuer.TrimEnd('/') -ne "https://login.microsoftonline.com/$($Deployment.tenantId)/v2.0" -or
        $audiences.Count -ne 2 -or $audiences -notcontains $Deployment.httpAuthClientId -or
        $audiences -notcontains "api://$($Deployment.httpAuthClientId)" -or
        $clients.Count -ne 1 -or $clients[0] -ne $script:AzureCliClientId) {
        throw 'App Service must require single-tenant HTTPS bearer authentication for every route, with the expected API audience and Azure CLI client.'
    }
}

function Enable-PublicEntraAccess {
    param([hashtable]$Deployment)
    $path = Join-Path $script:ProjectRoot ".local\http-auth-settings-$([guid]::NewGuid().ToString('N')).json"
    $payload = @{ properties = @{
        platform = @{ enabled = $true; runtimeVersion = '~1' }
        globalValidation = @{ requireAuthentication = $true; unauthenticatedClientAction = 'Return401'; excludedPaths = @() }
        identityProviders = @{ azureActiveDirectory = @{
            enabled = $true
            registration = @{
                clientId = $Deployment.httpAuthClientId
                openIdIssuer = "https://login.microsoftonline.com/$($Deployment.tenantId)/v2.0"
            }
            validation = @{
                allowedAudiences = @($Deployment.httpAuthClientId, "api://$($Deployment.httpAuthClientId)")
                defaultAuthorizationPolicy = @{ allowedApplications = @($script:AzureCliClientId) }
            }
        } }
        httpSettings = @{ requireHttps = $true }
        login = @{ tokenStore = @{ enabled = $false } }
    } }
    try {
        Write-ProtectedJson $payload $path
        $url = "https://management.azure.com$($Deployment.webAppResourceId)/config/authsettingsV2?api-version=2025-03-01"
        Invoke-Az -Arguments @('rest', '--method', 'put', '--url', $url, '--headers', 'Content-Type=application/json',
            '--body', "@$path", '--subscription', $Deployment.subscriptionId) -NoOutput -Operation 'Require single-tenant bearer authentication'
        Assert-HttpAuthentication $Deployment
        Invoke-Az -Arguments @('webapp', 'config', 'access-restriction', 'set', '--resource-group', $Deployment.resourceGroup,
            '--name', $Deployment.webAppName, '--subscription', $Deployment.subscriptionId,
            '--default-action', 'Allow', '--scm-default-action', 'Allow', '--use-same-restrictions-for-scm-site', 'false') -NoOutput -Operation 'Open public networking after authentication readback'
    } finally { if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force -WhatIf:$false } }
}

function Get-CallerHeaders {
    param([hashtable]$Deployment)
    if (!$Deployment['httpAuthClientId']) { return @{} }
    $scope = "api://$($Deployment.httpAuthClientId)/user_impersonation"
    try {
        $token = Invoke-Az -Arguments @('account', 'get-access-token',
            '--subscription', $Deployment.subscriptionId, '--scope', $scope) -Operation 'HTTP caller token acquisition'
        if ([string]::IsNullOrWhiteSpace($token['accessToken'])) { throw 'Missing access token.' }
        return @{ Authorization = "Bearer $($token.accessToken)" }
    } catch {
        throw "Cannot acquire the HTTP caller token. Run: az login --tenant $($Deployment.tenantId) --scope $scope . No token or response body was logged."
    }
}

function Assert-AnonymousChatBlocked {
    param([hashtable]$Deployment)
    if (!$Deployment['httpAuthClientId']) { return }
    foreach ($headers in @(@{}, @{ Authorization = 'Bearer invalid-test-token' })) {
        $response = Invoke-WebRequest -Uri "https://$($Deployment.hostname)/chat" -Method Post -ContentType 'application/json' `
            -Body '{}' -Headers $headers -SkipHttpErrorCheck -TimeoutSec 30 -MaximumRedirection 0
        if ($response.StatusCode -ne 401) {
            throw 'Anonymous/invalid-token chat rejection was not verified. Do not treat this public endpoint as ready.'
        }
    }
    Write-Host 'Public API authentication: anonymous and invalid-token /chat requests rejected (HTTP 401).'
}

function Test-RoleAssignment {
    param([string]$PrincipalId, [string]$Scope, [string]$RoleId, [string]$SubscriptionId)
    $assignments = @(Invoke-Az -Arguments @('role', 'assignment', 'list', '--assignee-object-id', $PrincipalId,
        '--scope', $Scope, '--include-inherited', '--fill-principal-name', 'false', '--subscription', $SubscriptionId) -Operation 'Role assignment readback')
    return @($assignments | Where-Object {
        $_.roleDefinitionId -like "*/$RoleId" -and $_.principalId -eq $PrincipalId -and !$_['condition'] -and
        ($_.scope -eq $Scope -or $Scope.StartsWith("$($_.scope)/", [StringComparison]::OrdinalIgnoreCase))
    }).Count -gt 0
}

function Get-RoleAdminAction {
    param([string]$PrincipalId, [string]$Scope, [string]$RoleId, [string]$SubscriptionId)
    return "az role assignment create --assignee-object-id $PrincipalId --assignee-principal-type ServicePrincipal --role $RoleId --scope $Scope --subscription $SubscriptionId"
}

function Set-MergedAppSettings {
    param([hashtable]$Deployment, [hashtable]$Settings)
    $path = Join-Path $script:ProjectRoot ".local\settings-$([guid]::NewGuid().ToString('N')).json"
    try {
        Write-ProtectedJson $Settings $path
        Invoke-Az -Arguments @('webapp', 'config', 'appsettings', 'set', '--resource-group', $Deployment.resourceGroup,
            '--name', $Deployment.webAppName, '--subscription', $Deployment.subscriptionId, '--settings', "@$path") -NoOutput -Operation 'Additive app settings update'
    } finally { if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force -WhatIf:$false } }
}

function Get-AppSettings {
    param([hashtable]$Deployment)
    $entries = @(Invoke-Az -Arguments @('webapp', 'config', 'appsettings', 'list', '--resource-group', $Deployment.resourceGroup,
        '--name', $Deployment.webAppName, '--subscription', $Deployment.subscriptionId) -Operation 'App settings readback')
    $settings = @{}
    foreach ($entry in $entries) { $settings[$entry.name] = [string]$entry.value }
    return $settings
}

function Wait-SourceDeployment {
    param([hashtable]$Deployment, [string]$DeploymentId, [int]$Attempts = 120, [int]$PollSeconds = 10)
    Assert-Guid $DeploymentId 'source deployment ID'
    $previous = $null
    for ($i = 0; $i -lt $Attempts; $i++) {
        $records = $null
        try {
            $records = @(Invoke-Az -Arguments @('webapp', 'log', 'deployment', 'list',
                '--resource-group', $Deployment.resourceGroup, '--name', $Deployment.webAppName,
                '--subscription', $Deployment.subscriptionId) -Operation 'Source build status readback')
        } catch { }
        $record = @($records | Where-Object { $_ -and $_.id -eq $DeploymentId })
        if ($record.Count -gt 1) { throw 'Ambiguous source deployment status; no readiness claim is made.' }
        if ($record.Count -eq 1) {
            $status = [int]$record[0].status
            if ($status -eq 4) {
                Write-Host "Source deployment ${DeploymentId}: SUCCEEDED."
                return
            }
            if ($status -eq 3) {
                throw "Source build $DeploymentId failed. Inspect az webapp log deployment show for this ID; no response payload was logged."
            }
            if ($status -notin @(0, 1, 2)) { throw 'Unsupported source deployment status; no readiness claim is made.' }
            if ($status -ne $previous) {
                Write-Host "Source deployment ${DeploymentId}: pending/building/deploying (status $status)."
                $previous = $status
            }
        }
        if ($i -lt $Attempts - 1) { Start-Sleep -Seconds $PollSeconds }
    }
    throw "Source deployment $DeploymentId has not completed within the wait limit. It may still be building; inspect this SAME deployment before uploading again."
}

function Wait-AppHealth {
    param([hashtable]$Deployment, [int]$Attempts = 36)
    $headers = Get-CallerHeaders $Deployment
    for ($i = 0; $i -lt $Attempts; $i++) {
        try {
            $result = Invoke-WebRequest -Uri "https://$($Deployment.hostname)/health" -Headers $headers -TimeoutSec 20 -MaximumRedirection 0
            if ($result.StatusCode -eq 200) { return }
        } catch { }
        if ($i -lt $Attempts - 1) { Start-Sleep -Seconds 10 }
    }
    throw 'Health verification failed. Check App Service startup, HTTP authentication, or caller CIDRs; response bodies were suppressed.'
}

function Invoke-BaselineChat {
    param([hashtable]$Deployment, [string]$Message = 'Explain what this sample agent does in one short sentence.')
    $headers = Get-CallerHeaders $Deployment
    $conversation = "sample-$([guid]::NewGuid().ToString('N'))"
    $body = @{ message = $Message; conversation_id = $conversation } | ConvertTo-Json -Compress
    try {
        $response = Invoke-RestMethod -Uri "https://$($Deployment.hostname)/chat" -Method Post -ContentType 'application/json' -Body $body -Headers $headers -TimeoutSec 120 -MaximumRedirection 0
    } catch { throw 'Chat/model verification failed. Check HTTP authentication/caller CIDRs, system-assigned model permissions, and sanitized App Service diagnostics; HTTP response body suppressed.' }
    if ($response.reply -isnot [string] -or [string]::IsNullOrWhiteSpace($response.reply) -or $response.conversation_id -ne $conversation) { throw 'Unexpected chat response or correlation mismatch.' }
    $trace = if ($response.PSObject.Properties.Name -contains 'trace_id' -and $response.trace_id) { $response.trace_id } else { '(not returned; expected when telemetry is disabled)' }
    Write-Host "Deployment/model request: SUCCESS`nConversation ID: $conversation`nTrace ID: $trace"
    Write-Host "Reply: $($response.reply)"
    return $response
}

function Invoke-RuntimeReadback {
    param([string]$SettingsFile)
    $SettingsFile = Get-LocalPath $SettingsFile
    $python = Join-Path $script:ProjectRoot '.venv\Scripts\python.exe'
    if (!(Test-Path -LiteralPath $python)) { throw 'Create the repository .venv and install its requirements before administrative runtime validation.' }
    $PSNativeCommandUseErrorActionPreference = $false
    Push-Location $script:ProjectRoot
    try {
        $null = & $python -m src.onboarding validate-runtime --settings-file $SettingsFile 2>&1
        if ($LASTEXITCODE -ne 0) { throw 'Runtime relationship validation failed. Run the notebook/read-only validate-runtime helper interactively to resolve identity, registration, federation, or linked Foundry configuration; no settings changed.' }
    } finally { Pop-Location }
}
