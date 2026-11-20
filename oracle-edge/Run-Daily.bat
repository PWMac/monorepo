@echo off
rem Oracle Edge - daily after-close update (started by Windows Task Scheduler).
setlocal
cd /d "%~dp0"
set "UV_PYTHON_INSTALL_DIR=%~dp0runtime\python"
set "UV_CACHE_DIR=%~dp0runtime\cache"
set "UV_PROJECT_ENVIRONMENT=%~dp0runtime\venv"
set "UV_PYTHON_PREFERENCE=only-managed"
set "UV_NO_CONFIG=1"
if not exist "%~dp0logs" mkdir "%~dp0logs"
"%~dp0uv.exe" sync --frozen --no-dev --quiet >> "%~dp0logs\daily.log" 2>&1
"%~dp0runtime\venv\Scripts\python.exe" -m oracle_edge.cli daily --wait >> "%~dp0logs\daily.log" 2>&1
exit /b %errorlevel%
