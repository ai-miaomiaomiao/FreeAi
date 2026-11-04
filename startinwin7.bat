@echo off
rem ===========================================================================
rem  startinwin7.bat -- start the Web AI Agent on Windows 7
rem
rem  Why a separate script instead of start.bat:
rem    1. start.bat runs "chcp 65001", which the Win7 console cannot handle
rem       ("The system cannot write to the specified device.");
rem    2. start.bat kills leftovers with PowerShell Get-CimInstance, but Win7
rem       ships PowerShell 2.0 which only has Get-WmiObject. That call fails
rem       silently and the old instance keeps holding the port;
rem    3. start.bat falls back to a fixed Python 3.14 path, which does not
rem       exist on Win7 (3.8.10 is the newest Python that runs there).
rem
rem  Rules this file follows on purpose:
rem    * pure ASCII, no "chcp" -- cmd.exe parses .bat byte by byte, and mixed
rem      encodings in echo/rem lines break it;
rem    * no PowerShell at all: port cleanup uses netstat + taskkill, leftover
rem      cleanup uses "wmic ... where commandline like";
rem    * starts Python in the FOREGROUND, never with "start". If your company
rem      uses transparent file encryption, the whitelist is inherited through
rem      the parent-process chain (devenv.exe -> shell -> python.exe -> rg.exe);
rem      "start" detaches the child and breaks that chain. See the note at the
rem      bottom of this file.
rem
rem  Usage:  run it from a "cmd" window that belongs to Visual Studio 2019
rem          (View -> Terminal), or from a prompt started by devenv.exe:
rem              startinwin7.bat
rem              startinwin7.bat 8000
rem              set AGENT_PORT=8000 && startinwin7.bat
rem
rem  Prerequisites: run installtowin7.bat once (Python 3.8 + dependencies).
rem  This script only starts the service; it never installs anything.
rem ===========================================================================
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
set "ROOT=%~dp0"
set "PY="
set "PYVER="

if not exist "%ROOT%src\web_agent.py" (
  echo [error] src\web_agent.py not found.
  echo         Put this script in the project root folder and run it again.
  pause
  exit /b 1
)

rem ---------------------------------------------------------------------------
rem 0. Port. Same convention as start.bat:  argument > AGENT_PORT > 8000
rem ---------------------------------------------------------------------------
if not "%~1"=="" (
  echo %~1| findstr /r "^[0-9][0-9]*$" >nul
  if not errorlevel 1 set "AGENT_PORT=%~1"
)
rem Accept ONLY a pure number. Anything else - empty, a stray space left by
rem "set AGENT_PORT= &", junk inherited from a parent shell - falls back to
rem 8000. Without this check every netstat filter below degrades to ":",
rem which matches the entire connection table.
rem "echo(" instead of "echo ": it prints an empty line for an empty value
rem instead of the literal text "ECHO is off.".
echo(%AGENT_PORT%| findstr /r "^[0-9][0-9]*$" >nul
if errorlevel 1 set "AGENT_PORT=8000"

echo ============================================================
echo   Web AI Agent  /  Windows 7  (port %AGENT_PORT%)
echo ============================================================
echo.

rem ---------------------------------------------------------------------------
rem 1. Stop old instances.
rem
rem Two passes, because either one alone leaves a process behind:
rem   a) whoever is LISTENING on our port (the real blocker);
rem   b) any python.exe still running web_agent.py (a wedged instance that
rem      has not bound the port yet).
rem wmic is used instead of PowerShell: Get-CimInstance does not exist on the
rem PowerShell 2.0 that ships with Windows 7.
rem ---------------------------------------------------------------------------
echo [1/4] Stopping old instances ...

set "OLDPID="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /r /c:"LISTENING" ^| findstr /c:":%AGENT_PORT% "') do (
  set "OLDPID=%%P"
)
if defined OLDPID (
  echo        Port %AGENT_PORT% held by PID !OLDPID!, terminating ...
  taskkill /F /PID !OLDPID! >nul 2>&1
)

rem Scan by command line. BOTH python.exe and pythonw.exe must be covered:
rem watchdog.bat starts the service with pythonw.exe (no console window), and
rem a python.exe-only filter leaves that instance running and holding the port.
rem "like" with % wildcards is a wmic feature; the doubled %% is how a
rem literal % is written inside a batch file.
set "FOUND="
for /f "skip=1 tokens=1" %%P in ('wmic process where "(name='python.exe' or name='pythonw.exe') and commandline like '%%web_agent.py%%'" get ProcessId 2^>nul') do (
  rem %%P MUST be a number. When nothing matches, wmic still prints
  rem "No Instance(s) Available." and its first token would otherwise be
  rem taken for a PID - taskkill then fails silently while we cheerfully
  rem report "terminated".
  echo %%P| findstr /r "^[0-9][0-9]*$" >nul
  if not errorlevel 1 (
    set "FOUND=1"
    taskkill /F /PID %%P >nul 2>&1
  )
)
if defined FOUND echo        Leftover web_agent.py process(es) terminated.

rem Wait until the port is really released (not instant on Windows).
set /a tries=0
:waitport
set /a tries+=1
netstat -ano | findstr "LISTENING" | findstr ":%AGENT_PORT% " >nul 2>&1
if errorlevel 1 goto portfree
if %tries% geq 10 goto portbusy
rem "ping" instead of "timeout": timeout /t reads the console input buffer and
rem aborts with "input redirection is not supported" whenever stdin is not a
rem real console (running inside VS, a task runner, a pipe). ping always works.
ping -n 2 127.0.0.1 >nul
goto waitport

:portbusy
echo.
echo [error] Port %AGENT_PORT% is still in use by another program:
echo.
netstat -ano | findstr "LISTENING" | findstr ":%AGENT_PORT% "
echo.
echo         Close it (see the PID in the last column) and run this script
echo         again, or use another port:  startinwin7.bat 8000
echo.
pause
exit /b 1

:portfree
echo        Port %AGENT_PORT% is free.

rem ---------------------------------------------------------------------------
rem 2. Locate Python 3.7 / 3.8.
rem
rem Windows 7 cannot run Python 3.9+ at all, so a newer interpreter is
rem rejected here instead of failing later with a confusing traceback.
rem The version is read from stdout, not from the exit code: the "python.exe"
rem stubs in WindowsApps / PyManager exit 0 without running anything.
rem ---------------------------------------------------------------------------
echo [2/4] Looking for Python 3.7 or 3.8 ...
call :try_py_launcher
if not defined PY call :check_py "%LOCALAPPDATA%\Programs\Python\Python38\python.exe"
if not defined PY call :check_py "%LOCALAPPDATA%\Programs\Python\Python37\python.exe"
if not defined PY call :check_py "%ProgramFiles%\Python38\python.exe"
if not defined PY call :check_py "%ProgramFiles%\Python37\python.exe"
if not defined PY call :check_py "%ProgramFiles(x86)%\Python38\python.exe"
if not defined PY call :check_py "%ProgramFiles(x86)%\Python37\python.exe"
if not defined PY call :check_py "C:\Python38\python.exe"
if not defined PY call :check_py "C:\Python37\python.exe"
if not defined PY call :try_py_on_path
if not defined PY call :scan_user_profiles

if not defined PY (
  echo.
  echo [error] No usable Python 3.7 / 3.8 was found.
  echo         Run installtowin7.bat first - it installs Python and the
  echo         dependencies this service needs.
  echo.
  pause
  exit /b 1
)
echo        Using: %PY%  (Python %PYVER%)

rem ---------------------------------------------------------------------------
rem 3. Sanity checks before starting.
rem
rem compileall: this project is edited by the agent itself, and one stray
rem character makes the service die with a traceback that looks like a Python
rem bug. Compile first, so the broken file and line number are printed here.
rem ---------------------------------------------------------------------------
echo [3/4] Checking source ...
"%PY%" -m compileall -q "%ROOT%src" >nul 2>&1
if errorlevel 1 (
  echo.
  echo [error] The source does not compile with Python %PYVER%:
  echo.
  "%PY%" -m compileall -q "%ROOT%src"
  echo.
  pause
  exit /b 1
)

rem Dependencies: cryptography is only needed by the SSH / password features,
rem but a missing wheel shows up as a traceback in the middle of a request,
rem which is much harder to diagnose than a message here.
"%PY%" -c "import cryptography" >nul 2>&1
if errorlevel 1 (
  echo.
  echo [error] The "cryptography" module is missing for this interpreter.
  echo         Run installtowin7.bat first, or:
  echo           "%PY%" -m pip install "cryptography==3.3.2"
  echo.
  pause
  exit /b 1
)
echo        Source OK.

rem ---------------------------------------------------------------------------
rem 4. Start in the foreground: logs stay in this window, Ctrl+C stops it.
rem
rem Do NOT change this to "start ...". The service must stay a child of this
rem cmd.exe window so that any whitelist inherited from Visual Studio is kept
rem by python.exe and by the rg.exe it spawns.
rem ---------------------------------------------------------------------------
echo [4/4] Starting the service ...
echo       Local:  http://127.0.0.1:%AGENT_PORT%
echo       Press Ctrl+C to stop. Keep this window open.
echo.

"%PY%" "%ROOT%src\web_agent.py"
set "code=%errorlevel%"

echo.
echo --------------------------------------------
echo The service exited (exit code = %code%)
echo.
pause
exit /b %code%

rem ===========================================================================
rem  Subroutines
rem ===========================================================================

:check_py
rem %1 = candidate interpreter. Accepted only when it really is 3.7.x / 3.8.x.
if defined PY goto :eof
if "%~1"=="" goto :eof
if not exist "%~1" goto :eof
set "CAND="
rem Deliberately NOT for /f here: inside for /f a command that starts with a
rem quoted path loses its outer quotes and the line silently never matches.
rem A temp file + set /p is boring and always works.
"%~1" -c "import sys;print(sys.version.split()[0])" >"%TEMP%\wa7_pyver.txt" 2>nul
if not exist "%TEMP%\wa7_pyver.txt" goto :eof
set /p CAND=<"%TEMP%\wa7_pyver.txt"
if not defined CAND goto :eof
if not "%CAND:~0,3%"=="3.7" if not "%CAND:~0,3%"=="3.8" goto :eof
set "PY=%~1"
set "PYVER=%CAND%"
goto :eof

:try_py_launcher
if defined PY goto :eof
for /f "delims=" %%i in ('py -3.8 -c "import sys;print(sys.executable)" 2^>nul') do call :check_py "%%i"
if defined PY goto :eof
for /f "delims=" %%i in ('py -3.7 -c "import sys;print(sys.executable)" 2^>nul') do call :check_py "%%i"
goto :eof

:try_py_on_path
if defined PY goto :eof
for /f "delims=" %%i in ('where python 2^>nul') do call :check_py "%%i"
goto :eof

:scan_user_profiles
rem "where python" only sees the PATH of the account running this script. A
rem Win7 box often has the interpreter under another profile, so scan all.
if defined PY goto :eof
for /d %%u in ("C:\Users\*") do (
  call :check_py "%%~u\AppData\Local\Programs\Python\Python38\python.exe"
  call :check_py "%%~u\AppData\Local\Programs\Python\Python37\python.exe"
)
goto :eof

rem ===========================================================================
rem  Notes for whoever receives this file
rem ===========================================================================
rem
rem  1) Do NOT send agent_config.json with the project. It holds a real
rem     api_key, a plain-text password and internal endpoints. Send
rem     agent_config.example.json and let your colleague fill it in.
rem
rem  2) Transparent file encryption: this script must be started from a shell
rem     that belongs to the whitelisted process tree (Visual Studio 2019:
rem     View -> Terminal). Double-clicking it starts it from explorer.exe,
rem     which usually is NOT whitelisted - read_file / search_text would then
rem     silently return ciphertext instead of plain text.
rem
rem  3) "View -> Terminal" on Windows 7 may refuse to open (it relies on
rem     ConPTY, which exists from Windows 10 1809). If it does not open, the
rem     whitelist-inheritance route is unavailable on this machine.
rem ===========================================================================
