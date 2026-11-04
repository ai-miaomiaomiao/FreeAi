@echo off
rem ---------------------------------------------------------------------------
rem 以「应用模式」打开主界面：无地址栏、无标签栏，像独立应用。
rem
rem 为什么需要这个脚本：网页里的 window.open() 只能开浏览器标签页，
rem 浏览器**不允许**网页自己开 --app 窗口（安全限制）。要那种无边框窗口，
rem 只能由外部进程带 --app= 参数启动，或者安装 PWA 后点桌面图标。
rem
rem 端口跟 start.bat 保持一致（默认值见下方 set PORT 行），可以传参覆盖：
rem     app.bat 8000
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

set "PORT=%~1"
if not defined PORT set "PORT=8000"
set "URL=http://127.0.0.1:%PORT%/"

set "EXE="
for %%p in (
  "%ProgramFiles%\Google\Chrome\Application\chrome.exe"
  "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
  "%LocalAppData%\Google\Chrome\Application\chrome.exe"
  "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
  "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
) do (
  if not defined EXE if exist "%%~p" set "EXE=%%~p"
)

if not defined EXE (
  echo 没找到 Chrome / Edge，改用默认浏览器打开（会有地址栏）
  start "" "%URL%"
  exit /b 0
)

start "" "%EXE%" --app=%URL%
exit /b 0
