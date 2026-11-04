@echo off
chcp 65001 >nul
setlocal

rem Always run from the script's own folder (also when double-clicked)
cd /d "%~dp0"

rem ---------------------------------------------------------------------------
rem 0. Port. Default port is the value on the "set AGENT_PORT" line below.
rem    Priority: first command-line argument > AGENT_PORT env > the default.
rem    Examples:  start.bat           -> the default port
rem               start.bat 8000      -> 8000 (same as the default)
rem               set AGENT_PORT=8000 ^&^& start.bat
rem    AGENT_PORT is exported below, so web_agent.py / watchdog.py inherit it.
rem ---------------------------------------------------------------------------
if not "%~1"=="" (
  echo %~1| findstr /r "^[0-9][0-9]*$" >nul
  if not errorlevel 1 set "AGENT_PORT=%~1"
)
if not defined AGENT_PORT set "AGENT_PORT=8000"

echo ============================================
echo   Web AI Agent  重启脚本  (端口 %AGENT_PORT%)
echo ============================================
echo.

rem ---------------------------------------------------------------------------
rem 1. Kill every old instance.
rem
rem Do NOT trust .ai\server.pid alone: before the single-instance guard existed,
rem dozens of leftover processes could pile up (once 112 of them, each polling
rem WeChat and answering the same message). Scan by command line instead.
rem Only python.exe running web_agent.py is touched.
rem
rem NOTE: keep this file ASCII in comments. cmd parses UTF-8 batch files byte by
rem byte, and multi-byte Chinese inside rem lines can be cut apart into commands.
rem ---------------------------------------------------------------------------
echo [1/3] 停止旧实例...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | Where-Object { $_.CommandLine -like '*web_agent.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1

rem Wait until the port is really released (not instant on Windows)
set /a tries=0
:waitport
set /a tries+=1
netstat -ano | findstr "LISTENING" | findstr ":%AGENT_PORT%" >nul 2>&1
if errorlevel 1 goto portfree
if %tries% geq 10 goto portbusy
timeout /t 1 /nobreak >nul
goto waitport

:portbusy
echo.
echo [error] 端口 %AGENT_PORT% 还被占着，而且不是 web_agent.py：
echo.
netstat -ano | findstr "LISTENING" | findstr ":%AGENT_PORT%"
echo.
echo         先关掉它（看上面最后一列的 PID）再运行本脚本，
echo         或者换端口： start.bat 8000
pause
exit /b 1

:portfree
echo       已清理，端口 %AGENT_PORT% 空闲

rem ---------------------------------------------------------------------------
rem 2. Resolve the real interpreter.
rem
rem "python" may be a launcher (PyManager) that spawns a child and exits.
rem Then this window would print "service exited" while the service is still
rem running. sys.executable is the actual binary, so the window stays attached.
rem ---------------------------------------------------------------------------
set "PY="
for /f "delims=" %%i in ('where python 2^>nul') do if not defined PY set "PY=%%i"

rem Fallback: the Python installed by the IDE runtime is not on PATH.
if not defined PY (
  if exist "%USERPROFILE%\.workbuddy\binaries\python\versions\3.14.3\python.exe" (
    set "PY=%USERPROFILE%\.workbuddy\binaries\python\versions\3.14.3\python.exe"
  )
)

if not defined PY (
  echo.
  echo [error] PATH 里找不到 python，先把 Python 加进环境变量。
  pause
  exit /b 1
)

rem ---------------------------------------------------------------------------
rem 2.5 Syntax check first.
rem
rem This project gets edited by the agent itself, and a stray character at
rem the top of a file (seen twice: "hognq", "nk") makes the whole service die
rem with a traceback that looks like a Python bug. Compile first, so the
rem broken file and its line number are printed right here instead.
rem ---------------------------------------------------------------------------
"%PY%" -m compileall -q src >nul 2>&1
if errorlevel 1 (
  echo.
  echo [error] 源码有语法错误，服务不会启动：
  echo.
  "%PY%" -m compileall -q src
  echo.
  pause
  exit /b 1
)

rem ---------------------------------------------------------------------------
rem 3. Start in the foreground: logs stay in this window, Ctrl+C stops it.
rem ---------------------------------------------------------------------------
echo [2/3] 启动服务... 端口 %AGENT_PORT%
echo       本机打开 http://127.0.0.1:%AGENT_PORT%
echo       局域网访问 http://本机IP:%AGENT_PORT%
echo       Ctrl+C 停止；想重启就关掉窗口再运行本脚本
echo       换端口：start.bat 8000   或   set AGENT_PORT=8000 后运行
echo.
echo [3/3] ----------------------------------------
echo.

"%PY%" src/web_agent.py
set code=%errorlevel%

echo.
echo --------------------------------------------
echo 服务已退出（exit code = %code%）
pause
