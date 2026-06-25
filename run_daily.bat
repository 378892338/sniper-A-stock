@echo off
REM ⚠️ DEPRECATED — 请使用 run_daily_pipeline.bat
REM 此脚本引用的 run_daily.py 不存在，已废弃。使用 setup_schedule.bat 重建计划任务。
REM Quant System 日报自动运行
D:\Python313\python.exe D:\Projects\quant-system\run_daily.py >> D:\Projects\quant-system\outputs\reports\run_log.txt 2>&1
