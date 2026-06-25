@echo off
chcp 65001 >nul
title 安装量化日报计划任务

set PROJECT_DIR=D:\projects\quant-system
set PYTHON_EXE=D:\Python313\python.exe
set TASK_NAME=QuantDailyReport
set WRAPPER_SCRIPT=%PROJECT_DIR%\run_daily_pipeline.bat

echo ========================================
echo  安装全链路日报计划任务
echo ========================================
echo.
echo 项目路径: %PROJECT_DIR%
echo Python:   %PYTHON_EXE%
echo 任务名:   %TASK_NAME%
echo.

:: ── 生成启动包装脚本 ──
(
echo @echo off
echo chcp 65001 ^>nul
echo cd /d %PROJECT_DIR%
echo %PYTHON_EXE% -m scripts.run_pipeline
) > %WRAPPER_SCRIPT%

echo ✅ 启动脚本已创建: %WRAPPER_SCRIPT%
echo.

:: ── 删除旧任务（如有） ──
schtasks /delete /tn %TASK_NAME% /f >nul 2>&1

:: ── 创建日度任务（每天 16:00） ──
schtasks /create /tn %TASK_NAME% /sc daily /st 16:00 /f ^
    /tr "\"%WRAPPER_SCRIPT%\"" ^
    /ru %USERNAME%

if %ERRORLEVEL% equ 0 (
    echo ✅ 计划任务已创建
    echo    每天 16:00 自动执行全链路
    echo.
) else (
    echo ❌ 创建失败，请以管理员身份运行
    echo.
    pause
    exit /b 1
)

:: ── 创建开机补跑任务 ──
set TASK_NAME_STARTUP=QuantDailyReportStartup

schtasks /delete /tn %TASK_NAME_STARTUP% /f >nul 2>&1

schtasks /create /tn %TASK_NAME_STARTUP% /sc onstart /delay 0005:00 /f ^
    /tr "\"%WRAPPER_SCRIPT%\"" ^
    /ru %USERNAME%

if %ERRORLEVEL% equ 0 (
    echo ✅ 开机补跑任务已创建
    echo    开机 5 分钟后自动补跑缺失的日报
    echo.
) else (
    echo ⚠️ 开机补跑任务创建失败
    echo    可以手动创建: 任务计划程序 → 创建任务 → 触发器: 启动时
)

echo ========================================
echo  验证
echo ========================================
echo.
echo 1. 查看已安装的任务:
echo    schtasks /query /tn %TASK_NAME%
echo.
echo 2. 手动跑一次验证:
echo    cd /d %PROJECT_DIR%
echo    %PYTHON_EXE% -m scripts.run_pipeline --date 2026-06-03
echo.
echo 3. 查看日报输出:
echo    dir D:\Obsidian\SecondBrain\02-Projects\05-量化系统\
echo.
echo ========================================
echo  安装完成！ 可以按任意键关闭
echo ========================================
pause
