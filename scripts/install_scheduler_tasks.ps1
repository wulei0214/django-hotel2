# PMS2 本地调度任务安装器（MC-010 残项收口：真实调度器安装证据）
# 依据：《性能优化与生产部署升级说明》§三 Windows Task Scheduler 调度口径
#       （expire_reservation_holds 5min / expire_invitations 15min /
#         run_billing_maintenance 10min / payment_compensation_worker 5min /
#         recover_stale_night_audits 15min / sync_inventory_calendar daily 02:30）
#
# 设计约束（2026-11-18 修正，务必保持）：
#   1. 动作一律用 pythonw.exe：python.exe 是控制台子系统程序，以交互会话
#      运行时会由 Windows Terminal 弹出可见窗口（标题显示可执行文件路径），
#      且并发触发时控制台创建会失败返回 0x800710E0。pythonw 不分配控制台。
#   2. 每类任务显式 --log=logs/scheduler/<slug>.log：六类任务并发追加同一
#      文件会互相穿插，证据不可读。
#   3. payment_compensation_worker 必须带 --once：该命令默认 while True 循环，
#      不带 --once 的任务永不退出，会长期占用一个实例并使后续触发全部被拒。
#   4. 触发重复期不得短于运维复核周期：早期写成 72h 会让任务在第 4 天静默停摆。
#   5. MultipleInstances=IgnoreNew + 15 分钟执行时限：单轮卡死不会堆积实例。
#   6. sync_inventory_calendar 必须带 --start-date/--end-date（命令强制要求），
#      用 {today} / {today+30} 占位符表示滚动窗口，由 scheduler_runner.py 展开；
#      早期漏传导致该任务每天 02:30 都以退出码 2 静默失败。
#
# 用法：
#   安装（注册并启用，等同"我要在本机部署调度"）：
#     powershell -ExecutionPolicy Bypass -File scripts\install_scheduler_tasks.ps1
#   停用（保留定义、不再触发；本机日常开发默认用这个）：
#     powershell -ExecutionPolicy Bypass -File scripts\install_scheduler_tasks.ps1 -Disable
#   重新启用（不改定义，仅恢复触发）：
#     powershell -ExecutionPolicy Bypass -File scripts\install_scheduler_tasks.ps1 -Enable
#   彻底移除：
#     powershell -ExecutionPolicy Bypass -File scripts\install_scheduler_tasks.ps1 -Uninstall
#
# 状态语义（务必区分）：
#   安装 = 部署意图；Disable/Enable 只切换计划任务 State，不重写定义，
#   因此停用后不会因为重跑 -Enable 之外的任何操作悄悄恢复触发。

param(
    [switch]$Uninstall,
    [switch]$Disable,
    [switch]$Enable
)

$ErrorActionPreference = "Stop"

# 以脚本位置反推仓库根，避免换机/换盘后动作指向错误路径
$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo ".venv\Scripts\pythonw.exe"
$runner = Join-Path $repo "scripts\scheduler_runner.py"

# 卸载/停用/启用只操作既有任务，不依赖 venv，故校验放在安装分支内
if ($Uninstall) {
    Get-ScheduledTask | Where-Object { $_.TaskName -like 'PMS2 *' } | ForEach-Object {
        if ($_.State -eq "Running") { Stop-ScheduledTask -TaskName $_.TaskName }
        Unregister-ScheduledTask -TaskName $_.TaskName -Confirm:$false
        Write-Output ("removed: " + $_.TaskName)
    }
    return
}

if ($Disable -or $Enable) {
    $targets = @(Get-ScheduledTask | Where-Object { $_.TaskName -like 'PMS2 *' })
    if ($targets.Count -eq 0) {
        throw "未找到 PMS2 * 计划任务；请先执行安装（不带开关）。"
    }
    foreach ($t in $targets) {
        if ($Disable) {
            # 正在跑的一轮先停掉，否则禁用后仍会跑完才退出
            if ($t.State -eq "Running") { Stop-ScheduledTask -TaskName $t.TaskName }
            Disable-ScheduledTask -TaskName $t.TaskName | Out-Null
            Write-Output ("disabled: " + $t.TaskName)
        } else {
            Enable-ScheduledTask -TaskName $t.TaskName | Out-Null
            Write-Output ("enabled: " + $t.TaskName)
        }
    }
    return
}

if (-not (Test-Path -LiteralPath $python)) {
    throw "缺少 GUI 子系统解释器：$python（venv 内应自带 pythonw.exe）"
}
if (-not (Test-Path -LiteralPath $runner)) {
    throw "缺少调度入口：$runner"
}

# Slug 用于独立日志文件；Cmd 为完整 manage.py 参数串
$tasks = @(
    @{ Slug = "expire_reservation_holds";    Name = "PMS2 expire_reservation_holds";    Cmd = "expire_reservation_holds";                Interval = 5 },
    @{ Slug = "expire_invitations";          Name = "PMS2 expire_invitations";          Cmd = "expire_invitations";                      Interval = 15 },
    @{ Slug = "billing_maintenance";         Name = "PMS2 billing_maintenance";         Cmd = "run_billing_maintenance";                 Interval = 10 },
    @{ Slug = "payment_compensation";        Name = "PMS2 payment_compensation_worker"; Cmd = "payment_compensation_worker --once";       Interval = 5 },
    @{ Slug = "recover_stale_night_audits";  Name = "PMS2 recover_stale_night_audits";  Cmd = "recover_stale_night_audits";              Interval = 15 }
)

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 1)

# 10 年重复期：等效"长期有效"，避免早期 72h 写法导致第 4 天静默停摆
$repetitionDuration = New-TimeSpan -Days 3650

foreach ($t in $tasks) {
    $logRel = "logs/scheduler/" + $t["Slug"] + ".log"
    $action = New-ScheduledTaskAction -Execute $python `
        -Argument ('"' + $runner + '" --log=' + $logRel + ' ' + $t["Cmd"]) `
        -WorkingDirectory $repo
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes $t["Interval"]) `
        -RepetitionDuration $repetitionDuration
    Register-ScheduledTask -TaskName $t["Name"] -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Force | Out-Null
    Write-Output ("installed: " + $t["Name"])
}

$dailyLog = "logs/scheduler/sync_inventory_calendar.log"
# 滚动窗口 {today} ~ {today+30}：与部署文档 cron 口径
# （--start-date $(date +%F) --end-date $(date -d "+30 days" +%F)）一致；
# 由 scheduler_runner.py 在派发前展开为 ISO 日期。
$dailyArgs = ' sync_inventory_calendar --property-code DEFAULT --room-type-code STD' +
    ' --start-date={today} --end-date={today+30}'
$dailyAction = New-ScheduledTaskAction -Execute $python `
    -Argument ('"' + $runner + '" --log=' + $dailyLog + $dailyArgs) `
    -WorkingDirectory $repo
$dailyTrigger = New-ScheduledTaskTrigger -Daily -At "02:30"
Register-ScheduledTask -TaskName "PMS2 sync_inventory_calendar" -Action $dailyAction `
    -Trigger $dailyTrigger -Settings $settings -Principal $principal -Force | Out-Null
Write-Output "installed: PMS2 sync_inventory_calendar"
