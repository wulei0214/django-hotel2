# Start the evidence dev server against an isolated acceptance database.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\acceptance\serve_isolated_db.ps1 `
#       -Port 8001 -DbName test_hotel_e2e
#
# Prerequisites:
#   The isolated database must exist and be seeded first:
#     python scripts\acceptance\prepare_isolated_db.py --db-name test_hotel_e2e
#
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 decodes .ps1 with the
# system ANSI code page (GBK here); non-ASCII comments break the parser.
#
# Do NOT set $ErrorActionPreference = "Stop" here: Django writes progress lines
# to stderr, and under Stop that native stderr is raised as a terminating
# NativeCommandError which aborts the run.
param(
  [Parameter(Mandatory = $true)][int]$Port,
  [Parameter(Mandatory = $true)][string]$DbName
)

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location -LiteralPath $repoRoot
$py = Join-Path $repoRoot ".venv\Scripts\python.exe"

$envFile = Join-Path $repoRoot ".env"
if (-not (Test-Path -LiteralPath $envFile)) {
  Write-Error "missing .env at repo root: $envFile"
  exit 2
}
if (-not (Test-Path -LiteralPath $py)) {
  Write-Error "missing virtualenv interpreter: $py"
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

Write-Output "=== evidence dev server: db=$DbName port=$Port ==="
& $py (Join-Path $repoRoot "scripts\acceptance\isolated_devserver.py") "127.0.0.1:$Port"
exit $LASTEXITCODE
