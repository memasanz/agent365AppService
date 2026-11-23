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
            if ($Arguments[0] -eq 'role') {{ return $script:FakeAssignments }}
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
