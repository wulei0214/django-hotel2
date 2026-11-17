# PMS2 本地调度任务安装器（MC-010 残项收口：真实调度器安装证据）
# 依据：《性能优化与生产部署升级说明》§三 Windows Task Scheduler 调度口径
#       （expire_reservation_holds 5min / expire_invitations 15min /
#         run_billing_maintenance 10min / payment_compensation_worker 5min /
#         recover_stale_night_audits 15min / sync_inventory_calendar daily 02:30）
# 实现：动作 = venv python + scripts/scheduler_runner.py（载入 .env 后执行
#       manage.py 子命令），显式 WorkingDirectory = 仓库根（schtasks 无"起始于"
#       设置，故用 ScheduledTasks API）；输出统一追加 logs/scheduler.log。
# 用法：powershell -ExecutionPolicy Bypass -File scripts\install_scheduler_tasks.ps1
# 卸载：powershell -Command "Unregister-ScheduledTask -TaskName 'PMS2 *' -Confirm:$false"

$repo = "F:\Desktop\django-hotel"
$python = Join-Path $repo ".venv\Scripts\python.exe"
$runner = Join-Path $repo "scripts\scheduler_runner.py"

$tasks = @(
    @{ Name = "PMS2 expire_reservation_holds";   Cmd = "expire_reservation_holds";    Interval = 5 },
    @{ Name = "PMS2 expire_invitations";         Cmd = "expire_invitations";          Interval = 15 },
    @{ Name = "PMS2 billing_maintenance";        Cmd = "run_billing_maintenance";     Interval = 10 },
    @{ Name = "PMS2 payment_compensation_worker"; Cmd = "payment_compensation_worker"; Interval = 5 },
    @{ Name = "PMS2 recover_stale_night_audits"; Cmd = "recover_stale_night_audits";  Interval = 15 }
)

foreach ($t in $tasks) {
    $action = New-ScheduledTaskAction -Execute $python `
        -Argument ($runner + " " + $t["Cmd"]) -WorkingDirectory $repo
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes $t["Interval"]) `
        -RepetitionDuration (New-TimeSpan -Hours 72)
    Register-ScheduledTask -TaskName $t["Name"] -Action $action -Trigger $trigger -Force | Out-Null
    Write-Output ("installed: " + $t["Name"])
}

$action = New-ScheduledTaskAction -Execute $python `
    -Argument ($runner + " sync_inventory_calendar --property-code DEFAULT --room-type-code STD") `
    -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Daily -At "02:30"
Register-ScheduledTask -TaskName "PMS2 sync_inventory_calendar" -Action $action -Trigger $trigger -Force | Out-Null
Write-Output "installed: PMS2 sync_inventory_calendar"
