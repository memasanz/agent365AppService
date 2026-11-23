#requires -Version 7.0
<#
.SYNOPSIS
Validate live identity/registration relationships, then explicitly enable dormant exporters.
.DESCRIPTION
-Approve authorizes settings/restart only. -AssignMonitoringRole separately authorizes
the system-assigned identity's Monitoring Metrics Publisher role on the linked AppInsights.
Agent365Only must match an artifact deliberately generated in that mode.
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium')]
param(
    [string]$DeploymentFile = (Join-Path (Split-Path $PSScriptRoot -Parent) '.local\deployment.json'),
    [string]$SettingsFile = (Join-Path (Split-Path $PSScriptRoot -Parent) '.local\runtime-settings.json'),
    [switch]$Agent365Only,
    [switch]$AssignMonitoringRole,
    [switch]$Approve
)
. (Join-Path $PSScriptRoot 'common.ps1')
$DeploymentFile = [IO.Path]::GetFullPath($DeploymentFile)
$SettingsFile = [IO.Path]::GetFullPath($SettingsFile)
$deployment = Read-JsonFile $DeploymentFile
$artifact = Read-JsonFile $SettingsFile
$null = Assert-Deployment $deployment
if ($artifact['schemaVersion'] -ne 1 -or $artifact['deployment'] -isnot [hashtable] -or $artifact['settings'] -isnot [hashtable]) {
    throw 'Unsupported or incomplete runtime settings artifact; regenerate it through the notebook.'
}
foreach ($key in $deployment.Keys) {
    if ($artifact.deployment[$key] -cne $deployment[$key]) { throw "Runtime artifact belongs to another deployment ($key differs)." }
}
foreach ($name in @('blueprintObjectId', 'blueprintClientId', 'blueprintPrincipalId', 'childAgentId', 'provisioningClientId')) { Assert-Guid $artifact[$name] $name }
if (!$artifact['registrationId']) { throw 'Agent 365 registration ID is required.' }
$settings = $artifact.settings
$required = @('ENABLE_AGENT365_TELEMETRY', 'ENTRA_TENANT_ID', 'ENTRA_BLUEPRINT_CLIENT_ID', 'ENTRA_AGENT_IDENTITY_OBJECT_ID',
    'AZURE_MANAGED_IDENTITY_CLIENT_ID', 'AGENT_IDENTITY_NAME', 'A365_SPONSOR_USER_ID', 'A365_SPONSOR_DISPLAY_NAME', 'A365_SPONSOR_UPN',
    'A365_CAPTURE_CONTENT', 'A365_EXPORT_TO_FOUNDRY')
$allowed = $required + @('APPLICATIONINSIGHTS_CONNECTION_STRING', 'A365_AZURE_MONITOR_USE_ENTRA')
foreach ($key in $settings.Keys) {
    if ($key -notin $allowed -or $settings[$key] -isnot [string]) { throw 'Runtime artifact contains an unexpected setting or non-string value. No payload was logged.' }
}
foreach ($key in $required) { if ([string]::IsNullOrWhiteSpace($settings[$key])) { throw "Runtime artifact is missing $key." } }
if ($settings.ENABLE_AGENT365_TELEMETRY -cne 'true' -or $settings.A365_CAPTURE_CONTENT -cne 'false' -or
    $settings.ENTRA_TENANT_ID -ne $deployment.tenantId -or $settings.ENTRA_BLUEPRINT_CLIENT_ID -ne $artifact.blueprintClientId -or
    $settings.ENTRA_AGENT_IDENTITY_OBJECT_ID -ne $artifact.childAgentId -or
    $settings.AZURE_MANAGED_IDENTITY_CLIENT_ID -ne $deployment.managedIdentityClientId) {
    throw 'Runtime activation flags/identity bindings do not match the deployment and onboarding artifacts.'
}
Assert-Guid $settings.A365_SPONSOR_USER_ID 'sponsor user ID'
$insightsScope = $null
$monitoringSubscriptionId = $null
$needsMonitoringRole = $false
if ($Agent365Only) {
    if ($null -ne $artifact['foundry'] -or $settings.A365_EXPORT_TO_FOUNDRY -cne 'false') {
        throw '-Agent365Only requires an explicitly Agent 365-only artifact (foundry=null and A365_EXPORT_TO_FOUNDRY=false); no silent downgrade is permitted.'
    }
    if ($AssignMonitoringRole) { throw '-AssignMonitoringRole is not applicable in Agent 365-only mode.' }
} else {
    $foundry = $artifact['foundry']
    if ($foundry -isnot [hashtable] -or $settings.A365_EXPORT_TO_FOUNDRY -cne 'true' -or
        $settings['A365_AZURE_MONITOR_USE_ENTRA'] -cne 'true' -or
        [string]::IsNullOrWhiteSpace($settings['APPLICATIONINSIGHTS_CONNECTION_STRING'])) {
        throw 'Default activation requires the dual-destination artifact, linked Application Insights connection string, and explicit Entra ingestion. Regenerate through the notebook; do not downgrade silently.'
    }
    foreach ($key in @('projectEndpoint', 'externalAgentName', 'registrationId', 'version', 'otelAgentId', 'applicationInsightsResourceId')) {
        if (!$foundry[$key]) { throw "Foundry artifact is missing $key." }
    }
    Assert-ProjectEndpoint $foundry.projectEndpoint
    if ($foundry.otelAgentId -ne $artifact.childAgentId -or $foundry['connectionVerified'] -isnot [bool] -or !$foundry.connectionVerified) {
        throw 'Foundry must bind to the same child agent GUID and have a verified project-linked Application Insights connection.'
    }
    $insightsScope = $foundry.applicationInsightsResourceId
    if ($insightsScope -notmatch '^/subscriptions/([0-9a-f-]{36})/resourceGroups/[^/]+/providers/Microsoft.Insights/components/[^/]+$') {
        throw 'Foundry-linked Application Insights scope must be an exact components resource ID.'
    }
    $monitoringSubscriptionId = $Matches[1]
    Assert-AzureContext $deployment.tenantId $monitoringSubscriptionId
    $null = Invoke-Az -Arguments @('resource', 'show', '--ids', $insightsScope, '--api-version', '2020-02-02', '--subscription', $monitoringSubscriptionId) -Operation 'Linked Application Insights readback'
    $needsMonitoringRole = !(Test-RoleAssignment $deployment.systemAssignedPrincipalId $insightsScope $script:MonitoringRoleId $monitoringSubscriptionId)
}
$current = Get-AppSettings $deployment
if ($current.ContainsKey('AZURE_CLIENT_ID')) { throw 'Remove the global AZURE_CLIENT_ID setting explicitly; it must not redirect hosted model access to the telemetry UAMI.' }
if ($current['FOUNDRY_PROJECT_ENDPOINT'] -ne $deployment.foundryProjectEndpoint -or $current['FOUNDRY_MODEL'] -ne $deployment.modelDeployment) {
    throw 'Live model settings do not match the intended baseline deployment.'
}
Write-Host 'Performing read-only administrative Graph and Foundry relationship validation (interactive sign-in may open a browser).'
Invoke-RuntimeReadback $SettingsFile
Write-Host 'Readback matched deployment, attached identities, blueprint trust, child, sponsor, Agent 365 registration, and selected Foundry registration/link.'
Write-Host 'PREVIEW: merge only the following setting names; existing unrelated settings are preserved. Values, sponsor profiles, and connection strings are suppressed.'
foreach ($name in ($settings.Keys | Sort-Object)) {
    $change = if ($current.ContainsKey($name)) { if ($current[$name] -ceq $settings[$name]) { 'unchanged' } else { 'update' } } else { 'add' }
    Write-Host "  $name ($change)"
}
Write-Host "Enable Agent 365 export; Foundry export: $(!$Agent365Only); capture content: false; restart App Service."
Write-Host 'Azure Monitor ingestion/retention may incur charges: https://azure.microsoft.com/pricing/details/monitor/'
if ($needsMonitoringRole) {
    $adminAction = Get-RoleAdminAction $deployment.systemAssignedPrincipalId $insightsScope $script:MonitoringRoleId $monitoringSubscriptionId
    Write-Host "Missing ingestion role. Precise administrator action:`n$adminAction"
    if (!$AssignMonitoringRole) { throw 'No settings changed. Have an administrator assign this role, or explicitly authorize it with -AssignMonitoringRole, then rerun.' }
    Write-Host 'Explicit -AssignMonitoringRole authorizes only the displayed system-identity ingestion role at the linked resource.'
}
if (!$Approve -and !$WhatIfPreference) { throw 'Preview only: rerun with -Approve to authorize this settings update and restart.' }
if (!$PSCmdlet.ShouldProcess($deployment.webAppResourceId, 'Merge validated telemetry settings, optionally assign the explicitly authorized ingestion role, and restart')) { return }
if ($needsMonitoringRole) {
    Invoke-Az -Arguments @('role', 'assignment', 'create', '--assignee-object-id', $deployment.systemAssignedPrincipalId, '--assignee-principal-type',
        'ServicePrincipal', '--role', $script:MonitoringRoleId, '--scope', $insightsScope, '--subscription', $monitoringSubscriptionId) -NoOutput -Operation 'Explicit linked-resource ingestion role assignment'
    if (!(Test-RoleAssignment $deployment.systemAssignedPrincipalId $insightsScope $script:MonitoringRoleId $monitoringSubscriptionId)) {
        throw 'Ingestion role is not yet visible; wait for RBAC propagation and rerun. No settings changed.'
    }
}
$null = Assert-Deployment $deployment
Set-MergedAppSettings $deployment $settings
$readback = Get-AppSettings $deployment
foreach ($name in $settings.Keys) { if ($readback[$name] -cne $settings[$name]) { throw "App setting readback failed for $name; payload suppressed." } }
foreach ($name in $current.Keys) {
    if (!$settings.ContainsKey($name) -and $readback[$name] -cne $current[$name]) { throw 'An unrelated setting changed during activation; investigate concurrent updates.' }
}
Invoke-Az -Arguments @('webapp', 'restart', '--resource-group', $deployment.resourceGroup, '--name', $deployment.webAppName,
    '--subscription', $deployment.subscriptionId) -NoOutput -Operation 'App Service restart'
Wait-AppHealth $deployment.hostname
Write-Host 'Configuration enabled and health verified. This is not proof of token acquisition, accepted export, or portal visibility.'
$modeFlag = if ($Agent365Only) { ' -Agent365Only' } else { '' }
Write-Host "Next: .\scripts\verify.ps1 -DeploymentFile '$DeploymentFile' -SettingsFile '$SettingsFile'$modeFlag"
