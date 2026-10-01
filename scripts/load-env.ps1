# scripts/load-env.ps1 — load .env into the CURRENT PowerShell session.
#
# Why this exists: several operator commands run on the HOST and need the same values the
# containers get from compose — the gateway CLI needs DATABASE_URL, and pytest (including
# the `odoo`-marked tests) needs DATABASE_URL plus the Odoo settings.
#
# Usage — dot-source it, so the variables land in YOUR session. Running it as a child
# process throws the values away when that process exits:
#
#     . .\scripts\load-env.ps1                 # uses ./.env
#     . .\scripts\load-env.ps1 -Path infra\.env
#     . .\scripts\load-env.ps1 -Quiet
#
# Then, for example:
#
#     python -m moni_gateway.cli list-odoo-users
#     pytest -m odoo
#
# It sets only what .env defines; it never clears or overrides anything else, and it
# prints variable names but never values (secrets must not reach the console — §3.11).
#
# HOST vs CONTAINER ADDRESSES — read this before wondering why a URL looks different.
# `.env` is read by BOTH `docker compose` and this script, but the two need different
# addresses for the same service. Inside the compose network a service is reached by its
# service name; from the host that name does not resolve and the published loopback port
# must be used instead. The variables where this matters are rewritten here, for the
# session only — the file on disk keeps the container-facing value:
#
#     variable        .env (containers)      rewritten here (host commands)
#     LANGFUSE_HOST   http://langfuse:3000   http://127.0.0.1:${LANGFUSE_PORT}
#
# The rewrite is announced in the output ("rewrote for host access: ...") so it is never
# silent. If a host command reaches the wrong address, check that line first.

[CmdletBinding()]
param(
    [string]$Path = '.env',
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $Path)) {
    Write-Error ("env file not found: {0}. Create it first:  Copy-Item .env.example .env" -f $Path)
    return
}

# Read as UTF-8 without a BOM. A BOM would end up glued to the first variable's name, and
# PowerShell 5.1's Get-Content can mangle the encoding.
$content = [System.IO.File]::ReadAllText((Resolve-Path -LiteralPath $Path).Path, [System.Text.UTF8Encoding]::new($false))

$loaded = New-Object System.Collections.Generic.List[string]
$skipped = 0

foreach ($rawLine in $content -split "`r?`n") {
    $line = $rawLine.Trim()

    # Skip blanks and comments.
    if (-not $line -or $line.StartsWith('#')) { continue }

    $separator = $line.IndexOf('=')
    if ($separator -lt 1) { $skipped++; continue }

    $name = $line.Substring(0, $separator).Trim()
    $value = $line.Substring($separator + 1).Trim()

    # Strip one layer of matching quotes, if present.
    if ($value.Length -ge 2) {
        $first = $value[0]
        $last = $value[$value.Length - 1]
        if (($first -eq '"' -and $last -eq '"') -or ($first -eq "'" -and $last -eq "'")) {
            $value = $value.Substring(1, $value.Length - 2)
        }
    }

    # A valid env var name: letters, digits and underscores, not starting with a digit.
    if ($name -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { $skipped++; continue }

    Set-Item -Path ("Env:{0}" -f $name) -Value $value
    $loaded.Add($name)
}

# Host-run commands cannot resolve compose service names, but .env carries the
# CONTAINER-facing hostnames (that same file feeds `docker compose`). Rewrite the ones that
# have a published loopback port so a host process reaches the service instead of failing
# with a DNS error. Only applied when the variable is actually set, and the container value
# is never mutated on disk.
#
# PowerShell 5.1 has no null-coalescing operator, hence the explicit fallback below.
$langfusePort = if ($env:LANGFUSE_PORT) { $env:LANGFUSE_PORT } else { '3001' }
$hostOverrides = @{
    'LANGFUSE_HOST' = ('http://127.0.0.1:{0}' -f $langfusePort)
}
$overridden = New-Object System.Collections.Generic.List[string]
foreach ($name in $hostOverrides.Keys) {
    if ($loaded -contains $name) {
        Set-Item -Path ("Env:{0}" -f $name) -Value $hostOverrides[$name]
        $overridden.Add($name)
    }
}

if (-not $Quiet) {
    Write-Host ("loaded {0} variables from {1}" -f $loaded.Count, $Path) -ForegroundColor Green
    if ($skipped -gt 0) {
        Write-Host ("  ({0} line(s) skipped: not NAME=value)" -f $skipped) -ForegroundColor DarkGray
    }
    # Names only: values are secrets.
    Write-Host ("  " + (($loaded | Sort-Object) -join ', ')) -ForegroundColor DarkGray

    $hasDatabaseUrl = $loaded -contains 'DATABASE_URL'
    if ($hasDatabaseUrl) {
        Write-Host '  DATABASE_URL is set; host-run commands can reach the database.' -ForegroundColor DarkGray
    }
    else {
        Write-Warning ('DATABASE_URL is NOT set in {0}; host-run commands that need the database will fail.' -f $Path)
    }

    if ($overridden.Count -gt 0) {
        Write-Host ("  rewrote for host access: {0}" -f (($overridden | Sort-Object) -join ', ')) -ForegroundColor DarkGray
    }
}
