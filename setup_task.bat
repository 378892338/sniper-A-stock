@echo off
REM ⚠️ DEPRECATED — 请使用 setup_schedule.bat
REM 此脚本创建的是旧版计划任务（指向已废弃的 run_daily.bat）。
REM 请以管理员身份运行 setup_schedule.bat 来创建正确的计划任务。
schtasks /create /tn "QuantDailyReport" /tr "D:\Projects\quant-system\run_daily.bat" /sc DAILY /st 17:00 /f
echo Task created: %ERRORLEVEL%
