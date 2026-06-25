$action = New-ScheduledTaskAction -Execute 'D:\Python313\python.exe' -Argument '-m scripts.weekly_optimize' -WorkingDirectory 'D:\projects\quant-system'
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 09:00
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -RunLevel Highest
Register-ScheduledTask -TaskName 'QuantWeeklyOptimize' -Action $action -Trigger $trigger -Principal $principal -Force
Write-Host 'QuantWeeklyOptimize 已创建 (每周一 09:00)'
