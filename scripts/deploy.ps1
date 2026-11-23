#requires -Version 7.0
<#
.SYNOPSIS
Preview and explicitly approve a CIDR-restricted Linux App Service source deployment.
.DESCRIPTION
Uses an existing Foundry account/project/model; does not create or alter them. -Approve
authorizes the displayed billable infrastructure/configuration/source changes, not RBAC.
-AssignModelRole separately authorizes the narrow account-scoped model role.
-WhatIf performs reads and previews only. No publishing passwords or client secrets are used.
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium')]
param(
    [Parameter(Mandatory)][string]$TenantId,
    [Parameter(Mandatory)][string]$SubscriptionId,
    [Parameter(Mandatory)][ValidatePattern('^[\w().-]+$')][string]$ResourceGroup,
    [Parameter(Mandatory)][ValidatePattern('^[a-z0-9]+$')][string]$Location,
    [Parameter(Mandatory)][ValidatePattern('^[a-zA-Z0-9][a-zA-Z0-9-]{0,58}[a-zA-Z0-9]$')][string]$WebAppName,
    [Parameter(Mandatory)][string]$FoundryProjectEndpoint,
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$ModelDeployment,
    [Parameter(Mandatory)][string]$ModelAccessScope,
    [Parameter(Mandatory)][string[]]$CallerCidrs,
    [Parameter(Mandatory)][string[]]$ScmCallerCidrs,
    [ValidateSet('B1', 'S1', 'P1v3')][string]$Sku = 'B1',
    [ValidatePattern('^[a-zA-Z0-9-]+$')][string]$PlanName = "$WebAppName-plan",
    [ValidatePattern('^[a-zA-Z0-9-]+$')][string]$ManagedIdentityName = "$WebAppName-blueprint-mi",
    [string]$DeploymentFile = (Join-Path (Split-Path $PSScriptRoot -Parent) '.local\deployment.json'),
    [switch]$AssignModelRole,
    [switch]$Approve
)
. (Join-Path $PSScriptRoot 'common.ps1')
$cliVersion = Invoke-Az -Arguments @('version') -Operation 'Azure CLI version check'
if ([version]$cliVersion['azure-cli'] -lt [version]'2.48.1') { throw 'Azure CLI 2.48.1 or later is required for Entra-authenticated ZIP deployment with SCM basic authentication disabled.' }
Assert-AzureContext $TenantId $SubscriptionId
Assert-ProjectEndpoint $FoundryProjectEndpoint
Assert-Cidrs $CallerCidrs 'CallerCidrs'
Assert-Cidrs $ScmCallerCidrs 'ScmCallerCidrs'

if ($ModelAccessScope -notmatch '^/subscriptions/([0-9a-f-]{36})/resourceGroups/[^/]+/providers/Microsoft.CognitiveServices/accounts/[^/]+$') {
    throw 'ModelAccessScope must be the exact existing Microsoft.CognitiveServices/accounts resource ID, not a subscription, resource group, project, or deployment.'
}
$modelSubscriptionId = $Matches[1]
Assert-AzureContext $TenantId $modelSubscriptionId
$modelAccount = Invoke-Az -Arguments @('resource', 'show', '--ids', $ModelAccessScope, '--api-version', '2025-06-01', '--subscription', $modelSubscriptionId) -Operation 'Model account readback'
if ($modelAccount.kind -notin @('AIServices', 'OpenAI') -or
    !$modelAccount.properties.customSubDomainName -or
    ([uri]$FoundryProjectEndpoint).Host -ne "$($modelAccount.properties.customSubDomainName).services.ai.azure.com") {
    throw 'The model-access scope must be the AIServices/OpenAI account owning the supplied Foundry project endpoint. No broader scope is accepted.'
}
$model = Invoke-Az -Arguments @('cognitiveservices', 'account', 'deployment', 'show', '--resource-group', $modelAccount.resourceGroup,
    '--name', $modelAccount.name, '--deployment-name', $ModelDeployment, '--subscription', $modelSubscriptionId) -Operation 'Model deployment readback'
if ($model.properties.provisioningState -ne 'Succeeded') { throw 'The supplied model deployment is not successfully provisioned.' }

$template = Join-Path $script:ProjectRoot 'infra\main.bicep'
if (!(Test-Path (Join-Path $script:ProjectRoot 'requirements.txt')) -or !(Test-Path (Join-Path $script:ProjectRoot 'src\app.py'))) {
    throw 'The source root must contain requirements.txt and src\app.py before deployment.'
}
$groupExists = Invoke-Az -Arguments @('group', 'exists', '--name', $ResourceGroup, '--subscription', $SubscriptionId) -Operation 'Resource group existence read'
$siteId = "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup/providers/Microsoft.Web/sites/$WebAppName"
$miId = "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup/providers/Microsoft.ManagedIdentity/userAssignedIdentities/$ManagedIdentityName"
$existingPrincipal = $null
$retainedIdentities = @()
if ($groupExists) {
    $resources = @(Invoke-Az -Arguments @('resource', 'list', '--resource-group', $ResourceGroup, '--subscription', $SubscriptionId) -Operation 'Existing resource inventory')
    if (@($resources | Where-Object { $_.id -eq $siteId }).Count) {
        $existing = Invoke-Az -Arguments @('resource', 'show', '--ids', $siteId, '--api-version', '2025-03-01', '--subscription', $SubscriptionId) -Operation 'Existing web app read'
        if ($existing.kind -notmatch 'linux') { throw 'Refusing to convert an existing non-Linux App Service.' }
        if ($existing['identity']) {
            $existingPrincipal = $existing.identity['principalId']
            if ($existing.identity['userAssignedIdentities']) { $retainedIdentities = @($existing.identity.userAssignedIdentities.Keys) }
        }
        $before = Get-AppSettings @{ resourceGroup = $ResourceGroup; webAppName = $WebAppName; subscriptionId = $SubscriptionId }
        if ($before.ContainsKey('AZURE_CLIENT_ID')) { throw 'Remove the existing global AZURE_CLIENT_ID setting explicitly before proceeding; hosted model access must select the system-assigned identity.' }
        if ($before['WEBSITE_RUN_FROM_PACKAGE'] -and $before['WEBSITE_RUN_FROM_PACKAGE'] -ne '0') {
            throw 'Existing WEBSITE_RUN_FROM_PACKAGE is incompatible with this Oryx source ZIP build. Remove that setting explicitly before deployment.'
        }
    }
    if (@($resources | Where-Object { $_.id -eq $miId }).Count) {
        $existingMi = Invoke-Az -Arguments @('resource', 'show', '--ids', $miId, '--api-version', '2023-01-31', '--subscription', $SubscriptionId) -Operation 'Dedicated UAMI ownership read'
        if (!$existingMi['tags'] -or $existingMi.tags['agent365StarterWebAppResourceId'] -ne $siteId) {
            throw 'An existing managed identity with this name is not marked as dedicated to this app. Choose a new ManagedIdentityName; unrelated/shared identities are not adopted.'
        }
    }
}
$values = @{
    location = $Location; webAppName = $WebAppName; planName = $PlanName; managedIdentityName = $ManagedIdentityName
    sku = $Sku; callerCidrs = $CallerCidrs; scmCallerCidrs = $ScmCallerCidrs; existingUserAssignedIdentityIds = $retainedIdentities
}
$parameters = @{ '$schema' = 'https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#'; contentVersion = '1.0.0.0'; parameters = @{} }
foreach ($key in $values.Keys) { $parameters.parameters[$key] = @{ value = $values[$key] } }
$parameterPath = Join-Path $script:ProjectRoot ".local\deploy-parameters-$([guid]::NewGuid().ToString('N')).json"
$zipPath = Join-Path $script:ProjectRoot ".local\app-$([guid]::NewGuid().ToString('N')).zip"
try {
    Write-ProtectedJson $parameters $parameterPath
    Write-Host "PREVIEW: tenant $TenantId; subscription $SubscriptionId; resource group $ResourceGroup ($Location)."
    Write-Host "Create/update Linux plan '$PlanName' ($Sku, one worker), Python 3.12 app '$WebAppName', dedicated UAMI '$ManagedIdentityName', both app identities, HTTPS/TLS, disabled basic publishing."
    Write-Host "Caller allowlist: $($CallerCidrs -join ', '); independent SCM deployment allowlist: $($ScmCallerCidrs -join ', '); unmatched traffic DENIED on both."
    Write-Host 'The current deployment machine must be in BOTH applicable allowlists. CIDRs restrict networks, not individual users.'
    Write-Host 'Additive baseline settings reset telemetry OFF; deploy only requirements.txt + src Python files; restart; perform one billable model call.'
    Write-Host "RBAC creation explicitly authorized: $AssignModelRole (Cognitive Services OpenAI User only, at supplied account)."
    Write-Host 'COSTS: App Service plan bills continuously, including while idle; existing plan SKU/capacity changes affect every hosted app. Model calls consume quota and incur usage charges. Telemetry ingestion may incur charges only after later activation.'
    Write-Host 'Review regional prices before approval: https://azure.microsoft.com/pricing/details/app-service/linux/ and https://azure.microsoft.com/pricing/details/cognitive-services/openai-service/'
    if ($groupExists) {
        $preview = Invoke-Az -Arguments @('deployment', 'group', 'what-if', '--resource-group', $ResourceGroup, '--subscription', $SubscriptionId,
            '--template-file', $template, '--parameters', "@$parameterPath", '--no-pretty-print', '--result-format', 'ResourceIdOnly') -Operation 'Infrastructure what-if'
        foreach ($change in $preview.changes) { Write-Host "$($change.changeType): $($change.resourceId)" }
    } else {
        Write-Host 'Resource group does not exist: approval also creates it. ARM what-if is deferred until the group exists; the resource changes above are the pre-write preview.'
    }
    if (!$Approve -and !$WhatIfPreference) { throw 'Preview only: rerun with -Approve to authorize the described cloud changes (and -AssignModelRole only if authorized for RBAC).' }
    if (!$PSCmdlet.ShouldProcess($siteId, 'Apply billable infrastructure, merge baseline settings, deploy source, and invoke one model request')) { return }
    if (!$groupExists) {
        Invoke-Az -Arguments @('group', 'create', '--name', $ResourceGroup, '--location', $Location, '--subscription', $SubscriptionId) -NoOutput -Operation 'Resource group creation'
    }
    $result = Invoke-Az -Arguments @('deployment', 'group', 'create', '--name', "agent365-$([DateTime]::UtcNow.ToString('yyyyMMddHHmmss'))",
        '--resource-group', $ResourceGroup, '--subscription', $SubscriptionId, '--mode', 'Incremental',
        '--template-file', $template, '--parameters', "@$parameterPath") -Operation 'Infrastructure deployment'
    $deployment = @{}
    foreach ($entry in $result.properties.outputs.GetEnumerator()) { $deployment[$entry.Key] = $entry.Value.value }
    $deployment.foundryProjectEndpoint = $FoundryProjectEndpoint.TrimEnd('/')
    $deployment.modelDeployment = $ModelDeployment
    $deployment.modelAccessScope = $ModelAccessScope
    $null = Assert-Deployment $deployment
    if ($existingPrincipal -and $deployment.systemAssignedPrincipalId -ne $existingPrincipal) {
        throw 'System-assigned identity changed unexpectedly. Stop and investigate; do not recreate or delete existing role assignments.'
    }
    Set-MergedAppSettings $deployment @{
        FOUNDRY_PROJECT_ENDPOINT = $deployment.foundryProjectEndpoint
        FOUNDRY_MODEL = $ModelDeployment
        ENABLE_AGENT365_TELEMETRY = 'false'
        A365_EXPORT_TO_FOUNDRY = 'true'
        A365_CAPTURE_CONTENT = 'false'
        SCM_DO_BUILD_DURING_DEPLOYMENT = 'true'
        ENABLE_ORYX_BUILD = 'true'
    }
    $hasModelRole = Test-RoleAssignment $deployment.systemAssignedPrincipalId $ModelAccessScope $script:ModelRoleId $modelSubscriptionId
    if (!$hasModelRole) {
        $adminAction = Get-RoleAdminAction $deployment.systemAssignedPrincipalId $ModelAccessScope $script:ModelRoleId $modelSubscriptionId
        if (!$AssignModelRole) {
            Write-Host "Administrator action (then rerun deployment):`n$adminAction"
            throw 'Infrastructure exists with telemetry disabled, but model authorization is missing. No readiness artifact was written and source deployment was stopped.'
        }
        Invoke-Az -Arguments @('role', 'assignment', 'create', '--assignee-object-id', $deployment.systemAssignedPrincipalId, '--assignee-principal-type',
            'ServicePrincipal', '--role', $script:ModelRoleId, '--scope', $ModelAccessScope, '--subscription', $modelSubscriptionId) -NoOutput -Operation 'Explicit account-scoped model role assignment'
        if (!(Test-RoleAssignment $deployment.systemAssignedPrincipalId $ModelAccessScope $script:ModelRoleId $modelSubscriptionId)) {
            throw 'Model role assignment not visible yet. Wait for RBAC propagation and rerun; baseline readiness has not been established.'
        }
    }
    Add-Type -AssemblyName System.IO.Compression
    $archive = [IO.Compression.ZipFile]::Open($zipPath, [IO.Compression.ZipArchiveMode]::Create)
    try {
        $files = @((Get-Item (Join-Path $script:ProjectRoot 'requirements.txt')))
        $files += @(Get-ChildItem (Join-Path $script:ProjectRoot 'src') -Recurse -File -Filter '*.py' |
            Where-Object { $_.FullName -notmatch '[\\/](?:__pycache__|tests?|notebooks|\.local|\.venv|\.git)[\\/]' -and !($_.Attributes -band [IO.FileAttributes]::ReparsePoint) })
        foreach ($file in $files) {
            $relative = [IO.Path]::GetRelativePath($script:ProjectRoot, $file.FullName).Replace('\', '/')
            $null = [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, $file.FullName, $relative, [IO.Compression.CompressionLevel]::Optimal)
        }
    } finally { $archive.Dispose() }
    # Current az webapp deploy uses Entra authentication when SCM basic authentication is disabled.
    Invoke-Az -Arguments @('webapp', 'deploy', '--resource-group', $ResourceGroup, '--name', $WebAppName, '--subscription', $SubscriptionId,
        '--src-path', $zipPath, '--type', 'zip', '--async', 'false', '--track-status', 'true', '--timeout', '1200000', '--restart', 'true') -NoOutput -Operation 'Entra-authenticated Oryx ZIP deployment'
    $null = Assert-Deployment $deployment
    $actualSettings = Get-AppSettings $deployment
    if ($actualSettings['ENABLE_AGENT365_TELEMETRY'] -ne 'false' -or $actualSettings.ContainsKey('AZURE_CLIENT_ID') -or
        $actualSettings['FOUNDRY_PROJECT_ENDPOINT'] -ne $deployment.foundryProjectEndpoint -or $actualSettings['FOUNDRY_MODEL'] -ne $ModelDeployment) {
        throw 'Baseline settings readback failed; no readiness artifact written.'
    }
    Wait-AppHealth $deployment.hostname
    $null = Invoke-BaselineChat $deployment.hostname
    Write-ProtectedJson $deployment ([IO.Path]::GetFullPath($DeploymentFile))
    Write-Host "Baseline verified; both activity exporters dormant. Deployment artifact: $DeploymentFile"
    $deployment | ConvertTo-Json -Depth 5 | Write-Host
    Write-Host 'Next: run the onboarding notebook using this verified deployment artifact.'
} finally {
    foreach ($path in @($parameterPath, $zipPath)) { if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force -WhatIf:$false } }
}
