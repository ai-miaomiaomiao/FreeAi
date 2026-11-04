@echo off
rem ---------------------------------------------------------------------------
rem ai.bat - command line entry for the Web AI Agent (calls src\cli.py)
rem
rem   ai "hello"              new session, one question
rem   ai -s "and then?"       reuse the active session of the current workspace
rem   ai -s --sid s3 "..."    reuse session s3
rem   ai -m debug "..."       pick an agent mode (default: standard)
rem   ai --help               full options
rem
rem Requires the server to be running already (start.bat / ./start.sh).
rem Put this folder into PATH, then "ai" works from any directory.
rem
rem NOTE: keep this file pure ASCII. cmd parses batch files byte by byte;
rem multi-byte characters can cut a "rem" line into a bogus command.
rem ---------------------------------------------------------------------------
chcp 65001 >nul
setlocal

set "PY=python"
%PY% -c "import sys" >nul 2>nul
if errorlevel 1 set "PY=py"
%PY% -c "import sys" >nul 2>nul
if errorlevel 1 goto nopython

rem -X utf8: force UTF-8 stdio so Chinese in argv and in the reply is not mangled
%PY% -X utf8 "%~dp0src\cli.py" %*
set "RC=%errorlevel%"
endlocal & exit /b %RC%

:nopython
echo [error] Python 3 not found in PATH. Install it, or edit ai.bat and set PY.
endlocal & exit /b 9009
