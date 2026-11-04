@echo off
chcp 65001 >nul
setlocal

rem ---------------------------------------------------------------------------
rem 看门狗启动器：把 watchdog.py 以无窗口后台方式常驻。
rem
rem 为什么不直接双击 watchdog.py：那样会开一个黑窗口，误关就失效了。
rem 这里用 pythonw.exe（没有控制台）拉起，同时把看门狗自己的 stdout 丢掉，
rem 它写日志走 .ai\watchdog.log，不依赖控制台。
rem
rem 本文件注释必须保持 ASCII（见 start.bat 里的同样的坑）。
rem ---------------------------------------------------------------------------

cd /d "%~dp0"

rem Port: default is the AGENT_PORT value set below
rem (same convention as start.bat / install_autostart.bat).
if not defined AGENT_PORT set "AGENT_PORT=8000"

set "PYW="
rem pythonw.exe 和 python.exe 在同一个目录，优先用 sys.executable 同目录的
if exist "%USERPROFILE%\.workbuddy\binaries\python\versions\3.14.3\pythonw.exe" (
  set "PYW=%USERPROFILE%\.workbuddy\binaries\python\versions\3.14.3\pythonw.exe"
)
if not defined PYW (
  for /f "delims=" %%i in ('where pythonw 2^>nul') do if not defined PYW set "PYW=%%i"
)

if not defined PYW (
  echo [error] 找不到 pythonw.exe，退回到带窗口运行。
  for /f "delims=" %%i in ('where python 2^>nul') do if not defined PYW set "PYW=%%i"
)
if not defined PYW (
  echo [error] 找不到 python，先把 Python 加进环境变量。
  pause
  exit /b 1
)

rem 已经在跑就别再起第二个（否则两个看门狗会互相把服务重启来重启去）
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"name='pythonw.exe' or name='python.exe'\" | Where-Object { $_.CommandLine -like '*watchdog.py*' } | Select-Object -First 1 -ExpandProperty ProcessId" > "%TEMP%\_wd.txt" 2>nul
set /p WD_PID=<"%TEMP%\_wd.txt"
del "%TEMP%\_wd.txt" 2>nul
if defined WD_PID (
  echo 看门狗已在运行（PID %WD_PID%），不重复启动。
  echo 日志：.ai\watchdog.log
  exit /b 0
)

echo 启动看门狗...
start "" /b "%PYW%" "%~dp0src\watchdog.py"

timeout /t 2 /nobreak >nul
echo.
echo 看门狗已拉起。它会盯着 %AGENT_PORT%，服务挂掉后自动重启。
echo   看日志： type .ai\watchdog.log
echo   看状态： python src\watchdog.py --status
echo   停掉它： 任务管理器结束 pythonw.exe（服务本身不受影响）
echo.
