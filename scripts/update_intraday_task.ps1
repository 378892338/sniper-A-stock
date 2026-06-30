# 更新 QuantDailyReportIntraday 计划任务
# 运行方式: 在 prompt 中输入 `! powershell -ExecutionPolicy Bypass -File D:\projects\quant-system\scripts\update_intraday_task.ps1

$taskName = "QuantDailyReportIntraday"
$python = "C:\Python314\python.exe"
$arg = "-m scripts.run_pipeline --mode intraday"
$workDir = "D:\projects\quant-system"

# 停止旧任务（如果有）
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

# 创建新任务
$action = New-ScheduledTaskAction -Execute $python -Argument $arg -WorkingDirectory $workDir
$trigger = New-ScheduledTaskTrigger -Daily -At 12:00
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force

Write-Output "任务 $taskName 已更新: Python=C:\Python314\python.exe, 工作目录=$workDir"
