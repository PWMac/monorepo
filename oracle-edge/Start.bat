@echo off
rem Oracle Edge - start the app.
rem  In the installed copy (C:\OracleEdge, opened by the desktop shortcut) this starts the app.
rem  In the source folder (e.g. C:\repos\monorepo\oracle-edge) it never runs or overwrites anything:
rem  it installs on first use, and otherwise only offers a fresh install after a double confirmation.
setlocal
set "DEST=C:\OracleEdge"
if exist "%~dp0.installed" goto run
if /i "%~dp0"=="%DEST%\" goto run

rem ---------------- source folder ----------------
if not exist "%DEST%\Start.bat" (
  echo.
  echo  Oracle Edge isn't installed yet. Installing it from this folder to %DEST% ...
  call "%~dp0Install-OracleEdge.bat"
  exit /b
)
set "VER=unknown"
for /f "tokens=2 delims==" %%v in ('findstr /b "__version__" "%DEST%\oracle_edge\__init__.py" 2^>nul') do set "VER=%%~v"
set "VER=%VER: =%"
set "VER=%VER:"=%"
echo.
echo  Oracle Edge is already installed in %DEST% (version %VER%).
echo  Nothing has been changed.
echo.
echo    - To open it, use the "Oracle Edge" shortcut on the desktop.
echo    - To install your code changes and keep your data, run Install-OracleEdge.bat.
echo    - To DELETE the installed copy (database, settings, history) and start over, type FRESH.
echo.
set "ANS="
set /p "ANS=  Type FRESH to start over, or just press Enter to leave everything as it is: "
if /i not "%ANS%"=="FRESH" goto nothing
echo.
echo  This permanently deletes %DEST%, including your database, signals, backtests and settings.
echo  Your backups in the backup folder are NOT deleted, so a backup can be restored later.
set "ANS2="
set /p "ANS2=  Are you sure? Type YES (capitals) to delete and reinstall: "
if not "%ANS2%"=="YES" goto nothing
call "%~dp0Install-OracleEdge.bat" /fresh
exit /b

:nothing
echo.
echo  OK. Nothing was changed.
pause
exit /b 0

rem ---------------- installed copy ----------------
:run
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
