@echo off
rem Opens Wrangler Diagnostics and keeps this window open so any error stays visible.
rem Leave the window open while you use the app; close it to quit.
cd /d "%~dp0"
where python >nul 2>nul && (python "Wrangler Diagnostics.pyw" & goto done)
where py >nul 2>nul && (py "Wrangler Diagnostics.pyw" & goto done)
echo Python was not found. Install it from python.org and tick "Add python.exe to PATH".
:done
echo.
pause
