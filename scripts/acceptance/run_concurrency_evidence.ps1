# MySQL concurrency arbitration evidence runner (isolated MySQL test database).
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\acceptance\run_concurrency_evidence.ps1 `
#       -Output docs\acceptance\evidence\g14-final-20261127\mysql-concurrency.log
#
# Scope:
#   Historical G14 set = inventory 7 + core.outbox 36 + reservations 2 = 45 cases.
#   The set below additionally covers three MySQL-only concurrency modules
#   (billing 5 + guests 2 + identity 3 = 10 cases), so the blast radius of the
#   ARCH-04 0016 migration and of the shared rate-limit component is exercised.
#   Additive only: the historical 45 cases stay directly comparable.
#
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 decodes .ps1 with the
# system ANSI code page (GBK here); non-ASCII comments break the parser.
param(
  [Parameter(Mandatory = $true)][string]$Output
)

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location -LiteralPath $repoRoot
$py = Join-Path $repoRoot ".venv\Scripts\python.exe"

# Explicit env vars win; .env only fills vars NOT already set (setdefault
# semantics) so an explicit DB name is never silently replaced.
Get-Content (Join-Path $repoRoot ".env") | ForEach-Object {
  $line = $_.Trim()
  if (-not $line -or $line.StartsWith('#') -or -not $line.Contains('=')) { return }
  $key, $value = $line -split '=', 2
  $key = $key.Trim()
  if ($key -eq 'DJANGO_PLATFORM_ADMIN_USERNAME' -or $key -eq 'DJANGO_PLATFORM_ADMIN_PASSWORD') { return }
  if ([Environment]::GetEnvironmentVariable($key, 'Process')) { return }
  [Environment]::SetEnvironmentVariable($key, $value.Trim().Trim('"').Trim("'"), 'Process')
}

$env:DJANGO_ENV = "test"
$env:PYTHONIOENCODING = "utf-8"

Write-Output "=== MySQL concurrency arbitration evidence ==="
# Do NOT set $ErrorActionPreference = "Stop" here: Django writes progress lines
# ("Creating test database...") to stderr, and under Stop that native stderr is
# raised as a terminating NativeCommandError which aborts the test run.
& $py manage.py test --noinput `
  apps.inventory.test_room_conflict_concurrency `
  apps.core.test_outbox `
  apps.reservations.test_mysql_concurrency `
  apps.billing.test_mysql_concurrency `
  apps.guests.test_mysql_concurrency `
  apps.identity.test_mysql_concurrency 2>&1 | Tee-Object -FilePath $Output
exit $LASTEXITCODE
