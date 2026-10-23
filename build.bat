@echo off
setlocal
cd /d "%~dp0"
title Building Claude's Trackpad

rem ---- Find Python 3.9+ with tkinter
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY (
  echo Python isn't installed. Get it from https://www.python.org/downloads/ and run this again.
  goto :fail
)
%PY% -c "import sys; sys.exit(sys.version_info < (3, 9))" || (
  echo Python 3.9 or newer is needed. Get it from https://www.python.org/downloads/
  goto :fail
)
%PY% -c "import tkinter" 2>nul || (
  echo Your Python is missing tkinter. Reinstall Python from python.org with "tcl/tk and IDLE" ticked.
  goto :fail
)

echo [1/4] Setting up a private Python environment...
if not exist ".venv\Scripts\python.exe" (
  %PY% -m venv .venv || goto :fail
)
set "VPY=.venv\Scripts\python.exe"

echo [2/4] Installing packages...
"%VPY%" -m pip install --disable-pip-version-check -q aiohttp qrcode pystray pillow pyinstaller || goto :fail

echo [3/4] Building the app (takes a minute)...
"%VPY%" claudes_trackpad.py --make-icon icon.ico || goto :fail
rem Close a running copy, or Windows won't let us replace its files
taskkill /im ClaudesTrackpad.exe /f >nul 2>&1 && timeout /t 1 /nobreak >nul
"%VPY%" -m PyInstaller --noconfirm --clean --log-level WARN --windowed ^
  --name ClaudesTrackpad --icon "%CD%\icon.ico" --hidden-import pystray._win32 ^
  --distpath dist --workpath build --specpath build claudes_trackpad.py || goto :fail
if not exist "dist\ClaudesTrackpad\ClaudesTrackpad.exe" (
  echo PyInstaller didn't produce the app.
  goto :fail
)

echo [4/4] Installing and adding it to the Start menu...
rem The copy is done here rather than by Python: Microsoft Store Python silently
rem redirects anything it writes under AppData into its own hidden sandbox folder.
set "DEST=%LOCALAPPDATA%\Programs\ClaudesTrackpad"
if exist "%DEST%" rmdir /s /q "%DEST%"
robocopy "dist\ClaudesTrackpad" "%DEST%" /e /njh /njs /nfl /ndl /np >nul
if not exist "%DEST%\ClaudesTrackpad.exe" (
  echo Couldn't copy the app to %DEST%
  goto :fail
)
powershell -NoProfile -ExecutionPolicy Bypass -File make_shortcut.ps1 -Target "%DEST%\ClaudesTrackpad.exe" || goto :fail

echo.
echo Done! Search "Claude's Trackpad" in the Start menu any time. Opening it now...
start "" "%DEST%\ClaudesTrackpad.exe"
pause
exit /b 0

:fail
echo.
echo Build failed. Scroll up to see what went wrong.
pause
exit /b 1