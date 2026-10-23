@echo off
title Removing Claude's Trackpad
taskkill /im ClaudesTrackpad.exe /f >nul 2>&1 && timeout /t 1 /nobreak >nul
rmdir /s /q "%LOCALAPPDATA%\Programs\ClaudesTrackpad" 2>nul
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Claude's Trackpad.lnk" 2>nul
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Claude's Trackpad.lnk" 2>nul
rmdir /s /q "%APPDATA%\ClaudesTrackpad" 2>nul
echo Claude's Trackpad has been removed. You can delete this folder too.
pause
