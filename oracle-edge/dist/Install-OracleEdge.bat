@echo off
rem ============================================================
rem  Oracle Edge installer / updater
rem  Unpacks OracleEdge-app.zip (next to this file) to C:\OracleEdge,
rem  adds a desktop shortcut, and starts the app.
rem  Safe to run again to update: your data, settings and the private
rem  Python runtime (data, runtime, logs, exports, profiles) are kept.
rem ============================================================
setlocal
set "ZIP=%~dp0OracleEdge-app.zip"
set "DEST=C:\OracleEdge"
set "TMPX=%TEMP%\OracleEdge-install"
if not exist "%ZIP%" (
  echo  Can't find OracleEdge-app.zip next to this installer. Has the folder finished syncing?
  pause
  exit /b 1
)
echo.
echo  Unpacking Oracle Edge ...
if exist "%TMPX%" rmdir /s /q "%TMPX%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath '%ZIP%' -DestinationPath '%TMPX%' -Force"
if errorlevel 1 (
  echo  Unpacking failed.
  pause
  exit /b 1
)
echo  Stopping Oracle Edge if it is running (so the new version is used) ...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | ForEach-Object {" ^
  " $p = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue;" ^
  " if ($p -and $p.Path -like '*OracleEdge*') { Stop-Process -Id $p.Id -Force } }" >nul 2>&1
timeout /t 2 /nobreak >nul
echo  Installing to %DEST% ...
robocopy "%TMPX%\OracleEdge" "%DEST%" /E /XD data runtime logs exports profiles /NFL /NDL /NJH /NJS /NP >nul
if %ERRORLEVEL% GEQ 8 (
  echo  Copy failed ^(robocopy error %ERRORLEVEL%^).
  pause
  exit /b 1
)
rmdir /s /q "%TMPX%" >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\Oracle Edge.lnk');" ^
  "$s.TargetPath='%DEST%\Start.bat';$s.WorkingDirectory='%DEST%';$s.IconLocation='%DEST%\tools\oracle_edge.ico';" ^
  "$s.Description='Oracle Edge - Buffett-style stock screener';$s.Save()" >nul 2>&1
echo  Done. A desktop shortcut "Oracle Edge" was added.
echo  Starting Oracle Edge (the first start sets up Python; a few minutes)...
echo.
call "%DEST%\Start.bat"
