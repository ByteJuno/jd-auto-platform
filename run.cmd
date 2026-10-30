@echo off
REM run.cmd - wrapper that bypasses ExecutionPolicy. ASCII only (cmd reads ANSI).
REM Usage: run.cmd up ^| init ^| push ^| watch ^| ps ^| logs ^| upgrade ^| down
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
endlocal
