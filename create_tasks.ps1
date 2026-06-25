$action = New-ScheduledTaskAction -Execute 'D:\Python313\python.exe' -Argument '-m scripts.run_pipeline' -WorkingDirectory 'D:\projects\quant-system'
$trigger = New-ScheduledTaskTrigger -Daily -At 16:00
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -RunLevel Highest
Register-ScheduledTask -TaskName 'QuantDailyReport' -Action $action -Trigger $trigger -Principal $principal -Force
Write-Host "QuantDailyReport created"

$startupTrigger = New-ScheduledTaskTrigger -AtStartup
$startupTrigger.Delay = 'PT5M'
Register-ScheduledTask -TaskName 'QuantDailyReportStartup' -Action $action -Trigger $startupTrigger -Principal $principal -Force
Write-Host "QuantDailyReportStartup created"
