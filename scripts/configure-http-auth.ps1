#requires -Version 7.0
<#
.SYNOPSIS
Create/reuse a single-tenant bearer-token API registration, without secrets or Graph workload permissions.
.DESCRIPTION
-Approve explicitly authorizes creating the API application/service principal and preauthorizing
Azure CLI for its user_impersonation scope. It does not configure/open the web app.
All tenant users can obtain delegated API tokens through Azure CLI, subject to tenant policy.
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium')]
param(
    [Parameter(Mandatory)][string]$TenantId,
    [Parameter(Mandatory)][string]$SubscriptionId,
    [Parameter(Mandatory)][string]$ResourceGroup,
    [Parameter(Mandatory)][ValidatePattern('^[a-zA-Z0-9][a-zA-Z0-9-]{0,58}[a-zA-Z0-9]$')][string]$WebAppName,
    [string]$AuthFile = (Join-Path (Split-Path $PSScriptRoot -Parent) '.local\http-auth.json'),
    [switch]$Approve
)
. (Join-Path $PSScriptRoot 'common.ps1')
Assert-AzureContext $TenantId $SubscriptionId
$AuthFile = Get-LocalPath $AuthFile
$siteId = "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup/providers/Microsoft.Web/sites/$WebAppName"
$name = "$WebAppName-http-api"
$state = if (Test-Path -LiteralPath $AuthFile) { Read-JsonFile $AuthFile } else {
    @{ schemaVersion = 1; tenantId = $TenantId; subscriptionId = $SubscriptionId; webAppResourceId = $siteId
        scopeId = [guid]::NewGuid().ToString(); createPending = $false; configured = $false }
}
if ($state.schemaVersion -ne 1 -or $state.tenantId -ne $TenantId -or
    $state.subscriptionId -ne $SubscriptionId -or $state.webAppResourceId -ne $siteId) {
    throw 'HTTP authentication artifact belongs to another tenant/deployment.'
}
$app = $null
if ($state['applicationObjectId']) {
    $app = Invoke-Az -Arguments @('rest', '--method', 'get', '--url', "https://graph.microsoft.com/v1.0/applications/$($state.applicationObjectId)",
        '--subscription', $SubscriptionId) -Operation 'Existing HTTP API application readback'
} else {
    $filter = [uri]::EscapeDataString("displayName eq '$name'")
    $response = Invoke-Az -Arguments @('rest', '--method', 'get', '--url', "https://graph.microsoft.com/v1.0/applications?`$filter=$filter",
        '--subscription', $SubscriptionId) -Operation 'HTTP API application lookup'
    $matches = @($response.value)
    if ($matches.Count -gt 1) { throw 'Multiple API application matches; reconcile before continuing.' }
    if ($matches.Count -eq 1) { $app = $matches[0] }
}
if ($app -and ($app.description -ne $siteId -or $app.signInAudience -ne 'AzureADMyOrg')) {
    throw 'An unrelated API application matches this name; it will not be adopted or patched.'
}
if (!$app -and $state.createPending) { throw 'Previous API application create is unresolved. Reconcile before recreating.' }
Write-Host "PREVIEW: create/reuse dedicated single-tenant API registration '$name' and its service principal."
Write-Host 'Expose only user_impersonation and preauthorize Azure CLI for that API scope. No Graph API permissions, secrets, or per-user restriction.'
Write-Host 'This does NOT open networking. deploy.ps1 will require/verify Entra authentication before opening the site and SCM.'
if (!$Approve -and !$WhatIfPreference) { throw 'Preview only: rerun with -Approve to authorize the described directory changes.' }
if (!$PSCmdlet.ShouldProcess($siteId, 'Create/configure dedicated single-tenant HTTP API registration and service principal')) { return }
$path = Join-Path $script:ProjectRoot ".local\api-registration-$([guid]::NewGuid().ToString('N')).json"
try {
    if (!$app) {
        $state.createPending = $true
        Write-ProtectedJson $state $AuthFile
        Write-ProtectedJson @{ displayName = $name; description = $siteId; signInAudience = 'AzureADMyOrg' } $path
        $app = Invoke-Az -Arguments @('rest', '--method', 'post', '--url', 'https://graph.microsoft.com/v1.0/applications',
            '--headers', 'Content-Type=application/json', '--body', "@$path", '--subscription', $SubscriptionId) -Operation 'HTTP API application creation'
        $state.createdApplication = $true
    }
    if ($state['clientId'] -and $state.clientId -ne $app.appId) { throw 'API client ID conflicts with saved state.' }
    $state.applicationObjectId = $app.id
    $state.clientId = $app.appId
    $state.createPending = $false
    Write-ProtectedJson $state $AuthFile
    $existingScopes = @()
    if ($app['api'] -and $app.api['oauth2PermissionScopes']) {
        $existingScopes = @($app.api.oauth2PermissionScopes)
    }
    if ($existingScopes.Count -gt 1 -or ($existingScopes.Count -eq 1 -and $existingScopes[0].value -ne 'user_impersonation')) {
        throw 'Existing API has unrelated scopes; it will not be overwritten.'
    }
    if ($existingScopes.Count -eq 1) { $state.scopeId = $existingScopes[0].id }
    $payload = @{
        identifierUris = @("api://$($state.clientId)")
        api = @{
            requestedAccessTokenVersion = 2
            oauth2PermissionScopes = @(@{
                id = $state.scopeId; value = 'user_impersonation'; type = 'User'; isEnabled = $true
                adminConsentDisplayName = 'Call the learning agent'; adminConsentDescription = 'Call this learning agent as a signed-in tenant user.'
                userConsentDisplayName = 'Call the learning agent'; userConsentDescription = 'Call this learning agent on your behalf.'
            })
        }
    }
    Write-ProtectedJson $payload $path
    Invoke-Az -Arguments @('rest', '--method', 'patch', '--url', "https://graph.microsoft.com/v1.0/applications/$($state.applicationObjectId)",
        '--headers', 'Content-Type=application/json', '--body', "@$path", '--subscription', $SubscriptionId) -NoOutput -Operation 'Configure owned HTTP API audience and scope'
    # Graph requires the scope to exist before another update can preauthorize it.
    $readback = Invoke-Az -Arguments @('rest', '--method', 'get', '--url', "https://graph.microsoft.com/v1.0/applications/$($state.applicationObjectId)",
        '--subscription', $SubscriptionId) -Operation 'HTTP API scope readback'
    if (@($readback.api.oauth2PermissionScopes | Where-Object { $_.id -eq $state.scopeId -and $_.isEnabled }).Count -ne 1) {
        throw 'HTTP API scope is not visible yet; keep state and retry after directory propagation.'
    }
    $payload.api.preAuthorizedApplications = @(@{ appId = $script:AzureCliClientId; delegatedPermissionIds = @($state.scopeId) })
    Write-ProtectedJson $payload $path
    Invoke-Az -Arguments @('rest', '--method', 'patch', '--url', "https://graph.microsoft.com/v1.0/applications/$($state.applicationObjectId)",
        '--headers', 'Content-Type=application/json', '--body', "@$path", '--subscription', $SubscriptionId) -NoOutput -Operation 'Preauthorize Azure CLI for the existing API scope'
    $filter = [uri]::EscapeDataString("appId eq '$($state.clientId)'")
    $response = Invoke-Az -Arguments @('rest', '--method', 'get', '--url', "https://graph.microsoft.com/v1.0/servicePrincipals?`$filter=$filter",
        '--subscription', $SubscriptionId) -Operation 'HTTP API principal lookup'
    $principals = @($response.value)
    if ($principals.Count -gt 1) { throw 'Ambiguous API service principal relationship.' }
    if (!$principals.Count) {
        Write-ProtectedJson @{ appId = $state.clientId } $path
        $principal = Invoke-Az -Arguments @('rest', '--method', 'post', '--url', 'https://graph.microsoft.com/v1.0/servicePrincipals',
            '--headers', 'Content-Type=application/json', '--body', "@$path",
            '--subscription', $SubscriptionId) -Operation 'HTTP API principal creation'
        $state.servicePrincipalId = $principal.id
        $state.createdServicePrincipal = $true
    } else { $state.servicePrincipalId = $principals[0].id }
    Assert-HttpAuthRegistration $state
    $state.configured = $true
    Write-ProtectedJson $state $AuthFile
    Write-Host "HTTP API registration verified. Client ID: $($state.clientId)"
    Write-Host "Protected artifact: $AuthFile"
    Write-Host "Next: add -HttpAuthFile '$AuthFile' to deploy.ps1. All tenant users may call with an Azure CLI-issued API token."
} finally { if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force -WhatIf:$false } }
