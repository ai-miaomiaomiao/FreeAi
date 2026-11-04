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

rem Port: default 8000, override with the AGENT_PORT environment variable
rem (same convention as start.bat / install_autostart.bat).
if not defined AGENT_PORT set "AGENT_PORT=8000"

rem Never trust the first PATH hit: %LOCALAPPDATA%\Microsoft\WindowsApps\ holds
rem the Microsoft Store app execution alias, not Python (it exits 9009 and runs
rem nothing). Probe every candidate and keep the first that really runs AND is
rem 3.10+. Same floor as start.bat -- see the note there about agent_handoff.py.
set "PYW="
set "PYWLAST="
call :pick_pyw
if defined PYW goto pyw_ok

echo [error] 找不到可用的 Python 3.10 或更高版本（pythonw.exe）。
if not defined PYWLAST goto pyw_hint
echo         找到过 %PYWLAST%，但版本太低。本项目需要 3.10 及以上。
:pyw_hint
echo         装一个 3.10+ 并勾选 "Add python.exe to PATH"。
pause
exit /b 1

:pyw_ok

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
exit /b 0

rem ===========================================================================
rem  :pick_pyw -- fill PYW with a usable pythonw.exe (Python 3.10+)
rem ===========================================================================
:pick_pyw
rem (1) The py launcher resolves the newest real interpreter for us.
for /f "usebackq delims=" %%i in (`py -3 -c "import sys;print(sys.executable)" 2^>nul`) do call :try_pyw "%%i"
if defined PYW goto :eof
rem (2) Per-user installs ("Python314" sorts before "Python38" -> newer wins).
for /d %%d in ("%LOCALAPPDATA%\Programs\Python\Python3*") do call :try_pyw "%%~d\python.exe"
if defined PYW goto :eof
rem (3) PATH, minus the Microsoft Store alias.
for /f "delims=" %%i in ('where pythonw 2^>nul') do (
  if not defined PYW (
    echo %%i| findstr /i /c:"\WindowsApps\" >nul
    if errorlevel 1 call :try_pyw "%%i"
  )
)
if defined PYW goto :eof
for /f "delims=" %%i in ('where python 2^>nul') do (
  if not defined PYW (
    echo %%i| findstr /i /c:"\WindowsApps\" >nul
    if errorlevel 1 call :try_pyw "%%i"
  )
)
goto :eof

rem ===========================================================================
rem  :try_pyw <path> -- probe a candidate; prefer its pythonw.exe sibling
rem
rem  pythonw.exe cannot print to a console, so the version probe always goes
rem  through python.exe in the same folder. Existence alone proves nothing (the
rem  Store alias and the PyManager launcher both exit 0 doing nothing).
rem
rem  The version goes through a temp file: "for /f ... in ('"...")'" is broken
rem  by design, cmd strips the outer quotes when the command starts with one.
rem ===========================================================================
:try_pyw
if defined PYW goto :eof
if "%~1"=="" goto :eof
if not exist "%~1" goto :eof
set "VPY=%~1"
for %%f in ("%~1") do if exist "%%~dpfpython.exe" set "VPY=%%~dpfpython.exe"
set "VTMP=%TEMP%\_webai_pywver.txt"
del "%VTMP%" >nul 2>&1
"%VPY%" -c "import sys;print(sys.version_info[0],sys.version_info[1])" > "%VTMP%" 2>nul
set "VCAND="
if exist "%VTMP%" for /f "usebackq tokens=1,2" %%a in ("%VTMP%") do set "VCAND=%%a.%%b"
del "%VTMP%" >nul 2>&1
if not defined VCAND goto :eof
set "PYWLAST=%~1 (Python %VCAND%)"
rem Hide the console window when we can: that is the whole point of pythonw.exe.
set "VPYW=%~1"
for %%f in ("%~1") do (
  if /i "%%~nxf"=="python.exe" if exist "%%~dpfpythonw.exe" set "VPYW=%%~dpfpythonw.exe"
)
for /f "tokens=1,2 delims=." %%a in ("%VCAND%") do (
  if %%a GEQ 4 set "PYW=%VPYW%"
  if %%a EQU 3 if %%b GEQ 10 set "PYW=%VPYW%"
)
goto :eof
