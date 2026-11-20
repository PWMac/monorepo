@echo off
rem Oracle Edge - one click to run. Everything stays inside this folder.
setlocal
cd /d "%~dp0"
set "UV_PYTHON_INSTALL_DIR=%~dp0runtime\python"
set "UV_CACHE_DIR=%~dp0runtime\cache"
set "UV_PROJECT_ENVIRONMENT=%~dp0runtime\venv"
set "UV_PYTHON_PREFERENCE=only-managed"
set "UV_NO_CONFIG=1"
if not exist "%~dp0runtime\venv\Scripts\pythonw.exe" (
  echo.
  echo  Oracle Edge - first start
  echo  Setting up a private copy of Python and the libraries it needs.
  echo  This happens once and takes a few minutes. Nothing is installed system-wide.
  echo.
)
"%~dp0uv.exe" sync --frozen --no-dev --quiet
if errorlevel 1 (
  echo.
  echo  Setup failed. Check your internet connection and try again.
  pause
  exit /b 1
)
start "" "%~dp0runtime\venv\Scripts\pythonw.exe" -m oracle_edge
exit /b 0
