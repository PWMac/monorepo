@echo off
rem ============================================================
rem  Oracle Edge installer / updater
rem  Installs the code in THIS folder (as it is now, including
rem  your local edits) to C:\OracleEdge, adds a desktop shortcut
rem  and starts the app. Nothing is downloaded from GitHub.
rem  Safe to run again: your data, settings, logs and the private
rem  Python runtime in C:\OracleEdge are never touched.
rem ============================================================
setlocal
set "SRC=%~dp0"
set "SRC=%SRC:~0,-1%"
set "DEST=C:\OracleEdge"

if not exist "%SRC%\oracle_edge\app.py" (
  echo  This installer must stay in the oracle-edge folder next to Start.bat.
  pause
  exit /b 1
)
for /f "tokens=2 delims==" %%v in ('findstr /b "__version__" "%SRC%\oracle_edge\__init__.py"') do set "VER=%%~v"
set "VER=%VER: =%"
set "VER=%VER:"=%"
echo.
echo  Installing Oracle Edge %VER% from %SRC%
echo  to %DEST% ...

echo  Stopping Oracle Edge if it is running (so the new version is used) ...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | ForEach-Object {" ^
  " $p = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue;" ^
  " if ($p -and $p.Path -like '*OracleEdge*') { Stop-Process -Id $p.Id -Force } }" >nul 2>&1
timeout /t 2 /nobreak >nul

rem  Copy the code. Local state and developer folders are skipped, so a repo used for
rem  development never overwrites the installed app's data.
robocopy "%SRC%" "%DEST%" /E /XD data runtime logs exports profiles .venv .git __pycache__ .pytest_cache .ruff_cache ^
  /XF Install-OracleEdge.bat *.pyc /NFL /NDL /NJH /NJS /NP >nul
if %ERRORLEVEL% GEQ 8 (
  echo  Copy failed ^(robocopy error %ERRORLEVEL%^).
  pause
  exit /b 1
)

rem  uv.exe (the 32 MB launcher) isn't in git. Keep the installed copy; download it only if there is none.
if not exist "%DEST%\uv.exe" (
  echo  Downloading uv.exe, the Python launcher ^(one time^) ...
  powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$z=Join-Path $env:TEMP 'uv-win.zip'; $d=Join-Path $env:TEMP 'uv-win';" ^
    "Invoke-WebRequest 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip' -OutFile $z;" ^
    "Expand-Archive $z $d -Force; Copy-Item (Join-Path $d 'uv.exe') '%DEST%\uv.exe' -Force"
  if not exist "%DEST%\uv.exe" (
    echo  Couldn't get uv.exe. Check the internet connection and run this again.
    pause
    exit /b 1
  )
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\Oracle Edge.lnk');" ^
  "$s.TargetPath='%DEST%\Start.bat';$s.WorkingDirectory='%DEST%';$s.IconLocation='%DEST%\tools\oracle_edge.ico';" ^
  "$s.Description='Oracle Edge - Buffett-style stock screener';$s.Save()" >nul 2>&1
echo  Done. Desktop shortcut "Oracle Edge" is in place.
echo  Starting Oracle Edge %VER% ...
echo.
call "%DEST%\Start.bat"
