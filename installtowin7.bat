@echo off
rem ===========================================================================
rem  installtowin7.bat -- bootstrap the Web AI Agent on Windows 7
rem
rem  Why a separate script: Windows 7 tops out at Python 3.8 (3.8.10 is the
rem  last build with an installer). Python 3.7.9 is also accepted, because a
rem  lot of Win7 boxes already have it. start.bat resolves "python" from PATH
rem  and falls back to the 3.14 runtime, which does not exist on 7.
rem
rem  Rules this file follows on purpose:
rem    * pure ASCII, no "chcp 65001" -- cmd.exe parses .bat byte by byte and
rem      the Win7 console is known to fail with "The system cannot write to
rem      the specified device." once the code page is 65001;
rem    * per-user install, no admin rights needed;
rem    * every version pin is the newest one that still supports Python 3.7,
rem      so it works unchanged on 3.7 and 3.8 (cryptography 3.4+ needs Rust
rem      and dropped Win7, so it is pinned).
rem
rem  Usage:  double-click it, or run:  installtowin7.bat
rem  Optional: drop python-3.7.9.exe or python-3.8.10.exe next to this file
rem  and the script installs it silently instead of asking you to download it.
rem ===========================================================================
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
set "ROOT=%~dp0"
set "PY="
set "PYVER="
set "FAILED="

echo ============================================================
echo   Web AI Agent  /  Windows 7 setup
echo ============================================================
echo.

if not exist "%ROOT%src\web_agent.py" (
  echo [error] src\web_agent.py not found.
  echo         Put this script in the project root folder and run it again.
  pause
  exit /b 1
)

rem ---------------------------------------------------------------------------
rem [1/6] Sanity checks
rem ---------------------------------------------------------------------------
ver | findstr /r /c:"Version 6\.1\." >nul
if errorlevel 1 (
  echo [1/6] OS: not Windows 7 - continuing anyway.
) else (
  echo [1/6] OS: Windows 7 detected.
)
set "ARCH=32"
if /i "%PROCESSOR_ARCHITECTURE%"=="AMD64" set "ARCH=64"
if /i "%PROCESSOR_ARCHITEW6432%"=="AMD64" set "ARCH=64"
echo        Architecture: %ARCH%-bit

rem ---------------------------------------------------------------------------
rem [2/6] Find (or install) Python 3.7 / 3.8
rem
rem 3.8 is preferred (newer stdlib), 3.7 is accepted because it is the most
rem common leftover on Win7 machines. Both are checked for the exact minor
rem version - a 3.9+ or a broken install is rejected here, not later.
rem ---------------------------------------------------------------------------
echo.
echo [2/6] Looking for Python 3.7 or 3.8 ...
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
rem Still nothing? Scan every user profile: "where python" only looks at the
rem PATH of the account running this script, and the interpreter is often
rem installed under another account (C:\Users\app\...).
if not defined PY call :scan_user_profiles

if not defined PY if exist "%ROOT%python-3.8.10.exe" (
  echo        Bundled installer found, installing silently ...
  "%ROOT%python-3.8.10.exe" /quiet InstallAllUsers=0 PrependPath=1 Include_test=0 Include_launcher=1 Include_pip=1
  call :check_py "%LOCALAPPDATA%\Programs\Python\Python38\python.exe"
  call :check_py "C:\Python38\python.exe"
)
if not defined PY if exist "%ROOT%python-3.7.9.exe" (
  echo        Bundled installer found, installing silently ...
  "%ROOT%python-3.7.9.exe" /quiet InstallAllUsers=0 PrependPath=1 Include_test=0 Include_launcher=1 Include_pip=1
  call :check_py "%LOCALAPPDATA%\Programs\Python\Python37\python.exe"
  call :check_py "C:\Python37\python.exe"
)

if not defined PY (
  echo.
  echo [error] No usable Python 3.7 / 3.8 was found.
  echo.
  echo   Windows 7 cannot run Python 3.9 or newer. Download one of these:
  echo   - 3.8.10  preferred, recommended:
  if "%ARCH%"=="64" (
    echo       https://www.python.org/ftp/python/3.8.10/python-3.8.10-amd64.exe
  ) else (
    echo       https://www.python.org/ftp/python/3.8.10/python-3.8.10.exe
  )
  echo   - 3.7.9  only if 3.8 cannot be installed:
  if "%ARCH%"=="64" (
    echo       https://www.python.org/ftp/python/3.7.9/python-3.7.9-amd64.exe
  ) else (
    echo       https://www.python.org/ftp/python/3.7.9/python-3.7.9.exe
  )
  echo.
  echo   During setup tick "Add Python to PATH".
  echo   Or save the installer in this folder as python-3.8.10.exe
  echo   ^(or python-3.7.9.exe^) and run this script again.
  pause
  exit /b 1
)

if not defined PYVER set "PYVER=?"
echo        Using: %PY%   ^(Python %PYVER%^)
if "%PYVER:~0,1%"=="3" if "%PYVER:~2,1%"=="7" (
  echo        Note: 3.7 is end-of-life. It works for this project, but 3.8.10
  echo              is still downloadable for Win7 and is the better choice.
)

rem ---------------------------------------------------------------------------
rem [3/6] pip
rem ---------------------------------------------------------------------------
echo.
echo [3/6] Checking pip ...
"%PY%" -m pip --version >nul 2>&1
if errorlevel 1 (
  echo        pip missing, bootstrapping with ensurepip ...
  "%PY%" -m ensurepip --default-pip >nul 2>&1
)
"%PY%" -m pip --version >nul 2>&1
if errorlevel 1 (
  echo [error] pip is not usable for this Python.
  echo         Try manually:  "%PY%" -m ensurepip
  pause
  exit /b 1
)
echo        Updating pip / setuptools / wheel ...
rem pip 24.1+ / setuptools 68+ / wheel 0.43+ all require Python 3.8, so on a
rem 3.7 interpreter these are already the last usable versions.
"%PY%" -m pip install --disable-pip-version-check -q "pip<24.1" "setuptools<68" "wheel<0.43" >nul 2>&1
if errorlevel 1 echo        [warn] pip upgrade failed - probably offline, continuing.

rem ---------------------------------------------------------------------------
rem [4/6] Dependencies
rem ---------------------------------------------------------------------------
echo.
echo [4/6] Installing dependencies ...
echo        cryptography is pinned to 3.3.2: that is the last release with
echo        Windows 7 wheels ^(4.x requires Rust and Win8.1+^).
rem Every pin below is the newest release that still supports Python 3.7, so
rem the same line works on 3.7 and 3.8.
"%PY%" -m pip install --disable-pip-version-check --prefer-binary ^
  "requests>=2.25,<2.32" ^
  "chardet>=4,<6" ^
  "qrcode>=7,<8" ^
  "cryptography==3.3.2" ^
  "paramiko>=2.7,<3.5"
if errorlevel 1 (
  set "FAILED=1"
  echo.
  echo [warn] Core dependency install failed.
  echo        If this is a network problem, retry later or point pip at a
  echo        mirror, e.g.:
  echo          "%PY%" -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple
  echo            "requests" "chardet" "qrcode" "cryptography==3.3.2" "paramiko"
)

echo        Optional: WeChat bridge ^(wcferry^) ...
"%PY%" -m pip install --disable-pip-version-check -q "wcferry" >nul 2>&1
if errorlevel 1 (
  echo        [warn] wcferry not installed - the WeChat assistant stays off,
  echo               everything else keeps working.
) else (
  echo        wcferry ok.
)

rem ---------------------------------------------------------------------------
rem [5/6] Source check
rem ---------------------------------------------------------------------------
echo.
echo [5/6] Checking source under this interpreter ...
"%PY%" -m compileall -q "%ROOT%src" >"%TEMP%\wa_compile.log" 2>&1
if errorlevel 1 (
  echo [error] The source does not compile with Python %PYVER%:
  echo.
  type "%TEMP%\wa_compile.log"
  echo.
  echo         A file listed above uses syntax newer than this Python.
  pause
  exit /b 1
)
echo        Syntax OK.
rem Import check: compileall only catches syntax. This catches "syntax is fine
rem but the module blows up at import time" (a 3.9-only API, a missing wheel).
rem A failure here is a warning, not a hard stop: the service may still start.
"%PY%" -c "import sys;sys.path.insert(0,r'%ROOT%src');import web_agent" >"%TEMP%\wa_import.log" 2>&1
if errorlevel 1 (
  echo        [warn] import web_agent failed - details in %TEMP%\wa_import.log
  type "%TEMP%\wa_import.log"
) else (
  echo        Import OK.
)

rem ---------------------------------------------------------------------------
rem [6/6] Config
rem ---------------------------------------------------------------------------
echo.
echo [6/6] Configuration ...
if exist "%ROOT%agent_config.json" (
  echo        agent_config.json exists - kept untouched.
) else (
  if exist "%ROOT%agent_config.example.json" (
    copy /y "%ROOT%agent_config.example.json" "%ROOT%agent_config.json" >nul
    echo        Created agent_config.json from the example.
    echo        Edit it first: base_url / api_key / model / port.
  ) else (
    echo        [warn] agent_config.example.json is missing.
  )
)

echo.
echo ============================================================
if defined FAILED (
  echo   Setup finished WITH WARNINGS - see the messages above.
) else (
  echo   Setup finished.
)
echo ============================================================
echo.
echo   Next steps:
echo     1. edit agent_config.json ^(api_key, model, port^)
echo     2. open a NEW cmd window and run start.bat
echo        ^(PATH changes only apply to new windows^)
echo.
echo   If you ever see "The system cannot write to the specified device."
echo   the console code page is the culprit: run "chcp 936" in that window.
echo.
set "ANS="
set /p "ANS=Start the agent now in a new window? [Y/N] "
if /i "%ANS%"=="Y" (
  start "Web AI Agent" "%PY%" "%ROOT%src\web_agent.py"
)
echo.
pause
exit /b 0

rem ===========================================================================
rem  Subroutines
rem ===========================================================================

:check_py
rem %1 = candidate interpreter. Accepted only when it really is 3.7.x / 3.8.x.
rem 3.9+ is rejected here on purpose: it cannot even install on Win7, and a
rem silent fallback would only fail much later with a confusing traceback.
rem The version is read from stdout, not from exit code: the "python.exe"
rem stubs in WindowsApps / PyManager exit 0 without running anything, and an
rem exit-code check would happily accept one of those as our interpreter.
if defined PY goto :eof
if "%~1"=="" goto :eof
if not exist "%~1" goto :eof
set "CAND="
rem Deliberately NOT for /f here: inside for /f a command that starts with a
rem quoted path loses its outer quotes and the line silently never matches.
rem A temp file + set /p is boring and always works.
"%~1" -c "import sys;print(sys.version.split()[0])" >"%TEMP%\wa_pyver.txt" 2>nul
if not exist "%TEMP%\wa_pyver.txt" goto :eof
set /p CAND=<"%TEMP%\wa_pyver.txt"
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
rem "where python" sees the PATH of the account running this script only.
rem A Win7 box often has the interpreter under another profile
rem (C:\Users\app\AppData\Local\Programs\Python\Python37\...), so scan all.
if defined PY goto :eof
for /d %%u in ("C:\Users\*") do (
  call :check_py "%%~u\AppData\Local\Programs\Python\Python38\python.exe"
  call :check_py "%%~u\AppData\Local\Programs\Python\Python37\python.exe"
)
goto :eof
