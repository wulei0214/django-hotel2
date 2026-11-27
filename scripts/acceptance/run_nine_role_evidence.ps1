# Run the nine-role permission denial matrix evidence on the isolated database.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\acceptance\run_nine_role_evidence.ps1 `
#       -RunId 20261127-r7 -Port 8011
#
# The wrapper starts the evidence dev server (isolated DB), waits until it is
# reachable, runs the matrix, and stops the server afterwards.
#
# Prerequisites:
#   1) The isolated database exists and is seeded:
#      python scripts\acceptance\prepare_isolated_db.py --db-name test_hotel_e2e
#   2) The local account manifests exist (gitignored working-dir files):
#      .work\v12_accounts.json  and  .work\v13\v13_accounts.json
#
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 decodes .ps1 with the
# system ANSI code page (GBK here); non-ASCII comments break the parser.
#
# Do NOT set $ErrorActionPreference = "Stop" here: Django writes progress lines
# to stderr, and under Stop that native stderr is raised as a terminating
# NativeCommandError which aborts the run.
param(
  [string]$RunId = "",
  [int]$Port = 8011,
  [string]$DbName = "test_hotel_e2e"
)

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location -LiteralPath $repoRoot
$py = Join-Path $repoRoot ".venv\Scripts\python.exe"

if (-not $RunId) {
  $RunId = (Get-Date -Format "yyyyMMdd-HHmmss")
}

$envFile = Join-Path $repoRoot ".env"
if (-not (Test-Path -LiteralPath $envFile)) {
  Write-Error "missing .env at repo root: $envFile"
  exit 2
}
if (-not (Test-Path -LiteralPath $py)) {
  Write-Error "missing virtualenv interpreter: $py"
  exit 2
}

# Account manifests are gitignored local files; fail early with a clear message
# instead of failing later inside the browser login step.
$missing = @()
foreach ($rel in @(".work\v12_accounts.json", ".work\v13\v13_accounts.json")) {
  if (-not (Test-Path -LiteralPath (Join-Path $repoRoot $rel))) { $missing += $rel }
}
if ($missing.Count -gt 0) {
  Write-Error ("missing account manifest(s): " + ($missing -join ", "))
  Write-Error "these are gitignored local files; generate them before running the matrix"
  exit 2
}

# Explicit env vars win; .env only fills vars NOT already set (setdefault
# semantics) so an explicit DB name is never silently replaced.
Get-Content $envFile | ForEach-Object {
  $line = $_.Trim()
  if (-not $line -or $line.StartsWith('#') -or -not $line.Contains('=')) { return }
  $key, $value = $line -split '=', 2
  $key = $key.Trim()
  if ($key -eq 'DJANGO_PLATFORM_ADMIN_USERNAME' -or $key -eq 'DJANGO_PLATFORM_ADMIN_PASSWORD') { return }
  if ([Environment]::GetEnvironmentVariable($key, 'Process')) { return }
  [Environment]::SetEnvironmentVariable($key, $value.Trim().Trim('"').Trim("'"), 'Process')
}

$env:DJANGO_ENV = "dev"
$env:DJANGO_DB_NAME = $DbName
# Local MySQL max_connections is only 20; avoid pool exhaustion (1040) while
# the evidence server is running.
$env:DJANGO_DB_CONN_MAX_AGE = "0"
$env:PYTHONIOENCODING = "utf-8"

$env:NINE_ROLE_BASE = "http://127.0.0.1:$Port"
$env:NINE_ROLE_RUN_ID = $RunId

$logDir = Join-Path $repoRoot ".work\nine-role"
if (-not (Test-Path -LiteralPath $logDir)) {
  New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}
$outLog = Join-Path $logDir "server-$RunId.out.log"
$errLog = Join-Path $logDir "server-$RunId.err.log"

function Test-PortOpen([int]$PortNumber, [int]$TimeoutMs = 500) {
  $client = New-Object System.Net.Sockets.TcpClient
  try {
    $async = $client.BeginConnect("127.0.0.1", $PortNumber, $null, $null)
    if (-not $async.AsyncWaitHandle.WaitOne($TimeoutMs)) { return $false }
    $client.EndConnect($async)
    return $true
  } catch {
    return $false
  } finally {
    $client.Close()
  }
}

Write-Output "=== nine-role matrix evidence: db=$($env:DJANGO_DB_NAME) base=$($env:NINE_ROLE_BASE) run=$RunId ==="

$proc = Start-Process -FilePath $py `
  -ArgumentList @((Join-Path $repoRoot "scripts\acceptance\isolated_devserver.py"), "127.0.0.1:$Port") `
  -WorkingDirectory $repoRoot `
  -RedirectStandardOutput $outLog `
  -RedirectStandardError $errLog `
  -NoNewWindow -PassThru

$exitCode = 1
try {
  $ready = $false
  for ($i = 0; $i -lt 60; $i++) {
    if ($proc.HasExited) {
      Write-Output "--- server stdout ---"
      if (Test-Path -LiteralPath $outLog) { Get-Content $outLog }
      Write-Output "--- server stderr ---"
      if (Test-Path -LiteralPath $errLog) { Get-Content $errLog }
      Write-Error "evidence server exited before it became reachable (exit=$($proc.ExitCode))"
      exit 1
    }
    if (Test-PortOpen -PortNumber $Port) { $ready = $true; break }
    Start-Sleep -Seconds 1
  }
  if (-not $ready) {
    Write-Error "evidence server did not become reachable on 127.0.0.1:$Port within 60s"
    exit 1
  }
  Write-Output "--- server stdout ---"
  if (Test-Path -LiteralPath $outLog) { Get-Content $outLog }

  & $py (Join-Path $repoRoot "scripts\acceptance\nine_role_matrix.py")
  $exitCode = $LASTEXITCODE
} finally {
  if ($proc -and -not $proc.HasExited) {
    Stop-Process -Id $proc.Id -Force
  }
}
exit $exitCode
