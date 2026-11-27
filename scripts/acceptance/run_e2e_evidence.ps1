# Run the full-chain HTTP E2E evidence against the isolated acceptance database.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\acceptance\run_e2e_evidence.ps1 `
#       -Evidence docs\acceptance\evidence\g14-final-20261127\e2e-56.log -Port 8001
#
# Prerequisites:
#   1) The isolated database exists and is seeded:
#      python scripts\acceptance\prepare_isolated_db.py --db-name test_hotel_e2e
#   2) The evidence server is running on the same port:
#      powershell -NoProfile -File scripts\acceptance\serve_isolated_db.ps1 `
#          -Port 8001 -DbName test_hotel_e2e
#
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 decodes .ps1 with the
# system ANSI code page (GBK here); non-ASCII comments break the parser.
#
# Do NOT set $ErrorActionPreference = "Stop" here: Django writes progress lines
# to stderr, and under Stop that native stderr is raised as a terminating
# NativeCommandError which aborts the run.
param(
  [Parameter(Mandatory = $true)][string]$Evidence,
  [int]$Port = 8001
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

# Platform-admin whitelist must stay off (activating it skews identity and
# permission checks).
$env:DJANGO_PLATFORM_ADMIN_USERNAME = ""
$env:DJANGO_PLATFORM_ADMIN_PASSWORD = ""

$env:DJANGO_ENV = "dev"
$env:DJANGO_DB_NAME = "test_hotel_e2e"
$env:PMS_E2E_ALLOWED_DB_NAMES = "test_hotel_e2e"
$env:PMS_E2E_SOURCE_DB = "hotel"
$env:PMS_E2E_BASE_URL = "http://127.0.0.1:$Port"

# The Windows console defaults to GBK and cannot encode the seed output check
# mark U+2713; the script mirrors stdout into a UTF-8 evidence file.
$env:PYTHONIOENCODING = "utf-8"

# Shared role password comes from the gitignored scripts/r6_env.txt; never
# printed, never stored.
$secretFile = Join-Path $repoRoot "scripts\r6_env.txt"
if (-not (Test-Path -LiteralPath $secretFile)) {
  Write-Error "missing controlled test credential file: $secretFile"
  exit 2
}
$pw = ""
foreach ($line in (Get-Content $secretFile)) {
  $t = $line.Trim()
  if ($t.StartsWith("TEST_ACCOUNT_PASSWORD=")) {
    $pw = $t.Substring("TEST_ACCOUNT_PASSWORD=".Length).Trim().Trim('"').Trim("'")
  }
}
if (-not $pw) {
  Write-Error "missing TEST_ACCOUNT_PASSWORD in scripts\r6_env.txt"
  exit 2
}
$env:PMS_E2E_PASSWORD = $pw

$evidenceDir = Split-Path -Parent $Evidence
if ($evidenceDir -and -not (Test-Path -LiteralPath $evidenceDir)) {
  New-Item -ItemType Directory -Path $evidenceDir -Force | Out-Null
}

Write-Output "=== full-chain E2E evidence: db=$($env:DJANGO_DB_NAME) base=$($env:PMS_E2E_BASE_URL) ==="
& $py (Join-Path $repoRoot "scripts\e2e_full_chain.py") `
  --i-understand-reset --evidence $Evidence 2>&1 | Tee-Object -FilePath "$Evidence.stdout.txt"
exit $LASTEXITCODE
