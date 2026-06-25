$action = New-ScheduledTaskAction -Execute 'D:\Python313\python.exe' -Argument '-m scripts.run_pipeline' -WorkingDirectory 'D:\projects\quant-system'
$trigger = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = 'PT5M'
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -RunLevel Highest
Register-ScheduledTask -TaskName 'QuantDailyReportStartup' -Action $action -Trigger $trigger -Principal $principal -Force
Write-Host "QuantDailyReportStartup created"
