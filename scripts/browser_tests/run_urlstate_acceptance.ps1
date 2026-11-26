# Center management opt-in acceptance runner (isolated sqlite DB).
#
# Purpose (AGENTS.md: temporary verification scripts must be promoted to formal
# tooling): one command performs the prerequisites and then runs
# scripts/browser_tests/e2e_center_urlstate.mjs, so the environment is not
# hand-assembled each time and the dev database can never be hit by accident.
#
# Discipline:
#   1) explicit env vars win; .env only fills vars NOT already set
#      (setdefault semantics) so the isolated DB name is never replaced;
#   2) prints the actual database it is connected to (manual cross-check);
#   3) uses a throwaway sqlite database (.work/ux_accept_test.sqlite3).
#
# Usage (PowerShell):
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\browser_tests\run_urlstate_acceptance.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\browser_tests\run_urlstate_acceptance.ps1 `
#       -Port 8124 -User ux_accept_user -Password "YourPass#2026" -Center rooms
#
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 decodes .ps1 as the
# system ANSI code page (GBK here); non-ASCII comments break the parser.
param(
  [int]$Port = 8124,
  [string]$User = "ux_accept_user",
  [string]$Password = "UxAccept#2026Strong",
  [string]$Center = "rooms"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$scriptDir = $PSScriptRoot
$dbPath = Join-Path $repoRoot ".work\ux_accept_test.sqlite3"
$py = Join-Path $repoRoot ".venv\Scripts\python.exe"

# ---- 1) explicit env vars first (highest priority) ----
$env:DJANGO_DB_ENGINE = "sqlite"
$env:DJANGO_DB_NAME = $dbPath
$env:DJANGO_ENV = "dev"
$env:DJANGO_DEBUG = "1"
$env:DJANGO_ALLOWED_HOSTS = "localhost,127.0.0.1"
$env:DJANGO_SETTINGS_MODULE = "hotel.settings"
$env:PYTHONIOENCODING = "utf-8"
$env:UX_ACCEPT_USER = $User
$env:UX_ACCEPT_PASSWORD = $Password

# ---- 2) .env fills the rest with setdefault semantics (never overrides above) ----
Get-Content (Join-Path $repoRoot ".env") | ForEach-Object {
  $line = $_.Trim()
  if (-not $line -or $line.StartsWith('#') -or -not $line.Contains('=')) { return }
  $key, $value = $line -split '=', 2
  $key = $key.Trim()
  if ($key -eq 'DJANGO_PLATFORM_ADMIN_USERNAME' -or $key -eq 'DJANGO_PLATFORM_ADMIN_PASSWORD') { return }
  if ([Environment]::GetEnvironmentVariable($key, 'Process')) { return }
  [Environment]::SetEnvironmentVariable($key, $value.Trim().Trim('"').Trim("'"), 'Process')
}

Write-Output "=== [1/5] migrate (isolated sqlite) ==="
& $py manage.py migrate 2>&1 | Select-Object -Last 3 | Out-String -Width 160
if ($LASTEXITCODE -ne 0) { throw "migrate failed" }

Write-Output "=== [2/5] seed_hotel_data ==="
& $py manage.py seed_hotel_data --rooms-per-type 3 --guests 5 --reservations 2 2>&1 |
  Select-Object -Last 5 | Out-String -Width 160
if ($LASTEXITCODE -ne 0) { throw "seed failed" }

Write-Output "=== [3/5] account + authorization setup (prints actual DB) ==="
& $py (Join-Path $scriptDir "prepare_acceptance_account.py") 2>&1 | Out-String -Width 200
if ($LASTEXITCODE -ne 0) { throw "account setup failed" }

Write-Output "=== [4/5] start server 127.0.0.1:$Port ==="
$out = Join-Path $env:TEMP "ux-accept-server.out.log"
$err = Join-Path $env:TEMP "ux-accept-server.err.log"
$proc = Start-Process -FilePath $py -ArgumentList "manage.py","runserver","127.0.0.1:$Port","--noreload" `
  -WorkingDirectory $repoRoot -RedirectStandardOutput $out -RedirectStandardError $err -WindowStyle Hidden -PassThru
$code = 1
try {
  $ready = $false
  for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 750
    if ($proc.HasExited) { break }
    try {
      $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/login/" -UseBasicParsing -TimeoutSec 5
      if ($r.StatusCode -eq 200) { $ready = $true; break }
    } catch { }
  }
  if (-not $ready) { throw "server not ready, see $err" }

  Write-Output "=== [5/5] real browser acceptance ==="
  Push-Location $scriptDir
  node "e2e_center_urlstate.mjs" --base "http://127.0.0.1:$Port" --center $Center `
    --user $User --password $Password 2>&1 | Out-String -Width 200
  $code = $LASTEXITCODE
  Pop-Location
  Write-Output "browser acceptance exit code = $code"
} finally {
  if ($proc -and -not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
}
exit $code
