@echo off
rem ===========================================================================
rem  install_autostart.bat  --  register / remove boot autostart for the agent
rem
rem  Usage:
rem    install_autostart.bat install     register scheduled task(s) + start now
rem    install_autostart.bat uninstall   remove task(s) + stop the watchdog
rem    install_autostart.bat status      show task / process / port state
rem    (no argument)                     same as install, plus pause at the end
rem
rem  Port: 8000 by default (same as start.bat). Override with the 2nd argument
rem  or the AGENT_PORT environment variable, e.g.
rem    install_autostart.bat install 8000
rem    set AGENT_PORT=8000 ^&^& install_autostart.bat install
rem
rem  A scheduled task starts src\watchdog.py via pythonw.exe (windowless) at
rem  logon and, if allowed, at boot. The watchdog keeps web_agent.py alive and
rem  restarts it within seconds whenever it dies -- see src\watchdog.py.
rem
rem  NOTE: keep every line of this file ASCII. cmd parses UTF-8 batch files
rem  byte by byte; multi-byte characters can be cut apart into commands
rem  (this actually happened here and broke the whole script).
rem ===========================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "TASK=WebAIAgent"
set "ACTION=%~1"
if "%ACTION%"=="" set "ACTION=install"
rem Only pause when double-clicked (no arguments). With an action argument the
rem script is being driven by automation and must not block on a keypress.
if not "%~1"=="" set "NO_PAUSE=1"

rem -- port: 2nd argument > AGENT_PORT env > 8000 ----------------------------
if not "%~2"=="" (
  echo %~2| findstr /r "^[0-9][0-9]*$" >nul
  if not errorlevel 1 set "AGENT_PORT=%~2"
)
if not defined AGENT_PORT set "AGENT_PORT=8000"
if /i "%ACTION%"=="on" set "ACTION=install"
if /i "%ACTION%"=="off" set "ACTION=uninstall"
if /i "%ACTION%"=="remove" set "ACTION=uninstall"
if /i "%ACTION%"=="query" set "ACTION=status"

rem -- resolve pythonw.exe (same lookup order as watchdog.bat) -----------------
rem
rem WARNING: `where python` frequently returns
rem   C:\Users\<you>\AppData\Local\Microsoft\WindowsApps\python.exe
rem That is NOT Python -- it is a Microsoft Store "app execution alias". With no
rem Store Python installed it exits with 9009 and runs nothing at all, so the
rem scheduled task looks perfectly healthy while the watchdog never starts.
rem Every PATH hit must therefore be screened against \WindowsApps\.
rem The lookup lives in :find_pyw so that :ensure_python can run it, install a
rem fresh interpreter, and probe the install roots again.
set "PYW="
call :find_pyw
set "WD=%~dp0src\watchdog.py"
rem Launchers generated at install time; they pin AGENT_PORT for the task.
rem WRAP     : logon trigger -> resident watchdog (also owns the tray icon)
rem WRAPBOOT : boot trigger  -> watchdog --once (service only, NO tray: at boot
rem            there may be no interactive session yet, and a tray started there
rem            can never appear in the notification area)
set "WRAP=%~dp0.ai\autostart_watchdog.cmd"
set "WRAPBOOT=%~dp0.ai\autostart_boot.cmd"

echo ============================================
echo   Web AI Agent autostart manager
echo ============================================
echo   task     : %TASK%
echo   script   : %WD%
if defined PYW (
  echo   interp   : %PYW%
) else (
  echo   interp   : none usable -- will be installed on demand
)
echo   port     : %AGENT_PORT%
echo   action   : %ACTION%
echo.

if /i "%ACTION%"=="status" goto :do_status
if /i "%ACTION%"=="uninstall" goto :do_uninstall
if /i "%ACTION%"=="install" goto :do_install

echo [error] unknown action: %ACTION%
echo         usage: install_autostart.bat [install^|uninstall^|status]
call :maybe_pause
exit /b 1

:do_install
if not exist "%WD%" (
  echo [error] not found: %WD%
  call :maybe_pause
  exit /b 1
)
rem No interpreter at all: this is the "double-click and nothing happens"
rem case on a colleague's machine. Try to install one instead of just dying.
if not defined PYW (
  call :ensure_python
)
if not defined PYW (
  echo [error] No usable Python 3.8+ found, and the automatic install failed.
  echo         See the message above: download the installer by hand, run it,
  echo         then run this script again.
  call :maybe_pause
  exit /b 1
)

rem A scheduled task does NOT inherit this console's environment, so the port
rem has to travel with the task. Wrap it in a tiny launcher that sets
rem AGENT_PORT first -- watchdog.py, tray.py and web_agent.py all read it.
rem (Trying to inline `cmd /c set AGENT_PORT=...` in /tr needs triple-escaped
rem  quotes and breaks easily; a generated .cmd file is boring and reliable.)
if not exist "%~dp0.ai" mkdir "%~dp0.ai" >nul 2>&1
> "%WRAP%" echo @echo off
rem Unquoted on purpose: `status` reads this line back by splitting on '=',
rem and a quoted form would leave a stray '"' in the parsed value.
>> "%WRAP%" echo set AGENT_PORT=%AGENT_PORT%
rem `start` so the launcher exits at once -- otherwise the cmd window stays
rem alive as long as the watchdog runs (visible black box at every logon).
rem No /b on purpose: the launcher's console dies right after, and a child
rem still attached to it can be killed by the console close event. pythonw is
rem a GUI-subsystem binary, so a plain `start` opens no window.
>> "%WRAP%" echo start "" "%PYW%" "%WD%"
if not exist "%WRAP%" (
  echo [error] cannot write launcher: %WRAP%
  call :maybe_pause
  exit /b 1
)

rem Boot-time launcher: --once only makes sure the service is up, then exits.
rem A resident watchdog started at boot may live in a non-interactive session,
rem where the tray icon can never show; the logon trigger owns the tray.
> "%WRAPBOOT%" echo @echo off
>> "%WRAPBOOT%" echo set AGENT_PORT=%AGENT_PORT%
>> "%WRAPBOOT%" echo start "" "%PYW%" "%WD%" --once
if not exist "%WRAPBOOT%" (
  echo [error] cannot write launcher: %WRAPBOOT%
  call :maybe_pause
  exit /b 1
)

rem /F overwrites an existing task so re-running this script is always safe.
echo [1/3] creating logon task ...
schtasks /create /tn "%TASK%" /tr "\"%WRAP%\"" /sc ONLOGON /rl HIGHEST /f
if errorlevel 1 (
  echo.
  echo [error] task creation failed. Likely causes:
  echo         1. not admin -- run this script as administrator
  echo         2. group policy blocks scheduled tasks
  call :maybe_pause
  exit /b 1
)

rem ONSTART needs admin; try it, do not fail the whole install if denied.
rem NOTE: boot trigger uses WRAPBOOT (--once). A resident watchdog at boot can
rem land in session 0; the tray icon would then be added to a desktop nobody
rem sees, and the logon-time watchdog would skip starting its own tray because
rem one already exists. Service at boot, tray at logon.
echo [2/3] adding boot trigger (service only, no tray) ...
schtasks /create /tn "%TASK%-boot" /tr "\"%WRAPBOOT%\"" /sc ONSTART /rl HIGHEST /delay 0000:30 /f >nul 2>&1
if errorlevel 1 (
  echo       skipped: needs admin. Logon trigger alone is still fine.
) else (
  echo       added.
)

rem Start the watchdog now instead of waiting for the next logon, but only if
rem nothing is running yet -- it also has its own single-instance guard.
echo [3/3] starting watchdog now ...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"name='pythonw.exe' or name='python.exe'\" | Where-Object { $_.CommandLine -like '*watchdog.py*' } | Select-Object -First 1 -ExpandProperty ProcessId" > "%TEMP%\_wact.txt" 2>nul
set /p RUNPID=<"%TEMP%\_wact.txt"
del "%TEMP%\_wact.txt" 2>nul
if defined RUNPID (
  echo       watchdog already running, pid %RUNPID% -- it keeps its old port.
  echo.
  echo       If you changed the port, the old watchdog keeps probing the OLD one,
  echo       so the service is killed and restarted forever (red tray icon looping).
  echo       Fix it now:
  echo         install_autostart.bat uninstall
  echo         install_autostart.bat install %AGENT_PORT%
  echo       and make sure agent_config.json "port" is the same number.
) else (
  schtasks /run /tn "%TASK%" >nul 2>&1
  if errorlevel 1 (
    rem fallback: the launcher carries AGENT_PORT itself
    start "" /b "%WRAP%"
  )
  echo       started.
)

echo.
echo --------------------------------------------
echo Done. After boot/logon the watchdog auto-starts; if web_agent.py dies it
echo is restarted within seconds. Keep-alive evidence:
echo   port         : %AGENT_PORT%  (launcher: %WRAP%)
echo   status       : install_autostart.bat status
echo   watchdog log : type .ai\watchdog.log
echo   server log   : type .ai\watchdog-child.log
echo   disable      : install_autostart.bat uninstall
echo.
echo NOTE: a background watchdog is now resident. Do NOT also run start.bat;
echo       its single-instance lock makes the second copy exit at once.
echo --------------------------------------------
call :maybe_pause
exit /b 0

:do_uninstall
echo [1/2] deleting scheduled tasks ...
schtasks /delete /tn "%TASK%" /f >nul 2>&1
if errorlevel 1 (
  echo       task %TASK% not present.
) else (
  echo       deleted %TASK%.
)
schtasks /delete /tn "%TASK%-boot" /f >nul 2>&1
if not errorlevel 1 echo       deleted %TASK%-boot.

echo [2/2] stopping the watchdog process ...
rem Only the watchdog is killed here. The running service is a separate
rem process and keeps serving until you stop it yourself.
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"name='pythonw.exe' or name='python.exe'\" | Where-Object { $_.CommandLine -like '*watchdog.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
echo       stopped (running service not affected).
rem the generated port launchers are useless once the tasks are gone
if exist "%WRAP%" del "%WRAP%" >nul 2>&1
if exist "%WRAPBOOT%" del "%WRAPBOOT%" >nul 2>&1

echo.
echo --------------------------------------------
echo Autostart removed. To start manually use start.bat (foreground, shows logs).
echo To also stop the service:
echo   powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" ^| Where-Object { $_.CommandLine -like '*web_agent.py*' } ^| ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo --------------------------------------------
call :maybe_pause
exit /b 0

:do_status
rem Show the port the installed task actually uses (from the launcher), not
rem just this console's default -- otherwise "status" lies after a 8000 install.
set "STATUS_PORT=%AGENT_PORT%"
if exist "%WRAP%" (
  for /f "tokens=2 delims==" %%a in ('findstr /i "AGENT_PORT" "%WRAP%"') do set "STATUS_PORT=%%a"
)
echo [tasks]
schtasks /query /tn "%TASK%" /fo LIST 2>nul | findstr /i "TaskName Status Next"
schtasks /query /tn "%TASK%-boot" /fo LIST 2>nul | findstr /i "TaskName Status Next"
echo.
echo [processes]
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"name='pythonw.exe' or name='python.exe'\" | Where-Object { $_.CommandLine -like '*watchdog.py*' -or $_.CommandLine -like '*web_agent.py*' } | Select-Object ProcessId,Name,@{n='Cmd';e={ if ($_.CommandLine -like '*watchdog.py*') {'watchdog'} else {'web_agent'} }} | Format-Table -AutoSize"
echo [port %STATUS_PORT%]
netstat -ano | findstr "LISTENING" | findstr ":%STATUS_PORT%"
echo.
call :maybe_pause
exit /b 0

rem ===========================================================================
rem  :find_pyw -- locate a usable interpreter (sets PYW, may leave it empty)
rem
rem  Same order as watchdog.bat. Every PATH hit must be screened against
rem  \WindowsApps\ first: that is the Microsoft Store "app execution alias",
rem  not Python. It exits with 9009 and runs nothing, so the task looks
rem  healthy while the watchdog never starts.
rem ===========================================================================
:find_pyw
set "PYW="
rem Real per-user installs: %LOCALAPPDATA%\Programs\Python\Python3xx\
rem (There used to be a hard-coded path to the developer's own IDE runtime
rem  here -- useless on any other machine and a leak of that environment.)
if not defined PYW (
  for /d %%d in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
    if not defined PYW if exist "%%~d\pythonw.exe" set "PYW=%%~d\pythonw.exe"
  )
)
if not defined PYW (
  for /f "delims=" %%i in ('where pythonw 2^>nul') do (
    if not defined PYW (
      echo %%i| findstr /i /c:"\WindowsApps\" >nul
      if errorlevel 1 set "PYW=%%i"
    )
  )
)
if not defined PYW (
  for /f "delims=" %%i in ('where python 2^>nul') do (
    if not defined PYW (
      echo %%i| findstr /i /c:"\WindowsApps\" >nul
      if errorlevel 1 set "PYW=%%i"
    )
  )
)
rem Last resort: only python.exe found. Prefer its pythonw.exe sibling so the
rem resident watchdog does not leave a console window on the desktop. The
rem Store stub is screened here too -- otherwise the probe below would launch
rem it and pop up the Microsoft Store on a machine that has no Python at all.
if not defined PYW (
  for /f "delims=" %%i in ('where python 2^>nul') do (
    if not defined PYW (
      echo %%i| findstr /i /c:"\WindowsApps\" >nul
      if errorlevel 1 set "PYW=%%i"
    )
  )
)
if defined PYW (
  for %%f in ("%PYW%") do (
    if /i "%%~nxf"=="python.exe" if exist "%%~dpfpythonw.exe" set "PYW=%%~dpfpythonw.exe"
  )
)
call :verify_pyw
exit /b 0

rem ===========================================================================
rem  :verify_pyw -- reject an interpreter that cannot actually run
rem
rem  Exit code is useless as a health signal: the Store alias and the PyManager
rem  launcher both exit 0 while doing nothing. So ask the interpreter to print
rem  its own major/minor version and read that; unreadable output counts as
rem  unusable. 3.10 is the floor -- src\agent_handoff.py annotates "dict | None"
rem  without "from __future__ import annotations", a TypeError on 3.8 / 3.9.
rem  (3.8.10 used to be listed here as the floor only because it is the last
rem  build with a Win7 installer; that is a different question -- see
rem  installtowin7.bat for the Win7 path.)
rem ===========================================================================
:verify_pyw
if not defined PYW exit /b 0
set "VPY=%PYW%"
for %%f in ("%PYW%") do if exist "%%~dpfpython.exe" set "VPY=%%~dpfpython.exe"
set "VTMP=%TEMP%\_webai_pyver.txt"
del "%VTMP%" >nul 2>&1
"%VPY%" -c "import sys;print(sys.version_info[0],sys.version_info[1])" > "%VTMP%" 2>nul
set "PYOK="
if exist "%VTMP%" (
  for /f "usebackq tokens=1,2" %%a in ("%VTMP%") do (
    echo %%a| findstr /r "^[0-9][0-9]*$" >nul
    if not errorlevel 1 (
      if %%a GEQ 4 set "PYOK=1"
      if %%a EQU 3 if %%b GEQ 10 set "PYOK=1"
    )
  )
  del "%VTMP%" >nul 2>&1
)
if not defined PYOK (
  echo        [warn] installed but not a usable Python 3.10+: %VPY%
  set "PYW="
)
exit /b 0

rem ===========================================================================
rem  :ensure_python -- download and install Python when nothing usable exists
rem
rem  This is the ONLY place in the project that fetches anything from the
rem  network (see platforms\README.md). Two choices are deliberate:
rem
rem  * certutil, NOT PowerShell Invoke-WebRequest. Win7 ships PowerShell 2.0,
rem    where that cmdlet does not exist, and newer PS still honours the
rem    machine-wide .NET TLS default, which is TLS 1.0 on unpatched boxes.
rem    certutil exists on Win7 and up and brings its own HTTP stack.
rem  * InstallAllUsers=0: a per-user install needs no admin, so no UAC prompt,
rem    which is what makes double-clicking this script viable for a colleague.
rem
rem  Returns with PYW set on success, left empty on failure (caller prints the
rem  manual fallback).
rem ===========================================================================
:ensure_python
echo.
echo        No usable Python found -- installing it automatically ...
echo        needs internet, about 25 MB. Per-user install: no admin, no UAC.
rem 64-bit Windows running a 32-bit cmd reports PROCESSOR_ARCHITECTURE=x86, so
rem ARCHITEW6432 has to be consulted too -- otherwise a 64-bit box silently
rem gets the 32-bit installer.
set "PYARCH=amd64"
if /i not "%PROCESSOR_ARCHITECTURE%"=="AMD64" if not defined PROCESSOR_ARCHITEW6432 set "PYARCH=win32"
rem Win7 (6.x) cannot run 3.9+ at all: its last installer build is 3.8.10.
set "OSMAJ="
for /f "tokens=4 delims=. " %%v in ('ver') do if not defined OSMAJ set "OSMAJ=%%v"
set "PYTAG=3.12.10"
set "PYDIG=312"
if "%OSMAJ%"=="6" (
  set "PYTAG=3.8.10"
  set "PYDIG=38"
)
set "PYURL=https://www.python.org/ftp/python/%PYTAG%/python-%PYTAG%-%PYARCH%.exe"
set "PYEXE=%TEMP%\webai-python-%PYTAG%-%PYARCH%.exe"
rem A previous run may have left a truncated download; refetch anything that is
rem implausibly small instead of trying to run a broken installer. Only the
rem %TEMP% copy is cleaned -- a file the user dropped here is never deleted.
if exist "%PYEXE%" for %%f in ("%PYEXE%") do if %%~zf LSS 20000000 del "%PYEXE%" >nul 2>&1
rem Offline path: if the official installer sits next to this script (same
rem naming convention as installtowin7.bat), use it and skip the download --
rem that is the only way this works on a machine with no internet access.
if exist "%~dp0python-%PYTAG%-%PYARCH%.exe" set "PYEXE=%~dp0python-%PYTAG%-%PYARCH%.exe"
if not exist "%PYEXE%" (
  echo        downloading: %PYURL%
  certutil -urlcache -split -f "%PYURL%" "%PYEXE%" >nul 2>&1
)
if not exist "%PYEXE%" (
  echo        [error] download failed.
  goto :ensure_py_fail
)
echo        installing silently (this can take a minute) ...
set "RC=?"
start /wait "" "%PYEXE%" /quiet InstallAllUsers=0 PrependPath=1 Include_test=0 Include_launcher=1 Include_pip=1
set "RC=%ERRORLEVEL%"
rem 0 = fine, 3010 = fine but wants a reboot. Trust the probe below, not the code.
rem `where python` would NOT see the new interpreter here: PrependPath only
rem affects processes started afterwards, and this cmd.exe keeps its old PATH.
rem So probe the known install roots directly instead of re-running :find_pyw.
set "PYW="
if exist "%LOCALAPPDATA%\Programs\Python\Python%PYDIG%\pythonw.exe" set "PYW=%LOCALAPPDATA%\Programs\Python\Python%PYDIG%\pythonw.exe"
if not defined PYW if exist "%ProgramFiles%\Python%PYDIG%\pythonw.exe" set "PYW=%ProgramFiles%\Python%PYDIG%\pythonw.exe"
if not defined PYW if exist "%ProgramFiles(x86)%\Python%PYDIG%\pythonw.exe" set "PYW=%ProgramFiles(x86)%\Python%PYDIG%\pythonw.exe"
if not defined PYW (
  echo        [error] installer reported exit code %RC%
)
rem Last chance on both failure paths: it may have landed in a folder we did
rem not predict (the version folder name comes from PYDIG). :find_pyw rescans
rem the real install roots and also re-verifies.
if not defined PYW call :find_pyw
if not defined PYW goto :ensure_py_fail
echo        using: %PYW%
exit /b 0

:ensure_py_fail
echo.
echo   Install Python by hand, then run this script again:
echo     %PYURL%
echo   During setup tick "Add Python to PATH".
echo.
echo   Offline alternative: save that installer next to this script as
echo     python-%PYTAG%-%PYARCH%.exe
echo   and re-run -- it is used instead of downloading.
echo   The silent switches this script passes are:
echo     /quiet InstallAllUsers=0 PrependPath=1 Include_test=0
echo     Include_launcher=1 Include_pip=1
exit /b 1

rem -- pause only when double-clicked (no argument), never in automation -------
:maybe_pause
if defined NO_PAUSE exit /b 0
if "%~1"=="nopause" exit /b 0
if "%~2"=="nopause" exit /b 0
pause
exit /b 0
