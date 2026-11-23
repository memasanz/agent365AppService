#requires -Version 7.0
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:ProjectRoot = Split-Path $PSScriptRoot -Parent
$script:ModelRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
$script:MonitoringRoleId = '3913510d-42f4-4e42-8a64-420c390055eb'

function Invoke-Az {
    param([Parameter(Mandatory)][string[]]$Arguments, [switch]$NoOutput, [string]$Operation = 'Azure CLI operation')
    $PSNativeCommandUseErrorActionPreference = $false
    $format = if ($NoOutput) { 'none' } else { 'json' }
    # Capture both streams: CLI errors can contain complete settings/request bodies.
    $result = & az @Arguments --only-show-errors --output $format 2>&1
    if ($LASTEXITCODE -ne 0) { throw "$Operation failed (Azure CLI exit $LASTEXITCODE). No response payload was logged. Check sign-in, permissions, network access, and the resource IDs." }
    if (!$NoOutput -and $result) {
        try { return ($result -join "`n" | ConvertFrom-Json -AsHashtable) }
        catch { throw "$Operation returned an unexpected response; payload suppressed." }
    }
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
    $directory = Split-Path ([IO.Path]::GetFullPath($Path)) -Parent
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
        !$app.identity.userAssignedIdentities.ContainsKey($Deployment.managedIdentityResourceId) -or
        $mi.properties.clientId -ne $Deployment.managedIdentityClientId -or
        $mi.properties.principalId -ne $Deployment.managedIdentityPrincipalId -or $mi.properties.tenantId -ne $Deployment.tenantId) {
        throw 'Live App Service/managed identity/hostname does not match the intended deployment artifact.'
    }
    return $app
}

function Test-RoleAssignment {
    param([string]$PrincipalId, [string]$Scope, [string]$RoleId, [string]$SubscriptionId)
    $assignments = @(Invoke-Az -Arguments @('role', 'assignment', 'list', '--assignee-object-id', $PrincipalId,
        '--scope', $Scope, '--include-inherited', '--all', '--fill-principal-name', 'false', '--subscription', $SubscriptionId) -Operation 'Role assignment readback')
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

function Wait-AppHealth {
    param([string]$Hostname, [int]$Attempts = 36)
    for ($i = 0; $i -lt $Attempts; $i++) {
        try {
            $result = Invoke-WebRequest -Uri "https://$Hostname/health" -TimeoutSec 20 -MaximumRedirection 0
            if ($result.StatusCode -eq 200) { return }
        } catch { }
        if ($i -lt $Attempts - 1) { Start-Sleep -Seconds 10 }
    }
    throw 'Health verification failed. Check App Service startup and confirm this machine is in the caller CIDRs; response bodies were suppressed.'
}

function Invoke-BaselineChat {
    param([string]$Hostname, [string]$Message = 'Explain what this sample agent does in one short sentence.')
    $conversation = "sample-$([guid]::NewGuid().ToString('N'))"
    $body = @{ message = $Message; conversation_id = $conversation } | ConvertTo-Json -Compress
    try {
        $response = Invoke-RestMethod -Uri "https://$Hostname/chat" -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 120 -MaximumRedirection 0
    } catch { throw 'Chat/model verification failed. Check caller CIDRs, system-assigned model permissions, and sanitized App Service diagnostics; HTTP response body suppressed.' }
    if ($response.reply -isnot [string] -or [string]::IsNullOrWhiteSpace($response.reply) -or $response.conversation_id -ne $conversation) { throw 'Unexpected chat response or correlation mismatch.' }
    $trace = if ($response.PSObject.Properties.Name -contains 'trace_id' -and $response.trace_id) { $response.trace_id } else { '(not returned; expected when telemetry is disabled)' }
    Write-Host "Deployment/model request: SUCCESS`nConversation ID: $conversation`nTrace ID: $trace"
    Write-Host "Reply: $($response.reply)"
    return $response
}

function Invoke-RuntimeReadback {
    param([string]$SettingsFile)
    $python = Join-Path $script:ProjectRoot '.venv\Scripts\python.exe'
    if (!(Test-Path -LiteralPath $python)) { throw 'Create the repository .venv and install its requirements before administrative runtime validation.' }
    $PSNativeCommandUseErrorActionPreference = $false
    Push-Location $script:ProjectRoot
    try {
        $null = & $python -m src.onboarding validate-runtime --settings-file ([IO.Path]::GetFullPath($SettingsFile)) 2>&1
        if ($LASTEXITCODE -ne 0) { throw 'Runtime relationship validation failed. Run the notebook/read-only validate-runtime helper interactively to resolve identity, registration, federation, or linked Foundry configuration; no settings changed.' }
    } finally { Pop-Location }
}
