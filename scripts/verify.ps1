#requires -Version 7.0
<#
.SYNOPSIS
Send one permitted, billable model request and distinguish readbacks from telemetry delivery.
.DESCRIPTION
Network-allowlisted callers only; the conversation ID is correlation metadata, not authentication.
Optional SettingsFile invokes administrative read-only Graph/Foundry validation, not portal checks.
This script performs no Azure/Graph configuration writes.
#>
[CmdletBinding()]
param(
    [string]$DeploymentFile = (Join-Path (Split-Path $PSScriptRoot -Parent) '.local\deployment.json'),
    [string]$SettingsFile,
    [switch]$Agent365Only,
    [ValidateLength(1, 2000)][string]$Message = 'Explain what this sample agent does in one short sentence.'
)
. (Join-Path $PSScriptRoot 'common.ps1')
$deployment = Read-JsonFile ([IO.Path]::GetFullPath($DeploymentFile))
$null = Assert-Deployment $deployment
$current = Get-AppSettings $deployment
if ($current.ContainsKey('AZURE_CLIENT_ID') -or $current['FOUNDRY_PROJECT_ENDPOINT'] -ne $deployment.foundryProjectEndpoint -or
    $current['FOUNDRY_MODEL'] -ne $deployment.modelDeployment) { throw 'Live model configuration does not match the deployment artifact or contains global AZURE_CLIENT_ID.' }
$artifact = $null
if ($SettingsFile) {
    $SettingsFile = [IO.Path]::GetFullPath($SettingsFile)
    $artifact = Read-JsonFile $SettingsFile
    if ($artifact['schemaVersion'] -ne 1 -or $artifact['deployment'] -isnot [hashtable]) { throw 'Invalid runtime settings artifact.' }
    foreach ($key in $deployment.Keys) {
        if ($artifact.deployment[$key] -cne $deployment[$key]) { throw "Settings artifact belongs to another deployment ($key differs)." }
    }
    if ($Agent365Only) {
        if ($null -ne $artifact['foundry'] -or $artifact.settings.A365_EXPORT_TO_FOUNDRY -cne 'false') { throw '-Agent365Only does not match this artifact.' }
    } elseif ($null -eq $artifact['foundry'] -or $artifact.settings.A365_EXPORT_TO_FOUNDRY -cne 'true') {
        throw 'Default verification requires the dual-destination artifact; explicitly select -Agent365Only for that opt-out.'
    }
    Invoke-RuntimeReadback $SettingsFile
    Write-Host 'Agent 365 registration/setup readback: MATCHED (not delivery proof).'
    if (!$Agent365Only) { Write-Host 'Foundry External Agent/project/child telemetry binding readback: MATCHED (not ingestion or portal proof).' }
    foreach ($name in $artifact.settings.Keys) {
        if ($current[$name] -cne $artifact.settings[$name]) { throw "Live runtime setting differs from validated artifact: $name. Run enable-observability.ps1." }
    }
} else {
    if ($Agent365Only) { throw '-Agent365Only requires -SettingsFile so the explicit opt-out can be checked.' }
    Write-Host 'Agent 365 and Foundry registration readback: NOT CHECKED (supply -SettingsFile after onboarding).'
}
Wait-AppHealth $deployment.hostname
$response = Invoke-BaselineChat $deployment.hostname $Message
if ($current['ENABLE_AGENT365_TELEMETRY'] -ne 'true') {
    Write-Host 'Telemetry: DISABLED. Baseline deployment/model succeeded; neither activity exporter is expected to run.'
    return
}
if ($response.PSObject.Properties.Name -notcontains 'trace_id' -or !$response.trace_id -or $response.trace_id -notmatch '^[0-9a-f]{32}$' -or $response.trace_id -eq ('0' * 32)) {
    Write-Host 'Trace correlation: MISSING/INVALID despite enabled telemetry. Model succeeded, but instrumentation/telemetry configuration must be investigated.'
}
Write-Host 'Runtime Observability app-only token: NOT PROVEN by HTTP success; inspect sanitized operational diagnostics for acquisition/renewal failures.'
Write-Host 'Agent 365 export acceptance: NOT PROVEN; inspect exporter diagnostics for rejection, partial-success, and transport errors. A flush is not acceptance.'
Write-Host 'Agent 365 Activity portal: PENDING MANUAL CONFIRMATION; find the matching child, conversation ID, and trace after ingestion/indexing delay.'
if ($current['A365_EXPORT_TO_FOUNDRY'] -eq 'true') {
    if ($artifact -and $artifact['foundry']) {
        Write-Host "Foundry target: $($artifact.foundry.projectEndpoint)"
        Write-Host "External Agent: $($artifact.foundry.externalAgentName); telemetry ID: $($artifact.foundry.otelAgentId)"
    }
    Write-Host 'Azure Monitor ingestion: NOT PROVEN; inspect SDK diagnostics, then find this trace ID in the project-linked Application Insights resource.'
    Write-Host 'Foundry portal: PENDING MANUAL CONFIRMATION. Open Build > Agents > registered external agent > Traces (or current equivalent); match the SAME trace ID and child gen_ai.agent.id.'
    Write-Host 'Raw Application Insights ingestion alone does not establish Foundry External Agent visibility. Check project linkage, otel_agent_id, Reader/Monitoring Reader access, and indexing delay.'
} else {
    Write-Host 'Foundry External Agent observability: NOT CONFIGURED (explicit Agent 365-only mode); no dual-export success is claimed.'
}
