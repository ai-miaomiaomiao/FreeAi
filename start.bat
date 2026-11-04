@echo off
chcp 65001 >nul
setlocal

rem Always run from the script's own folder (also when double-clicked)
cd /d "%~dp0"

rem ---------------------------------------------------------------------------
rem 0. Port. Windows default is 8000 (Linux start.sh uses 8000).
rem    Priority: first command-line argument > AGENT_PORT env > 8000.
rem    Examples:  start.bat           -> 8000
rem               start.bat 8000      -> 8000
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
rem
rem Never trust "where python" blindly. %LOCALAPPDATA%\Microsoft\WindowsApps\
rem python.exe is the Microsoft Store app execution alias, not Python: it exits
rem with 9009 and runs nothing, yet it is usually listed first. Feeding it to
rem compileall used to produce a non-zero exit that got reported below as
rem "syntax error in source" -- the machine simply had no usable Python.
rem So: probe every candidate, keep the first one that really runs AND is 3.10+.
rem ---------------------------------------------------------------------------
set "PY="
set "PYVER="
set "PYLAST="
call :pick_py
if defined PY goto :py_ok

echo.
echo [error] 找不到可用的 Python 3.10 或更高版本。
if not defined PYLAST goto :py_hint
echo         找到过 %PYLAST%，但版本太低。本项目需要 3.10 及以上。
:py_hint
echo.
echo         Windows 上装一个：https://www.python.org/downloads/windows/
echo         安装时勾选 "Add python.exe to PATH"。
echo         注意 %LOCALAPPDATA%\Microsoft\WindowsApps\python.exe 是应用商店别名，
echo         不是真的 Python，本脚本会自动跳过它。
pause
exit /b 1

:py_ok

rem ---------------------------------------------------------------------------
rem 2.5 Syntax check first.
rem
rem This project gets edited by the agent itself, and a stray character at
rem the top of a file (seen twice: "hognq", "nk") makes the whole service die
rem with a traceback that looks like a Python bug. Compile first, so the
rem broken file and its line number are printed right here instead.
rem
rem Two changes from the old block, both about not lying to the reader:
rem  * the interpreter path + version is printed first (screenshot-able);
rem  * the compiler output is KEPT. The old code ran ">nul 2>&1" and then
rem    printed the fixed sentence "源码有语法错误", so a Store stub, a too-old
rem    Python and a genuinely broken file all looked identical.
rem ---------------------------------------------------------------------------
echo       解释器: %PY%  (Python %PYVER%)

"%PY%" -m compileall -q src 1>"%TEMP%\_webai_compile.txt" 2>&1
if not errorlevel 1 goto compile_ok

echo.
echo [error] 启动前检查没通过。下面是被解释器原样吐出来的内容：
echo.
type "%TEMP%\_webai_compile.txt"
echo.
findstr /i /c:"PermissionError" /c:"Access is denied" /c:"Permission denied" "%TEMP%\_webai_compile.txt" >nul
if errorlevel 1 goto compile_hint_syntax
echo       判断：当前目录不可写（编译缓存 src\__pycache__ 建不了）。
echo       把项目挪到可写目录（别放 C:\Program Files 下），或用管理员身份运行。
goto compile_hint_end
:compile_hint_syntax
echo       判断：上面若出现 SyntaxError，它会指明具体文件和行号，改那一行即可；
echo       若没有 SyntaxError 字样，请把整屏内容截图发给维护者。
:compile_hint_end
del "%TEMP%\_webai_compile.txt" >nul 2>&1
echo.
pause
exit /b 1

:compile_ok
del "%TEMP%\_webai_compile.txt" >nul 2>&1

rem ---------------------------------------------------------------------------
rem 3. Start in the foreground: logs stay in this window, Ctrl+C stops it.
rem ---------------------------------------------------------------------------
echo [2/3] 启动服务... 端口 %AGENT_PORT%
echo       本机打开 http://127.0.0.1:%AGENT_PORT%
echo       对外访问 http://YOUR-IP:%AGENT_PORT%
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
exit /b %code%

rem ===========================================================================
rem  :pick_py -- fill PY with the first candidate that runs and is Python 3.10+
rem
rem  Order: official launcher -> per-user installs -> PATH.
rem  3.10 is the real floor, not 3.8: src\agent_handoff.py (imported by
rem  web_agent.py at startup) annotates "dict | None" without
rem  "from __future__ import annotations", which is a TypeError on 3.8 / 3.9.
rem ===========================================================================
:pick_py
rem (1) The py launcher knows every installed version; ask it for the newest
rem     real binary. This also skips a PyManager "python.exe" that only spawns
rem     a child and exits (the window would then lose the running service).
for /f "usebackq delims=" %%i in (`py -3 -c "import sys;print(sys.executable)" 2^>nul`) do call :try_py "%%i"
if defined PY goto :eof
rem (2) Per-user installs. "Python314" sorts before "Python38", so the newer
rem     version wins when both live under the same folder layout.
for /d %%d in ("%LOCALAPPDATA%\Programs\Python\Python3*") do call :try_py "%%~d\python.exe"
if defined PY goto :eof
rem (3) Whatever is on PATH, minus the Microsoft Store alias.
for /f "delims=" %%i in ('where python 2^>nul') do (
  if not defined PY (
    echo %%i| findstr /i /c:"\WindowsApps\" >nul
    if errorlevel 1 call :try_py "%%i"
  )
)
goto :eof

rem ===========================================================================
rem  :try_py <path> -- probe a candidate; sets PY / PYVER or leaves them empty
rem
rem  Existence is not enough and the exit code is not a health signal: the
rem  Store alias and the PyManager launcher both "succeed" while running
rem  nothing. So ask the interpreter to print its own version and read that.
rem
rem  The version goes through a temp file on purpose. "for /f ... in ('"...")'"
rem  breaks: cmd strips the outer quotes when the command starts with one, so
rem  the interpreter path arrives mangled and nothing is ever read back.
rem ===========================================================================
:try_py
if defined PY goto :eof
if "%~1"=="" goto :eof
if not exist "%~1" goto :eof
set "VTMP=%TEMP%\_webai_pyver.txt"
del "%VTMP%" >nul 2>&1
"%~1" -c "import sys;print(sys.version_info[0],sys.version_info[1])" > "%VTMP%" 2>nul
set "VCAND="
if exist "%VTMP%" for /f "usebackq tokens=1,2" %%a in ("%VTMP%") do set "VCAND=%%a.%%b"
del "%VTMP%" >nul 2>&1
if not defined VCAND goto :eof
set "PYLAST=%~1 (Python %VCAND%)"
for /f "tokens=1,2 delims=." %%a in ("%VCAND%") do (
  if %%a GEQ 4 set "PY=%~1"
  if %%a EQU 3 if %%b GEQ 10 set "PY=%~1"
)
if defined PY set "PYVER=%VCAND%"
goto :eof
