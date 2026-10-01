<#
.SYNOPSIS
    Keep the vLLM SSH tunnel to the GPU host alive, and say so when it dies.

.DESCRIPTION
    The vLLM endpoint the stack uses lives on a remote GPU host and is reached through an SSH
    local-forward: `127.0.0.1:8001` on this machine -> `127.0.0.1:8001` on that host. A bare
    `ssh -N -L ...` dies whenever the network hiccups, and when it does **every symptom looks
    like a stack failure**: the gateway reports `could not reach the model`, the agent answers
    from its no-evidence path, and nothing in the logs says "the tunnel is gone". It died four
    times in one week that way.

    This script supervises it: it reconnects in a loop with a fixed backoff and writes a
    timestamped line for every connection, disconnection and probe, so the next death is a line
    in a log rather than an investigation.

    It also passes two `ssh` options that a bare invocation lacks:
      * `ExitOnForwardFailure=yes` — fail fast instead of connecting with no forward in place,
        which is the state that looks like "the model is down" but is really "there is no tunnel";
      * `ServerAliveInterval`/`ServerAliveCountMax` — detect a dead link instead of hanging
        forever on a half-open connection.

.PARAMETER Target
    `user@host` of the GPU host. Defaults to `$env:MONI_GPU_SSH`, so the hostname can live in the
    environment rather than in this repository (there is no safe default: it is deployment data).

.PARAMETER Once
    Make a single attempt and exit. For a supervisor (systemd, Task Scheduler, a parent script)
    that owns the retry policy itself.

.EXAMPLE
    $env:MONI_GPU_SSH = 'ubuntu@203.0.113.10'; .\scripts\tunnel-vllm.ps1

.EXAMPLE
    .\scripts\tunnel-vllm.ps1 -Target ubuntu@203.0.113.10 -BackoffSeconds 5
#>
[CmdletBinding()]
param(
    [string]$Target = $env:MONI_GPU_SSH,
    [int]$LocalPort = 8001,
    [string]$RemoteHost = '127.0.0.1',
    [int]$RemotePort = 8001,
    [int]$BackoffSeconds = 5,
    [string]$LogPath,
    [switch]$Once
)

$ErrorActionPreference = 'Stop'

# `$PSScriptRoot` is EMPTY while `param()` defaults are being bound under PowerShell 5.1 — it is
# only populated once the script body runs. Using it as a default therefore produced
# "Join-Path : Cannot bind argument to parameter 'Path' because it is an empty string" before the
# script did anything at all. Resolved here instead, with a fallback for hosts that set neither.
$scriptDir = $PSScriptRoot
if (-not $scriptDir) {
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
}
if (-not $LogPath) {
    $LogPath = Join-Path $scriptDir '..\logs\tunnel-vllm.log'
}

function Write-Log {
    param([string]$Message, [string]$Level = 'INFO')

    $line = '{0} [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
    switch ($Level) {
        'WARN' { Write-Host $line -ForegroundColor Yellow }
        'ERROR' { Write-Host $line -ForegroundColor Red }
        default { Write-Host $line }
    }
    if ($script:LogFile) {
        # AppendAllText with an explicit no-BOM encoding: PowerShell 5.1's `Add-Content -Encoding
        # utf8` writes a BOM, which is how a BOM once reached pyproject.toml and broke the build.
        [System.IO.File]::AppendAllText(
            $script:LogFile, $line + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
    }
}

if (-not $Target) {
    Write-Log 'no SSH target. Pass -Target user@host, or set $env:MONI_GPU_SSH.' 'ERROR'
    Write-Log '  e.g.  $env:MONI_GPU_SSH = ''ubuntu@<gpu-host>''; .\scripts\tunnel-vllm.ps1'
    exit 2
}

if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) {
    Write-Log 'ssh is not on PATH; install the OpenSSH client.' 'ERROR'
    exit 2
}

# Resolve the log path once, so a relative -LogPath is not re-resolved from a different location.
$script:LogFile = $null
if ($LogPath) {
    try {
        $resolved = [System.IO.Path]::GetFullPath($LogPath)
        [System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($resolved)) | Out-Null
        $script:LogFile = $resolved
    }
    catch {
        Write-Log "could not prepare the log file '$LogPath' ($($_.Exception.Message)); logging to console only" 'WARN'
        $script:LogFile = $null
    }
}

# Somebody else's tunnel already holding the port would make every new one fail with "address
# already in use" — worth saying out loud, because the symptom otherwise is a healthy-looking
# port that this script did not create.
$existing = Get-NetTCPConnection -State Listen -LocalPort $LocalPort -ErrorAction SilentlyContinue
if ($existing) {
    Write-Log "port $LocalPort is already listening (pid $($existing[0].OwningProcess)) - is another tunnel running?" 'WARN'
}

$forward = '{0}:{1}:{2}' -f $LocalPort, $RemoteHost, $RemotePort
Write-Log "supervising: ssh -N -L $forward $Target (backoff ${BackoffSeconds}s)"
Write-Log "log file: $($script:LogFile)"

$attempt = 0
try {
    while ($true) {
        $attempt++
        Write-Log "attempt ${attempt}: connecting"

        $started = Get-Date
        & ssh -N `
            -o ExitOnForwardFailure=yes `
            -o BatchMode=yes `
            -o ServerAliveInterval=15 `
            -o ServerAliveCountMax=3 `
            -o ConnectTimeout=10 `
            -L $forward $Target
        $code = $LASTEXITCODE
        $held = [int]((Get-Date) - $started).TotalSeconds

        Write-Log "tunnel exited after ${held}s (ssh exit code $code)" 'WARN'

        if ($Once) {
            break
        }
        Write-Log "reconnecting in ${BackoffSeconds}s"
        Start-Sleep -Seconds $BackoffSeconds
    }
}
catch [System.Management.Automation.PipelineStoppedException] {
    # Ctrl+C: stop supervising rather than reconnecting forever while the operator is trying to quit.
    Write-Log "stopped by the operator after $attempt attempt(s)"
}
finally {
    Write-Log 'supervisor exiting'
}
