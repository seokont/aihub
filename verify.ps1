param(
    [string]$Root = 'D:\MEDINSKY_Start2',
    [switch]$KeepStack
)

# Acceptance harness for Phase 0 (tasks 0.1 + 0.2).
#
#   powershell -File verify.ps1
#
# It brings up a FRESH stack (down -v first), then checks:
#   * every service is healthy and only nginx/keycloak/langfuse publish ports, on 127.0.0.1;
#   * the dev page, /healthz and the gateway health through nginx — at /api/health, which nginx
#     routes to the gateway's /health because /api/ otherwise belongs to the UI fork;
#   * Keycloak imported the realm (OIDC discovery answers, test-user password grant works);
#   * /auth/me returns sub/email/roles for that token (through nginx, and NOT at /api/auth/me,
#     where the UI's Express backend answers 404);
#   * a tampered token and a missing token are rejected with 401;
#   * every gateway log line is JSON and carries a request_id.
#
# It creates .env from .env.example if missing (dev placeholders; POSTGRES_PORT is
# moved off 5432 because this host already runs a native PostgreSQL) and removes it
# again at the end unless it existed before.
#
# NOTE on booleans: PowerShell's -match/-notmatch on a collection return the FILTERED
# COLLECTION or $null, not a boolean, and passing that to a [bool] parameter throws.
# Every check therefore coerces explicitly with [bool].

$ErrorActionPreference = 'Continue'
Set-Location $Root
$compose = @('compose', '--env-file', '.env', '-f', 'infra/docker-compose.dev.yml')
$envFileCreated = $false
$failures = New-Object System.Collections.Generic.List[string]
$checksRun = 0

function Section([string]$Title) {
    Write-Output ''
    Write-Output "===== $Title ====="
}

function Check([string]$Name, [bool]$Ok, [string]$Detail = '') {
    $script:checksRun++
    if ($Ok) {
        Write-Output ("PASS  {0} {1}" -f $Name, $Detail)
    } else {
        Write-Output ("FAIL  {0} {1}" -f $Name, $Detail)
        $script:failures.Add($Name)
    }
}

# Normalise a PowerShell regex result (collection/$null) into a real boolean.
function AnyMatch($Value) { return [bool](@($Value | Where-Object { $_ }).Count -gt 0) }

# --- helpers that never throw out of a check -------------------------------
function Get-HttpStatus([string]$Uri, [hashtable]$Headers = @{}) {
    try {
        $response = Invoke-WebRequest -Uri $Uri -Headers $Headers -UseBasicParsing -TimeoutSec 20
        return [int]$response.StatusCode
    } catch {
        if ($_.Exception.Response) { return [int]$_.Exception.Response.StatusCode }
        return -1
    }
}

function Get-Json([string]$Uri, [hashtable]$Headers = @{}) {
    try {
        return Invoke-RestMethod -Uri $Uri -Headers $Headers -TimeoutSec 20
    } catch {
        Write-Output "  request error for ${Uri}: $($_.Exception.Message)"
        return $null
    }
}

function ConvertFrom-JwtPayload([string]$Token) {
    $payload = $Token.Split('.')[1].Replace('-', '+').Replace('_', '/')
    while ($payload.Length % 4) { $payload += '=' }
    $json = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($payload))
    return $json | ConvertFrom-Json
}

try {
    Section 'daemon'
    docker info --format 'server={{.ServerVersion}} os={{.OSType}}'
    if ($LASTEXITCODE -ne 0) {
        Write-Output 'RESULT: BLOCKED - docker daemon not reachable'
        exit 2
    }

    if (-not (Test-Path '.env')) {
        Copy-Item '.env.example' '.env' -Force
        $envFileCreated = $true
        $patched = (Get-Content '.env') -replace '(?m)^POSTGRES_PORT=.*$', 'POSTGRES_PORT=55432'
        [System.IO.File]::WriteAllLines((Join-Path (Get-Location) '.env'), $patched)
        Write-Output '.env created from .env.example (dev placeholders, POSTGRES_PORT=55432)'
    }
    $envMap = @{}
    foreach ($line in Get-Content '.env') {
        if ($line -match '^\s*([A-Z0-9_]+)\s*=\s*(.*)$') { $envMap[$Matches[1]] = $Matches[2].Trim() }
    }
    $testPassword = $envMap['MONI_TEST_USER_PASSWORD']
    $keycloakPort = if ($envMap['KEYCLOAK_PORT']) { $envMap['KEYCLOAK_PORT'] } else { '8081' }
    $httpPort = if ($envMap['NGINX_HTTP_PORT']) { $envMap['NGINX_HTTP_PORT'] } else { '80' }
    $realmIssuer = "http://127.0.0.1:$keycloakPort/realms/moni"
    $managerSubject = ''

    Section 'fresh start (down -v, then up -d --build --wait)'
    docker @compose down -v --remove-orphans *> $null
    # --build is required: `up -d` alone reuses the existing gateway image and would
    # silently test stale code.
    docker @compose up -d --build --wait --wait-timeout 420 2>$null
    $upExit = $LASTEXITCODE
    Write-Output "up --build --wait exit=$upExit"
    Check 'docker compose up -d --build --wait succeeded' ($upExit -eq 0)

    Section 'docker compose ps'
    docker @compose ps

    Section 'port bindings (published ports must be 127.0.0.1 only)'
    $rows = @(docker ps --filter 'name=moni-ai-dev' --format '{{.Names}}|{{.Ports}}')
    $published = @()
    foreach ($row in $rows) {
        $parts = $row -split '\|'
        $name = ($parts[0] -replace 'moni-ai-dev-', '') -replace '-\d+$', ''
        $ports = ''
        if ($parts.Count -gt 1) { $ports = $parts[1].Trim() }
        # Only mappings containing "->" are PUBLISHED to the host. A bare "5432/tcp"
        # is an exposed-only container port, which is what we want for postgres,
        # redis, langfuse-db and the gateway.
        if ($ports -match '->') {
            Write-Output ("  {0}: PUBLISHED {1}" -f $name, $ports)
            $published += $name
            Check "loopback-only: $name" ([bool]($ports -match '^127\.0\.0\.1:')) $ports
        } else {
            Write-Output ("  {0}: internal only ({1})" -f $name, $ports)
        }
    }
    Check 'no 0.0.0.0 anywhere' (-not (AnyMatch ($rows -match '0\.0\.0\.0')))
    foreach ($internal in 'gateway', 'postgres', 'redis', 'langfuse-db') {
        Check "$internal not published" (-not ($published -contains $internal))
    }

    Section 'health'
    $ps = @(docker @compose ps --format '{{.Service}}|{{.State}}|{{.Status}}')
    $unhealthy = @()
    foreach ($row in $ps) {
        $parts = $row -split '\|'
        Write-Output ("  {0}: {1} {2}" -f $parts[0], $parts[1], $parts[2])
        if ($parts[0] -eq 'migrate') { continue }  # one-shot service, asserted below
        if ($row -notmatch 'healthy') { $unhealthy += $parts[0] }
    }
    Check 'all long-running services healthy' ($unhealthy.Count -eq 0) ($unhealthy -join '; ')

    Section 'migrations (task 0.3)'
    # A one-shot container is removed by compose once it exits, so there is no
    # `migrate` row left in `ps` to inspect: the authoritative evidence is
    # (a) `up -d --wait` succeeded — which requires migrate to have completed
    # successfully — and (b) the revision actually applied in the database.
    $migrateContainer = @(docker ps -a --filter 'name=moni-ai-dev-migrate' --format '{{.Status}}')
    Write-Output ("  migrate container: " + $(if ($migrateContainer.Count) { $migrateContainer -join '; ' } else { '(removed after a successful run)' }))
    Check 'migrate ran without leaving a failed container' ([bool](
        $migrateContainer.Count -eq 0 -or (AnyMatch ($migrateContainer -match 'Exited \(0\)'))))
    Check 'compose up reported migrate success' ($upExit -eq 0)

    $current = @(docker @compose run --rm --no-deps migrate alembic -c db/alembic.ini current 2>$null)
    $currentText = ($current | Where-Object { $_ -match '\S' }) -join ' '
    Write-Output ("  alembic current: " + $currentText)
    # What these three prove, and what they cannot, because the difference cost a feature.
    #
    # They prove the *migrate image's own view* is consistent and at its own head. They CANNOT
    # prove that the image is current with the working tree: migration 0009 sat unapplied for
    # weeks behind a migrate container that exited 0, because the Alembic scripts are baked into
    # that image — a stale image runs `upgrade head`, finds nothing newer *in its own copy* and
    # reports `0008 (head)`, which passes every check below. A container's exit code is not
    # evidence about the schema, and neither is its own alembic.
    #
    # The check that reads the TREE — the only place the truth was newer than both the image and
    # the database. It is done here, in PowerShell, rather than by shelling out to
    # `scripts/check_migrations.py`, because this script assumes docker and nothing else and adding
    # a `uv` dependency to the acceptance run is a worse trade than reading nine small files.
    #
    # `make check-migrations` runs the same comparison through Alembic itself and is the authority
    # on what head is; this is the cross-check that cannot be fooled by a stale image.
    Check 'migrate image reports a 000x revision' ([bool]($currentText -match '000\d'))
    Check 'migrate image reports its own head' ([bool]($currentText -match 'head'))
    Check 'migrate image is at 0002 or later' ([bool]($currentText -match '000[2-9]'))

    $pgUser = $envMap['POSTGRES_USER']
    $pgDb = $envMap['POSTGRES_DB']

    # A revision is a head when nothing else declares it as its `down_revision`. Anchored with
    # `(?m)^` because `-match` is unanchored and the substring "revision: str" appears inside
    # "down_revision: str | None = ..." — an unanchored pattern would read every parent as a head.
    $revisions = @{}
    $referenced = @{}
    foreach ($file in Get-ChildItem -Path (Join-Path $Root 'db/migrations/versions') -Filter '*.py' -File) {
        $body = Get-Content -Path $file.FullName -Raw
        if ($body -match '(?m)^revision\s*(?::\s*str\s*)?=\s*"([^"]+)"') {
            $revisions[$Matches[1]] = $file.Name
        }
        if ($body -match '(?m)^down_revision\s*(?::\s*str\s*\|\s*None\s*)?=\s*"([^"]+)"') {
            $referenced[$Matches[1]] = $true
        }
    }
    $treeHeads = @($revisions.Keys | Where-Object { -not $referenced.ContainsKey($_) } | Sort-Object)
    Write-Output ("  migration files: " + $revisions.Count + ", tree head(s): " + ($treeHeads -join ', '))
    # Fail closed on the PARSING too, not only on the answer: a parse that found too few revisions
    # is exactly how a stale database would pass this check, so "not exactly one head" is a failure
    # rather than a skip.
    Check 'migration files parse to exactly one head' ([bool]($treeHeads.Count -eq 1)) ("heads: " + ($treeHeads -join ', '))

    $dbRevision = @(docker @compose exec -T postgres psql -U $pgUser -d $pgDb -tAc 'select version_num from alembic_version' 2>$null |
        Where-Object { $_ -match '\S' }) -join ''
    $dbRevision = $dbRevision.Trim()
    Write-Output ("  database revision: " + $dbRevision)
    Check 'database is at the head this checkout carries' ([bool](
        $treeHeads.Count -eq 1 -and $dbRevision -eq $treeHeads[0])) ("db=" + $dbRevision + " tree=" + ($treeHeads -join ','))

    $query = "SELECT count(*) FROM information_schema.tables WHERE table_name='audit_log';"
    $tableCount = (docker @compose exec -T postgres psql -U $pgUser -d $pgDb -tAc $query 2>$null | Where-Object { $_ -match '\S' }) -join ''
    Write-Output ("  audit_log tables: " + $tableCount)
    Check 'audit_log table exists' ($tableCount.Trim() -eq '1')
    $indexQuery = "SELECT string_agg(indexname, ',' ORDER BY indexname) FROM pg_indexes WHERE tablename='audit_log';"
    $indexes = (docker @compose exec -T postgres psql -U $pgUser -d $pgDb -tAc $indexQuery 2>$null | Where-Object { $_ -match '\S' }) -join ''
    Write-Output ("  indexes: " + $indexes)
    Check 'audit_log indexes present' ([bool]($indexes -match 'ix_audit_log_user_id_ts' -and $indexes -match 'ix_audit_log_trace_id'))

    # The allowlist is every table a migration legitimately creates, each with the task that added
    # it. It is not a "no new tables" rule — that would be red from the moment the next phase lands,
    # and a harness that is always red is a harness nobody reads, which is the same blindness that
    # let a migration sit unapplied behind a healthy stack (ADR 0011). What it catches is a table
    # nobody intended: a stray `create_all`, a hand-made table on a developer's machine.
    #
    #   audit_log            task 0.3   append-only audit (§3.8)
    #   odoo_user_map        task 1.1   per-user Odoo credentials (§3.2)
    #   alembic_version      task 0.3   Alembic's own bookkeeping
    #   checkpoints*         task 1.3   LangGraph PostgresSaver (three tables + its migrations)
    #   doc_sources          task 1.5   RAG corpus
    #   doc_chunks           task 1.5   RAG chunks (level added by 0009)
    #   approvals            task 2.2   the approval store (§3.3)
    #   odoo_idempotency     task 2.3   the write ledger (§3.7)
    #   processed_messages   task 2.6   the inbound-mail ledger (the trigger's dedup guard)
    #   auto_mode_whitelist  task 2.x   the (user, scenario) auto-mode grants (§3.3)
    $knownTables = @(
        'audit_log', 'odoo_user_map', 'alembic_version',
        'checkpoints', 'checkpoint_blobs', 'checkpoint_migrations', 'checkpoint_writes',
        'doc_sources', 'doc_chunks',
        'approvals', 'odoo_idempotency', 'processed_messages', 'auto_mode_whitelist'
    )
    $knownList = ($knownTables | ForEach-Object { "'$_'" }) -join ','
    $otherTables = (docker @compose exec -T postgres psql -U $pgUser -d $pgDb -tAc "SELECT string_agg(tablename, ',' ORDER BY tablename) FROM pg_tables WHERE schemaname='public' AND tablename NOT IN ($knownList);" 2>$null | Where-Object { $_ -match '\S' }) -join ''
    Check 'no unexpected tables were created' ([string]::IsNullOrWhiteSpace($otherTables)) ("found: " + $otherTables)

    $mapTable = (docker @compose exec -T postgres psql -U $pgUser -d $pgDb -tAc "SELECT count(*) FROM information_schema.tables WHERE table_name='odoo_user_map';" 2>$null | Where-Object { $_ -match '\S' }) -join ''
    Write-Output ("  odoo_user_map tables: " + $mapTable)
    Check 'odoo_user_map table exists (task 1.1)' ($mapTable.Trim() -eq '1')
    $mapColumns = (docker @compose exec -T postgres psql -U $pgUser -d $pgDb -tAc "SELECT string_agg(column_name, ',' ORDER BY ordinal_position) FROM information_schema.columns WHERE table_name='odoo_user_map';" 2>$null | Where-Object { $_ -match '\S' }) -join ''
    Write-Output ("  odoo_user_map columns: " + $mapColumns)
    Check 'odoo_user_map stores the key encrypted' ([bool]($mapColumns -match 'odoo_api_key_encrypted'))

    Section 'HTTP: dev page, healthz, gateway health'
    $dev = $null
    try { $dev = Invoke-WebRequest -Uri "http://127.0.0.1:$httpPort/" -UseBasicParsing -TimeoutSec 20 } catch { Write-Output "  error: $($_.Exception.Message)" }
    Check 'GET / is the dev page' ([bool]($dev -and $dev.StatusCode -eq 200 -and $dev.Content -match 'MONI AI dev')) ("status=" + $(if ($dev) { $dev.StatusCode } else { 'n/a' }))
    Check 'GET /healthz' ((Get-HttpStatus "http://127.0.0.1:$httpPort/healthz") -eq 200)
    $gwHealth = Get-Json "http://127.0.0.1:$httpPort/api/health"
    Check 'GET /api/health via nginx -> gateway' ([bool]($gwHealth -and $gwHealth.status -eq 'ok')) ($gwHealth | ConvertTo-Json -Compress)

    Section "Keycloak realm import (127.0.0.1:$keycloakPort)"
    $disco = Get-Json "$realmIssuer/.well-known/openid-configuration"
    Check 'realm moni discovery' ([bool]($disco -and $disco.issuer -eq $realmIssuer)) $(if ($disco) { $disco.issuer } else { 'no answer' })

    Section 'token for test user "manager" (password grant on moni-ui)'
    $tokenResponse = $null
    try {
        $tokenResponse = Invoke-RestMethod -Method Post `
            -Uri "$realmIssuer/protocol/openid-connect/token" `
            -Body @{
                grant_type = 'password'
                client_id  = 'moni-ui'
                username   = 'manager'
                password   = $testPassword
            } -ContentType 'application/x-www-form-urlencoded' -TimeoutSec 30
    } catch {
        Write-Output "  token request error: $($_.Exception.Message)"
    }
    $token = $null
    if ($tokenResponse) { $token = $tokenResponse.access_token }
    Check 'password grant returns an access token' ([bool]$token)
    if ($token) {
        $claims = ConvertFrom-JwtPayload $token
        Write-Output ("  token iss={0} aud={1} roles={2}" -f $claims.iss, ($claims.aud -join ','), ($claims.realm_access.roles -join ','))
        Check 'token iss is the public issuer' ($claims.iss -eq $realmIssuer) $claims.iss
        Check 'token aud contains moni-gateway' ([bool]($claims.aud -contains 'moni-gateway')) ($claims.aud -join ',')
    }

    Section 'GET /auth/me with the token'
    # NOT `/api/auth/me`. Task 1.4 gave `/api/` to the LibreChat fork, so the gateway's identity
    # route through nginx is `/auth/me` — and `/api/auth/me` falls to the UI's Express backend,
    # which answers `{"message":"Endpoint not found"}` with a 404. All five checks below probed the
    # wrong upstream for that reason: three identity assertions read fields off a 404 body, and the
    # two rejection checks asserted 401 against an endpoint that never returned anything else.
    # `tests/integration/gateway/test_auth_roundtrip.py` fixed its own paths to `/auth/me` and
    # `/health` when this was found; the harness was not updated at the same time, which is how a
    # check stays red for two phases and teaches everyone to skim it (ADR 0011's lesson, in nginx).
    if ($token) {
        $me = Get-Json "http://127.0.0.1:$httpPort/auth/me" @{ Authorization = "Bearer $token" }
        if ($me) { Write-Output ("  " + ($me | ConvertTo-Json -Compress)) }
        Check '/auth/me returns sub' ([bool]($me -and $me.sub))
        Check '/auth/me returns email manager@moni.local' ([bool]($me -and $me.email -eq 'manager@moni.local')) $(if ($me) { $me.email } else { '' })
        Check '/auth/me returns role manager' ([bool]($me -and ($me.roles -contains 'manager'))) $(if ($me) { $me.roles -join ',' } else { '' })
        # Reused by the audit assertions below: the row must be keyed by this sub.
        if ($me) { $managerSubject = $me.sub }
    } else {
        Check '/auth/me returns sub' $false 'no token available'
    }

    Section 'rejections'
    if ($token) {
        $tampered = $token.Substring(0, $token.Length - 8) + 'AAAAAAAA'
        $tamperedStatus = Get-HttpStatus "http://127.0.0.1:$httpPort/auth/me" @{ Authorization = "Bearer $tampered" }
        Check 'tampered token -> 401' ($tamperedStatus -eq 401) "status=$tamperedStatus"
    }
    $noTokenStatus = Get-HttpStatus "http://127.0.0.1:$httpPort/auth/me"
    Check 'missing token -> 401' ($noTokenStatus -eq 401) "status=$noTokenStatus"

    Section 'audit trail (task 0.3, §3.8)'
    $pgUser = $envMap['POSTGRES_USER']
    $pgDb = $envMap['POSTGRES_DB']
    function Invoke-Sql([string]$Sql) {
        return ((docker @compose exec -T postgres psql -U $pgUser -d $pgDb -tAc $Sql 2>$null) |
            Where-Object { $_ -match '\S' }) -join ''
    }

    $successCount = (Invoke-Sql "SELECT count(*) FROM audit_log WHERE action='auth.me' AND user_id='$managerSubject';").Trim()
    Check 'successful call wrote an audit row for the user sub' ($successCount -match '^[1-9]\d*$') "rows=$successCount sub=$managerSubject"

    $deniedCount = (Invoke-Sql "SELECT count(*) FROM audit_log WHERE action='auth.me.denied';").Trim()
    Check 'failed (401) attempt wrote an audit row' ($deniedCount -match '^[1-9]\d*$') "rows=$deniedCount"

    $deniedResult = (Invoke-Sql "SELECT result FROM audit_log WHERE action='auth.me.denied' ORDER BY ts DESC LIMIT 1;").Trim()
    Write-Output ("  last denied result: " + $deniedResult)
    Check 'denied row records a reason' ([bool]($deniedResult -match '^denied: '))

    $leaked = (Invoke-Sql "SELECT count(*) FROM audit_log WHERE args_redacted::text ILIKE '%bearer %' OR args_redacted::text LIKE '%eyJ%' OR args_redacted::text ILIKE '%authorization%';").Trim()
    Check 'no token or Authorization header stored in args_redacted' ($leaked -eq '0') "matching rows=$leaked"

    $sample = Invoke-Sql "SELECT user_id || ' | ' || action || ' | ' || coalesce(result,'') || ' | ' || coalesce(trace_id,'') FROM audit_log ORDER BY ts DESC LIMIT 4;"
    Write-Output '  most recent rows (user_id | action | result | trace_id):'
    foreach ($row in @($sample -split "`n")) { if ($row.Trim()) { Write-Output ('    ' + $row.Trim()) } }

    $traceLinked = (Invoke-Sql "SELECT count(*) FROM audit_log WHERE trace_id IS NOT NULL AND length(trace_id) = 32;").Trim()
    Check 'audit rows carry a trace id (request id)' ($traceLinked -match '^[1-9]\d*$') "rows=$traceLinked"

    Section 'append-only cannot be undone from the app'
    $forbidden = @(Select-String -Path 'gateway\src\moni_gateway\*.py' -Pattern 'sqlalchemy import .*\b(update|delete)\b' -ErrorAction SilentlyContinue)
    Check 'no update/delete imported anywhere in the gateway' ($forbidden.Count -eq 0) ($forbidden.Line -join '; ')

    Section 'gateway logs are JSON lines carrying request_id'
    $logLines = @(docker @compose logs --no-log-prefix --tail 80 gateway 2>$null)
    $jsonLines = 0
    $withRequestId = 0
    $nonJson = @()
    foreach ($line in $logLines) {
        $trimmed = $line.Trim()
        if (-not $trimmed.StartsWith('{')) { continue }
        try {
            $obj = $trimmed | ConvertFrom-Json
            $jsonLines++
            if ($obj.PSObject.Properties.Name -contains 'request_id') { $withRequestId++ }
        } catch {
            $nonJson += $trimmed
        }
    }
    Write-Output ("  parsed JSON lines: {0}, carrying request_id: {1}" -f $jsonLines, $withRequestId)
    Check 'gateway log lines are JSON' ($jsonLines -gt 0) "json=$jsonLines"
    Check 'every JSON log line carries request_id' ($jsonLines -gt 0 -and $jsonLines -eq $withRequestId) "json=$jsonLines withId=$withRequestId"
    Check 'no malformed JSON log line' ($nonJson.Count -eq 0) ($nonJson -join ' | ')
    $requestLines = @($logLines | Where-Object { $_ -match '"event":\s*"request"' })
    Check 'request lines were logged' ($requestLines.Count -gt 0) "count=$($requestLines.Count)"
    if ($requestLines.Count -gt 0) {
        Write-Output '  last request line:'
        Write-Output ('    ' + $requestLines[-1].Trim())
    }

    Section 'X-Request-ID echo'
    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$httpPort/api/health" -Headers @{ 'X-Request-ID' = 'verify-harness-id' } -UseBasicParsing -TimeoutSec 20
        Check 'X-Request-ID is echoed' ([bool]($resp.Headers['X-Request-ID'] -eq 'verify-harness-id')) ([string]$resp.Headers['X-Request-ID'])
    } catch {
        Check 'X-Request-ID is echoed' $false $_.Exception.Message
    }
}
catch {
    Write-Output ''
    Write-Output "HARNESS ERROR: $($_.Exception.Message)"
    Write-Output $_.ScriptStackTrace
    $failures.Add('harness error')
}
finally {
    if (-not $KeepStack) {
        Section 'teardown (down -v)'
        docker @compose down -v --remove-orphans *> $null
        Write-Output "down exit=$LASTEXITCODE"
    }
    if ($envFileCreated) {
        Remove-Item '.env' -Force -ErrorAction SilentlyContinue
        Write-Output '.env (generated by the harness) removed'
    }
}

Section 'RESULT'
Write-Output ("checks run: {0}, failures: {1}" -f $checksRun, $failures.Count)
if ($failures.Count -eq 0 -and $checksRun -gt 0) {
    Write-Output 'ALL CHECKS PASSED'
    exit 0
}
Write-Output ("FAILED CHECKS: {0}" -f ($failures -join '; '))
exit 1
