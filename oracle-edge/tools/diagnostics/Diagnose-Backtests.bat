@echo off
rem Oracle Edge: read-only backtest diagnostics. Writes backtest_diag.json next to this file.
cd /d "%~dp0"
"C:\OracleEdge\runtime\venv\Scripts\python.exe" "%~dp0diagnose_backtests.py"
echo.
pause
