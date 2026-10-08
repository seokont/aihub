# Mutation sweep for task 2a (docs/handoff/2a-external-primary.md).
#
# One mutation per guard — the mutation the handoff names for it; each is applied alone, the named
# test(s) are run and MUST fail, and the file is restored from **pristine bytes held in memory** for
# the whole run and verified by hash. Nothing is left mutated: a run that cannot restore stops
# immediately.
#
# Why this is a script rather than a one-off command: a mutation is only evidence if the file it was
# applied to is byte-identical afterwards. The first attempt at this sweep reported two guards as
# uncaught and three as unapplied — all five were bugs in the harness, not in the tests. Pristine
# bytes in memory, a hash check on every restore and a non-zero exit are what make that class of
# mistake impossible to mistake for a result.
#
# Usage:  & '<repo>\scripts\mutation_sweep_2a.ps1'
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $root '.venv\Scripts\python.exe'

if (-not (Test-Path $py)) {
    Write-Output "no interpreter at $py - run from a synced checkout (uv sync --group dev)"
    exit 2
}

$policy = 'router/src/moni_router/policy.py'
$chat = 'router/src/moni_router/chat.py'

function Hash([string]$path) { (Get-FileHash -Algorithm SHA256 -Path $path).Hash }

# Pristine bytes, captured before anything is touched. Restoring means writing these back, so the
# restore cannot depend on line-ending conversion, on a backup file surviving, or on git state.
$pristine = @{}
$baseline = @{}
foreach ($f in @($policy, $chat)) {
    $pristine[$f] = [System.IO.File]::ReadAllBytes((Join-Path $root $f))
    $baseline[$f] = Hash (Join-Path $root $f)
}

function Get-Text([string]$target) {
    return ([System.IO.File]::ReadAllText((Join-Path $root $target))) -replace "`r`n", "`n"
}

function Restore-File([string]$target) {
    [System.IO.File]::WriteAllBytes((Join-Path $root $target), $pristine[$target])
    return (Hash (Join-Path $root $target)) -eq $baseline[$target]
}

$mutations = @(
    @{
        Name = '1 - drop the level-A refusal'
        File = $policy
        Old  = "    if level != `"A`" or destination != `"external`" or level_a_permitted(routing):`n        return"
        New  = "    if destination != `"external`" or not level_a_permitted(routing):`n        return"
        Tests = @('tests/unit/router/test_external_primary.py::test_level_a_on_an_external_primary_is_refused_before_the_transport')
    },
    @{
        Name = '2 - generic refusal text'
        File = $policy
        Old  = '    raise LevelARefusedError(detail)'
        New  = '    raise LevelARefusedError(detail, message="model rejected the request")'
        Tests = @('tests/unit/router/test_external_primary.py::test_the_refusal_text_is_the_ukrainian_message_naming_the_reason')
    },
    @{
        Name = '3 - accept the permission outside dev'
        File = $chat
        Old  = "    if permitted:`n"
        New  = "    if permitted and False:`n"
        Tests = @('tests/unit/router/test_external_primary.py::test_the_permission_outside_dev_refuses_to_start')
    },
    @{
        Name = '4 - drop the external-implies-anonymise branch'
        File = $policy
        Old  = "    if destination == `"external`":`n        return True`n"
        New  = ""
        Tests = @('tests/unit/router/test_external_primary.py::test_the_permission_does_not_send_the_payload_in_the_clear')
    },
    @{
        Name = '5 - degraded route keeps anonymisation off'
        File = $policy
        Old  = '            requires_anonymisation=requires_anonymisation(level, destination),'
        New  = '            requires_anonymisation=False,'
        Tests = @('tests/unit/router/test_external_primary.py::test_a_degraded_route_under_an_external_primary_still_anonymises')
    },
    @{
        Name = '6 - stop recording external on the result'
        File = $chat
        Old  = '    result.destination = destination'
        New  = '    result.destination = "local"'
        Tests = @('tests/unit/router/test_policy.py::test_the_local_call_reports_an_external_destination_as_external')
    },
    @{
        Name = '7 - the flag becomes the only path to external'
        File = $policy
        Old  = '        primary_is_external=flagged or concern is not None,'
        New  = '        primary_is_external=flagged,'
        Tests = @('tests/unit/router/test_external_primary.py::test_a_public_url_is_external_without_any_flag')
    },
    @{
        Name = '8 - drop the startup check on the permission'
        File = $chat
        Old  = "    if permitted:`n"
        New  = "    if False:`n"
        Tests = @('tests/unit/router/test_external_primary.py::test_the_permission_outside_dev_refuses_to_start')
    },
    @{
        Name = '9 - rewrite the degraded reason unconditionally'
        File = $policy
        Old  = '    if not routing.primary_is_external:'
        New  = '    if False:'
        Tests = @('tests/unit/router/test_external_primary.py::test_a_private_primary_leaves_the_degraded_reason_string_unchanged')
    },
    @{
        Name = '10 - revert a reason to kept-on-the-server'
        File = $policy
        Old  = '; nothing was sent off this machine, and the payload is not anonymised'
        New  = '; the payload is kept on the server and is not anonymised'
        Tests = @('tests/unit/router/test_external_primary.py::test_the_private_degraded_reason_states_the_mechanism_and_not_a_slogan')
    }
)

$failures = @()
foreach ($m in $mutations) {
    $target = $m.File
    $text = Get-Text $target

    if (-not $text.Contains($m.Old)) {
        $failures += "$($m.Name): the text to mutate was not found in $target"
        Write-Output "SKIP      $($m.Name) - target text not found"
        continue
    }

    [System.IO.File]::WriteAllText((Join-Path $root $target), $text.Replace($m.Old, $m.New))

    $result = 1
    $tail = ''
    try {
        $out = & $py -m pytest @($m.Tests) -q 2>&1
        $result = $LASTEXITCODE
        $tail = ($out | Select-Object -Last 1)
    } catch {
        $result = 1
        $tail = "pytest raised: $($_.Exception.Message)"
    }

    if (-not (Restore-File $target)) {
        $failures += "$($m.Name): FAILED TO RESTORE $target - stopping"
        Write-Output "RESTORE-FAIL $($m.Name)"
        break
    }

    if ($result -eq 0) {
        $failures += "$($m.Name): the test still PASSED over the mutation"
        Write-Output "NOTCAUGHT $($m.Name)"
    } else {
        Write-Output "CAUGHT    $($m.Name)   [$(($tail -replace '\s+', ' ').Trim())]"
    }
}

foreach ($f in @($policy, $chat)) {
    $same = (Restore-File $f)
    Write-Output "RESTORED  $f  byte-exact=$same"
    if (-not $same) { $failures += "final hash mismatch for $f" }
}

if ($failures.Count -gt 0) {
    Write-Output 'SWEEP FAILURES:'
    foreach ($line in $failures) { Write-Output "  - $line" }
    exit 1
}
Write-Output 'SWEEP OK: every mutation was caught and every file was restored byte-exact'
exit 0
