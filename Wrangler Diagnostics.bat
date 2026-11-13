@echo off
rem Opens Wrangler Diagnostics without a terminal window. Use this if double-clicking the .pyw file does not work.
cd /d "%~dp0"
where pythonw >nul 2>nul && (start "" pythonw "Wrangler Diagnostics.pyw" & exit /b)
where pyw >nul 2>nul && (start "" pyw "Wrangler Diagnostics.pyw" & exit /b)
python "Wrangler Diagnostics.pyw"
