#requires -Version 7.0
<#
.SYNOPSIS
Create/reuse the public Microsoft Graph client used by notebook 01.
.DESCRIPTION
Creates an ordinary single-tenant app registration, not an agent blueprint or managed identity.
Requires Microsoft.Graph.Authentication and an authorized app-registration administrator.
-Approve authorizes application/service-principal configuration. -GrantAdminConsent separately
authorizes tenant-wide delegated consent and requires an authorized consent administrator.
Existing permissions, consent scopes, and redirect URIs are preserved. No credentials are created.
After verification it sets only ENTRA_PROVISIONING_CLIENT_ID in -EnvFile (default: repository .env),
creating the file if needed and leaving other lines unchanged.
.EXAMPLE
.\scripts\New-AgentIdentityProvisioningClient.ps1 -TenantId '<TENANT-GUID>' -WhatIf
.EXAMPLE
.\scripts\New-AgentIdentityProvisioningClient.ps1 -TenantId '<TENANT-GUID>' -Approve -GrantAdminConsent
.EXAMPLE
.\scripts\New-AgentIdentityProvisioningClient.ps1 -TenantId '<TENANT-GUID>' -Name 'MyAgentProvisioner' -Approve
.EXAMPLE
.\scripts\New-AgentIdentityProvisioningClient.ps1 -TenantId '<TENANT-GUID>' -ApplicationObjectId '<APP-OBJECT-GUID>' -Approve
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium')]
param(
    [Parameter(Mandatory)][guid]$TenantId,
    [Alias('Name')][ValidateNotNullOrEmpty()][string]$DisplayName = 'Agent365IdentityProvisioner',
    [guid]$ApplicationObjectId = [guid]::Empty,
    [string]$EnvFile = (Join-Path (Split-Path $PSScriptRoot -Parent) '.env'),
    [switch]$Approve,
    [switch]$GrantAdminConsent
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($TenantId -eq [guid]::Empty) { throw 'TenantId must be a nonempty tenant GUID.' }
if ([string]::IsNullOrWhiteSpace($DisplayName)) { throw 'DisplayName must not be blank.' }

$graphBase = 'https://graph.microsoft.com/v1.0'
$graphAppId = '00000003-0000-0000-c000-000000000000'
$redirectUri = 'http://localhost:8400'
$permissions = @(
    'AgentIdentityBlueprint.Create'
    'AgentIdentityBlueprint.Read.All'
    'AgentIdentityBlueprintPrincipal.Create'
    'AgentIdentityBlueprintPrincipal.Read.All'
    'AgentIdentity.Create.All'
    'AgentIdentity.Read.All'
    'AgentRegistration.ReadWrite.All'
    'User.ReadBasic.All'
)
Write-Host "PREVIEW: create/reuse ordinary single-tenant public client '$DisplayName' in tenant $TenantId."
Write-Host "Desktop redirect: $redirectUri. Delegated Microsoft Graph permissions: $($permissions -join ', ')."
Write-Host "Admin consent explicitly authorized: $GrantAdminConsent. No secrets, agent identities, Azure roles, or App Service changes. Client ID will be written to $EnvFile."
if (!$Approve -and !$WhatIfPreference) {
    throw 'Preview only: rerun with -Approve, and -GrantAdminConsent only when authorized to grant consent.'
}
if (!(Get-Module -ListAvailable Microsoft.Graph.Authentication)) {
    throw 'Install the prerequisite: Install-Module Microsoft.Graph.Authentication -Scope CurrentUser'
}
Import-Module Microsoft.Graph.Authentication
$connectionScopes = @('Application.ReadWrite.All')
if ($GrantAdminConsent) { $connectionScopes += 'DelegatedPermissionGrant.ReadWrite.All' }
Connect-MgGraph -TenantId $TenantId.ToString() -Scopes $connectionScopes -ContextScope Process -NoWelcome
$context = Get-MgContext
if (!$context -or [guid]$context.TenantId -ne $TenantId -or
    @($connectionScopes | Where-Object { $_ -notin $context.Scopes }).Count) {
    throw 'Graph sign-in tenant or bootstrap scopes do not match. No application changes were made.'
}

function Invoke-ProvisioningGraph {
    param([string]$Method, [string]$Uri, [hashtable]$Body)
    $arguments = @{ Method = $Method; Uri = $Uri; OutputType = 'Hashtable'; ErrorAction = 'Stop' }
    if ($null -ne $Body) {
        $arguments.Body = $Body | ConvertTo-Json -Depth 12
        $arguments.ContentType = 'application/json'
    }
    try { return Invoke-MgGraphRequest @arguments }
    catch { throw "Microsoft Graph $Method failed. Check administrator permissions and retry using the saved application object ID; response body withheld." }
}

function Get-GraphCollection {
    param([string]$Uri)
    while ($Uri) {
        $page = Invoke-ProvisioningGraph 'GET' $Uri
        foreach ($item in $page['value']) { $item }
        $Uri = $page['@odata.nextLink']
        if ($Uri -and !$Uri.StartsWith("$graphBase/")) { throw 'Unexpected Graph pagination URL.' }
    }
}

$graphPrincipals = @(Get-GraphCollection "$graphBase/servicePrincipals?`$filter=appId eq '$graphAppId'&`$select=id,oauth2PermissionScopes")
if ($graphPrincipals.Count -ne 1) { throw 'Cannot uniquely resolve the Microsoft Graph service principal.' }
$graphPrincipal = $graphPrincipals[0]
$requestedAccess = @(
    foreach ($permission in $permissions) {
        $matches = @($graphPrincipal['oauth2PermissionScopes'] | Where-Object {
            $_['value'] -eq $permission -and $_['isEnabled']
        })
        if ($matches.Count -ne 1) { throw "Delegated permission '$permission' is unavailable. No application changes were made." }
        @{ id = $matches[0]['id']; type = 'Scope' }
    }
)

$select = 'id,appId,displayName,signInAudience,publicClient,requiredResourceAccess'
if ($ApplicationObjectId -ne [guid]::Empty) {
    $application = Invoke-ProvisioningGraph 'GET' "$graphBase/applications/$ApplicationObjectId`?`$select=$select"
} else {
    $filter = [uri]::EscapeDataString("displayName eq '$($DisplayName.Replace("'", "''"))'")
    $applications = @(Get-GraphCollection "$graphBase/applications?`$filter=$filter&`$select=$select")
    if ($applications.Count -gt 1) {
        throw 'Multiple applications match this name. Select the intended one with -ApplicationObjectId.'
    }
    $application = if ($applications.Count -eq 1) { $applications[0] } else { $null }
}
if ($application -and ($application['signInAudience'] -ne 'AzureADMyOrg' -or
    ($application['@odata.type'] -and $application['@odata.type'] -ne '#microsoft.graph.application'))) {
    throw 'The selected object must be an ordinary single-tenant app registration, not an agent blueprint.'
}
$action = 'Configure the public client, eight delegated notebook scopes, and service principal'
if ($GrantAdminConsent) { $action += '; grant tenant-wide delegated consent for those scopes' }
if (!$PSCmdlet.ShouldProcess("$DisplayName in tenant $TenantId", $action)) { return }

$requiredResourceAccess = @()
$redirectUris = @($redirectUri)
$resourceAccess = @($requestedAccess)
if ($application) {
    foreach ($resource in $application['requiredResourceAccess']) {
        if ($resource['resourceAppId'] -eq $graphAppId) {
            $resourceAccess += @($resource['resourceAccess'])
        } else { $requiredResourceAccess += $resource }
    }
    if ($application['publicClient']) { $redirectUris += @($application['publicClient']['redirectUris']) }
}
$resourceAccess = @($resourceAccess | Group-Object { "$($_['id']):$($_['type'])" } | ForEach-Object {
    @{ id = $_.Group[0]['id']; type = $_.Group[0]['type'] }
})
$requiredResourceAccess += @{ resourceAppId = $graphAppId; resourceAccess = $resourceAccess }
$body = @{
    publicClient = @{ redirectUris = @($redirectUris | Sort-Object -Unique) }
    requiredResourceAccess = $requiredResourceAccess
}
if ($application) {
    $applicationId = $application['id']
    $null = Invoke-ProvisioningGraph 'PATCH' "$graphBase/applications/$applicationId" $body
} else {
    $body.displayName = $DisplayName
    $body.signInAudience = 'AzureADMyOrg'
    $application = Invoke-ProvisioningGraph 'POST' "$graphBase/applications" $body
    $applicationId = $application['id']
}
$clientId = $application['appId']
Write-Host "Application object ID: $applicationId"
Write-Host "Client ID: $clientId. If a later step fails, rerun with -ApplicationObjectId $applicationId."

$clientPrincipals = @(Get-GraphCollection "$graphBase/servicePrincipals?`$filter=appId eq '$clientId'&`$select=id,appId")
if ($clientPrincipals.Count -gt 1) { throw 'Multiple client service principals found; reconcile before retrying.' }
$clientPrincipal = if ($clientPrincipals.Count -eq 1) { $clientPrincipals[0] } else {
    Invoke-ProvisioningGraph 'POST' "$graphBase/servicePrincipals" @{ appId = $clientId }
}

if ($GrantAdminConsent) {
    $filter = [uri]::EscapeDataString("clientId eq '$($clientPrincipal['id'])' and resourceId eq '$($graphPrincipal['id'])'")
    $grantUri = "$graphBase/oauth2PermissionGrants?`$filter=$filter"
    $grants = @(Get-GraphCollection $grantUri | Where-Object { $_['consentType'] -eq 'AllPrincipals' })
    if ($grants.Count -gt 1) { throw 'Multiple tenant-wide grants found; reconcile before retrying.' }
    $scopeNames = @($permissions)
    if ($grants.Count -eq 1) { $scopeNames += @($grants[0]['scope'] -split ' ' | Where-Object { $_ }) }
    $consentBody = @{ scope = ($scopeNames | Sort-Object -Unique) -join ' ' }
    if ($grants.Count -eq 1) {
        $null = Invoke-ProvisioningGraph 'PATCH' "$graphBase/oauth2PermissionGrants/$($grants[0]['id'])" $consentBody
    } else {
        $consentBody.clientId = $clientPrincipal['id']
        $consentBody.resourceId = $graphPrincipal['id']
        $consentBody.consentType = 'AllPrincipals'
        $null = Invoke-ProvisioningGraph 'POST' "$graphBase/oauth2PermissionGrants" $consentBody
    }
    $verifiedGrants = @(Get-GraphCollection $grantUri | Where-Object { $_['consentType'] -eq 'AllPrincipals' })
    if ($verifiedGrants.Count -ne 1 -or
        @($permissions | Where-Object { $_ -notin @($verifiedGrants[0]['scope'] -split ' ') }).Count) {
        throw 'Required delegated admin consent could not be verified.'
    }
}

$verifiedApp = Invoke-ProvisioningGraph 'GET' "$graphBase/applications/$applicationId`?`$select=$select"
$configuredScopes = @(
    foreach ($resource in $verifiedApp['requiredResourceAccess']) {
        if ($resource['resourceAppId'] -eq $graphAppId) {
            foreach ($access in $resource['resourceAccess']) {
                if ($access['type'] -eq 'Scope') { $access['id'] }
            }
        }
    }
)
if ($verifiedApp['appId'] -ne $clientId -or $verifiedApp['signInAudience'] -ne 'AzureADMyOrg' -or
    $redirectUri -notin @($verifiedApp['publicClient']['redirectUris']) -or
    @($requestedAccess | Where-Object { $_['id'] -notin $configuredScopes }).Count) {
    throw 'Application redirect/permission readback failed.'
}
$verifiedPrincipal = Invoke-ProvisioningGraph 'GET' "$graphBase/servicePrincipals/$($clientPrincipal['id'])?`$select=id,appId"
if ($verifiedPrincipal['appId'] -ne $clientId) { throw 'Service principal belongs to a different application.' }
$envLine = "ENTRA_PROVISIONING_CLIENT_ID=$clientId"
$envLines = if (Test-Path -LiteralPath $EnvFile) { @(Get-Content -LiteralPath $EnvFile) } else { @() }
if (@($envLines | Where-Object { $_ -match '^\s*ENTRA_PROVISIONING_CLIENT_ID\s*=' }).Count) {
    $envLines = @($envLines | ForEach-Object { if ($_ -match '^\s*ENTRA_PROVISIONING_CLIENT_ID\s*=') { $envLine } else { $_ } })
} else { $envLines += $envLine }
Set-Content -LiteralPath $EnvFile -Value $envLines -Encoding utf8NoBOM
Write-Host "Verified. Wrote $envLine to $EnvFile."
if (!$GrantAdminConsent) { Write-Host 'Consent was not changed. An administrator must ensure the eight notebook scopes are consented before notebook sign-in.' }
[pscustomobject]@{
    applicationObjectId = $applicationId
    clientId = $clientId
    servicePrincipalId = $clientPrincipal['id']
}
