<#
.SYNOPSIS
    Install, update, remove or inspect the pcwall Windows service through NSSM.

.DESCRIPTION
    Registers "python -m pcwall server" as a Windows service named pcwall using
    NSSM (the Non-Sucking Service Manager), so the wall starts with the machine,
    restarts whenever it exits and writes its console output to logs\ under the
    repository. Run from an elevated PowerShell in the checkout:

        powershell -ExecutionPolicy Bypass -File scripts\install_service.ps1
        powershell -ExecutionPolicy Bypass -File scripts\install_service.ps1 -Action uninstall
        powershell -ExecutionPolicy Bypass -File scripts\install_service.ps1 -DryRun

    Install NSSM first with "winget install NSSM.NSSM" or from https://nssm.cc/.
    Variables from <Root>\.env are handed to the service as its environment, so
    PCWALL_PORT, PCWALL_KEY and the rest live there, never in this script.

.PARAMETER Action
    install (default), uninstall, restart or status.

.PARAMETER Root
    Repository root. Defaults to the parent of the scripts\ folder.

.PARAMETER Python
    python.exe that runs the service. Defaults to <Root>\.venv\Scripts\python.exe
    when it exists, otherwise the first python.exe on PATH.

.PARAMETER Nssm
    nssm.exe to use. Defaults to the first nssm.exe on PATH.

.PARAMETER ServiceName
    Windows service name. Defaults to pcwall.

.PARAMETER DryRun
    Print every nssm command instead of running it. Needs neither NSSM nor an
    elevated shell, and shows values read from .env as <set>, never in clear.
#>
[CmdletBinding()]
param(
    [ValidateSet('install', 'uninstall', 'restart', 'status')]
    [string]$Action = 'install',
    [string]$Root = '',
    [string]$Python = '',
    [string]$Nssm = '',
    [string]$ServiceName = 'pcwall',
    [switch]$DryRun
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

function Format-Argument([string]$Value) {
    # Paths and multi-word values are shown quoted so dry-run output pastes back cleanly.
    if ($Value -match '[\s"\\/]') { return '"' + ($Value -replace '"', '\"') + '"' }
    return $Value
}

function Invoke-Nssm([string[]]$Arguments) {
    $shown = ($Arguments | ForEach-Object { Format-Argument $_ }) -join ' '
    if ($DryRun) {
        Write-Output "nssm $shown"
        return
    }
    Write-Output "+ nssm $shown"
    & $script:NssmExe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "nssm $($Arguments[0]) failed with exit code $LASTEXITCODE" }
}

function Invoke-NssmEnvironment([string[]]$Pairs, [string[]]$Shown) {
    if ($DryRun) {
        Write-Output ("nssm set $ServiceName AppEnvironmentExtra " + ($Shown -join ' '))
        return
    }
    Write-Output ("+ nssm set $ServiceName AppEnvironmentExtra " + ($Shown -join ' '))
    & $script:NssmExe set $ServiceName AppEnvironmentExtra @Pairs
    if ($LASTEXITCODE -ne 0) { throw "nssm set AppEnvironmentExtra failed with exit code $LASTEXITCODE" }
}

function Test-Elevated {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-ServiceOrNull([string]$Name) {
    try { return Get-Service -Name $Name -ErrorAction Stop } catch { return $null }
}

function Read-EnvFile([string]$Path) {
    # KEY=VALUE lines; blanks, comments and malformed lines are skipped. Quotes around
    # the value are removed. Returns an ordered list of @{ Name; Value } entries.
    $entries = @()
    if (-not (Test-Path -LiteralPath $Path)) { return $entries }
    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if ($trimmed -eq '' -or $trimmed.StartsWith('#')) { continue }
        $index = $trimmed.IndexOf('=')
        if ($index -lt 1) { continue }
        $name = $trimmed.Substring(0, $index).Trim()
        if ($name -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { continue }
        $value = $trimmed.Substring($index + 1).Trim()
        if ($value.Length -ge 2 -and (($value[0] -eq '"' -and $value[-1] -eq '"') -or ($value[0] -eq "'" -and $value[-1] -eq "'"))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        $entries += @{ Name = $name; Value = $value }
    }
    return $entries
}

function Resolve-Root {
    if ([string]::IsNullOrEmpty($Root)) { $Root = Split-Path -Parent $PSScriptRoot }
    if (-not (Test-Path -LiteralPath $Root -PathType Container)) { throw "repository root does not exist: $Root" }
    return (Resolve-Path -LiteralPath $Root).Path.TrimEnd('\')
}

function Resolve-Python([string]$RootPath) {
    if (-not [string]::IsNullOrEmpty($Python)) { return $Python }
    $venv = Join-Path $RootPath '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venv) { return $venv }
    $found = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -ne $found) { return $found.Source }
    if ($DryRun) { return 'python.exe' }
    throw 'python.exe was not found; create .venv or pass -Python'
}

function Resolve-Nssm {
    if (-not [string]::IsNullOrEmpty($Nssm)) { return $Nssm }
    $found = Get-Command nssm.exe -ErrorAction SilentlyContinue
    if ($null -ne $found) { return $found.Source }
    if ($DryRun) { return 'nssm.exe' }
    throw 'nssm.exe was not found; install it with "winget install NSSM.NSSM" or pass -Nssm'
}

function Install-WallService([string]$RootPath, [string]$PythonExe) {
    $logs = Join-Path $RootPath 'logs'
    $data = Join-Path $RootPath 'data'
    $stdout = Join-Path $logs 'pcwall.log'
    $stderr = Join-Path $logs 'pcwall.err.log'
    $envFile = Join-Path $RootPath '.env'

    foreach ($directory in @($logs, $data)) {
        if ($DryRun) { Write-Output "mkdir $directory" }
        else { New-Item -ItemType Directory -Force -Path $directory | Out-Null }
    }

    # A dry run always shows the fresh-install sequence, whatever this host has installed.
    $existing = $null
    if (-not $DryRun) { $existing = Get-ServiceOrNull $ServiceName }
    if ($null -eq $existing) {
        Invoke-Nssm @('install', $ServiceName, $PythonExe, '-m', 'pcwall', 'server')
    } else {
        Write-Output "service $ServiceName already exists; updating its settings"
        Invoke-Nssm @('set', $ServiceName, 'Application', $PythonExe)
    }
    Invoke-Nssm @('set', $ServiceName, 'AppParameters', '-m pcwall server')
    Invoke-Nssm @('set', $ServiceName, 'AppDirectory', $RootPath)
    Invoke-Nssm @('set', $ServiceName, 'DisplayName', 'pcwall family wall')
    Invoke-Nssm @('set', $ServiceName, 'Description', 'Self-hosted family wall display (python -m pcwall server).')
    Invoke-Nssm @('set', $ServiceName, 'Start', 'SERVICE_AUTO_START')
    Invoke-Nssm @('set', $ServiceName, 'AppStdout', $stdout)
    Invoke-Nssm @('set', $ServiceName, 'AppStderr', $stderr)
    Invoke-Nssm @('set', $ServiceName, 'AppStdoutCreationDisposition', '4')
    Invoke-Nssm @('set', $ServiceName, 'AppStderrCreationDisposition', '4')
    Invoke-Nssm @('set', $ServiceName, 'AppRotateFiles', '1')
    Invoke-Nssm @('set', $ServiceName, 'AppRotateOnline', '1')
    Invoke-Nssm @('set', $ServiceName, 'AppRotateBytes', '10485760')
    Invoke-Nssm @('set', $ServiceName, 'AppExit', 'Default', 'Restart')
    Invoke-Nssm @('set', $ServiceName, 'AppRestartDelay', '5000')
    Invoke-Nssm @('set', $ServiceName, 'AppStopMethodConsole', '5000')

    $pairs = @('PYTHONUNBUFFERED=1')
    $shown = @('PYTHONUNBUFFERED=1')
    foreach ($entry in Read-EnvFile $envFile) {
        $pairs += "$($entry.Name)=$($entry.Value)"
        $shown += "$($entry.Name)=<set>"
    }
    Invoke-NssmEnvironment $pairs $shown

    if ($null -ne $existing -and $existing.Status -eq 'Running') {
        Invoke-Nssm @('restart', $ServiceName)
    } else {
        Invoke-Nssm @('start', $ServiceName)
    }

    if ($DryRun) {
        Write-Output 'dry run: nothing was installed or started.'
    } else {
        Write-Output "$ServiceName is installed and starting from $RootPath."
        Write-Output "Logs: $stdout and $stderr. Status: nssm status $ServiceName"
    }
}

function Uninstall-WallService {
    if ($null -eq (Get-ServiceOrNull $ServiceName) -and -not $DryRun) {
        Write-Output "service $ServiceName is not installed."
        return
    }
    try { Invoke-Nssm @('stop', $ServiceName) } catch { Write-Output "stop skipped: $($_.Exception.Message)" }
    Invoke-Nssm @('remove', $ServiceName, 'confirm')
    if (-not $DryRun) { Write-Output "$ServiceName removed." }
}

try {
    $rootPath = Resolve-Root
    $script:NssmExe = Resolve-Nssm
    if (-not $DryRun -and $Action -ne 'status' -and -not (Test-Elevated)) {
        throw 'run from an elevated PowerShell (Run as administrator), or add -DryRun to preview'
    }
    switch ($Action) {
        'install'   { Install-WallService $rootPath (Resolve-Python $rootPath) }
        'uninstall' { Uninstall-WallService }
        'restart'   { Invoke-Nssm @('restart', $ServiceName) }
        'status'    {
            Invoke-Nssm @('status', $ServiceName)
            Write-Output "Logs: $(Join-Path $rootPath 'logs\pcwall.log') and $(Join-Path $rootPath 'logs\pcwall.err.log')"
        }
    }
    exit 0
} catch {
    $host.UI.WriteErrorLine("install_service.ps1: $($_.Exception.Message)")
    exit 1
}
