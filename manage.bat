@echo off
setlocal
title ClipBridge v1.0.0 Manager - By Zeyad Ahmed

set "SCRIPT_DIR=%~dp0"
set "PY_SCRIPT=%SCRIPT_DIR%clipboard_bridge.py"
set "CB_CONFIG=%SCRIPT_DIR%config.json"
set "STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "VBS_LAUNCHER=%STARTUP_DIR%\ClipBridge_SilentStartup.vbs"

:MENU
cls
echo ======================================================================
echo   ClipBridge v1.0.0 - Windows ^<^> Android Clipboard ^& File Bridge
echo   Created by Zeyad Ahmed
echo ======================================================================
echo.
echo   [1] Install ^& Start Silently (Recommended First-Time Setup)
echo   [2] Show Connection Info, Secret Key ^& Android Import URL
echo   [3] Restart Background Server
echo   [4] Stop Background Server
echo   [5] Remove Integration (Startup, Firewall ^& Protocol; keep data)
echo   [6] Clean Temporary Storage (Safe / Non-Destructive)
echo   [0] Exit
echo.
set "CHOICE="
set /p CHOICE="Select an option (0-6): "

if "%CHOICE%"=="1" goto INSTALL
if "%CHOICE%"=="2" goto STATUS
if "%CHOICE%"=="3" goto RESTART
if "%CHOICE%"=="4" goto STOP
if "%CHOICE%"=="5" goto UNINSTALL
if "%CHOICE%"=="6" goto CLEANUP
if "%CHOICE%"=="0" exit /b 0
goto MENU

:CHECK_PYTHON
where python >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Python 3 was not found in your PATH. Please install Python 3.10+ first.
    pause
    exit /b 1
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 (
    echo [ERROR] ClipBridge requires Python 3.10 or newer.
    pause
    exit /b 1
)
exit /b 0

:READ_PORT
set "CB_PORT=5050"
where python >nul 2>nul
if errorlevel 1 exit /b 0
for /f "usebackq delims=" %%P in (`python -c "import json, os, pathlib; p = pathlib.Path(os.environ['CB_CONFIG']); d = json.loads(p.read_text(encoding='utf-8')) if p.exists() else {}; v = int(d.get('port', 5050)); print(v if 1 <= v <= 65535 else 5050)" 2^>nul`) do set "CB_PORT=%%P"
exit /b 0

:RESOLVE_RUNNER
set "PY_RUNNER=python"
for /f "usebackq delims=" %%R in (`python -c "import pathlib, sys; p = pathlib.Path(sys.executable); pw = p.with_name('pythonw.exe'); print(pw if pw.exists() else p)" 2^>nul`) do set "PY_RUNNER=%%R"
exit /b 0

:STOP_PORT
REM Stop only this installation's ClipBridge server Python instance.
REM Never terminate unrelated software that happens to listen on port 5050.
powershell.exe -NoProfile -NonInteractive -Command ^
  "$target = [IO.Path]::GetFullPath($env:PY_SCRIPT); " ^
  "$esc = [regex]::Escape($target); " ^
  "$q = [char]34; $sq = [char]39; " ^
  "$pat = '(?i)(?:^|\s|' + $q + '|' + $sq + ')' + $esc + '(?:' + $q + '|' + $sq + '|\s|$)'; " ^
  "$procs = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^(?:python(?:w|3(?:\.\d+)?)?|pyw?)\.exe$' -and $_.CommandLine -and ($_.CommandLine -match $pat) -and ($_.CommandLine -notmatch '(?i)\s--(?:register-only|unregister|protocol|cleanup(?:-force)?)\b') }); " ^
  "foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }; " ^
  "if ($procs.Count -gt 0) { Wait-Process -Id ($procs.ProcessId) -Timeout 5 -ErrorAction SilentlyContinue }"
exit /b 0

:CHECK_RUNNING
set "CB_STATE=STOPPED"
set "CB_STATUS_FILE=%TEMP%\clipbridge_status_%RANDOM%.tmp"
powershell.exe -NoProfile -NonInteractive -Command ^
  "$target = [IO.Path]::GetFullPath($env:PY_SCRIPT); " ^
  "$esc = [regex]::Escape($target); " ^
  "$q = [char]34; $sq = [char]39; " ^
  "$pat = '(?i)(?:^|\s|' + $q + '|' + $sq + ')' + $esc + '(?:' + $q + '|' + $sq + '|\s|$)'; " ^
  "$port = [int]$env:CB_PORT; " ^
  "$procs = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^(?:python(?:w|3(?:\.\d+)?)?|pyw?)\.exe$' -and $_.CommandLine -and ($_.CommandLine -match $pat) -and ($_.CommandLine -notmatch '(?i)\s--(?:register-only|unregister|protocol|cleanup(?:-force)?)\b') }); " ^
  "if ($procs.Count -gt 0) { Write-Output ('RUNNING:' + ($procs[0].ProcessId)); exit 0 }; " ^
  "$listener = $null; " ^
  "try { $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1 } catch {}; " ^
  "if ($listener) { Write-Output ('OCCUPIED:' + $listener.OwningProcess); exit 2 }; " ^
  "Write-Output 'STOPPED'; exit 1" > "%CB_STATUS_FILE%" 2>nul
if exist "%CB_STATUS_FILE%" (
    set /p CB_STATE=<"%CB_STATUS_FILE%"
    del /F /Q "%CB_STATUS_FILE%" >nul 2>nul
)
exit /b 0

:INSTALL
call :CHECK_PYTHON
if errorlevel 1 goto MENU
echo.
echo [1/4] Stopping any existing ClipBridge instance for this installation...
call :STOP_PORT

echo [2/4] Registering Windows Toast protocol ^& generating config.json...
python "%PY_SCRIPT%" --register-only
call :READ_PORT
call :RESOLVE_RUNNER

echo [3/4] Configuring Windows Firewall for port %CB_PORT% (Private / LocalSubnet)...
net session >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    netsh advfirewall firewall delete rule name="ClipBridge_%CB_PORT%" >nul 2>nul
    netsh advfirewall firewall delete rule name="ClipBridge_5050" >nul 2>nul
    netsh advfirewall firewall delete rule name="ClipboardBridge_5050" >nul 2>nul
    netsh advfirewall firewall add rule name="ClipBridge_%CB_PORT%" dir=in action=allow protocol=TCP localport=%CB_PORT% profile=private remoteip=localsubnet >nul 2>nul
    if errorlevel 1 (
        echo [WARNING] Failed to create Windows Firewall rule for port %CB_PORT%.
    ) else (
        echo   Firewall rule "ClipBridge_%CB_PORT%" configured ^(Private / LocalSubnet^).
    )
) else (
    netsh advfirewall firewall show rule name="ClipBridge_%CB_PORT%" >nul 2>nul
    if errorlevel 1 (
        echo [WARNING] Firewall setup requires elevation and a trusted Private network.
        echo           Run manage.bat as Administrator to allow port %CB_PORT% on Private/LocalSubnet.
    ) else (
        echo   Existing firewall rule "ClipBridge_%CB_PORT%" detected.
    )
)

echo [4/4] Creating silent Windows Startup launcher ^& starting server...
(
echo Set WshShell = CreateObject^("WScript.Shell"^)
echo WshShell.Run """%PY_RUNNER%"" ""%PY_SCRIPT%""", 0, False
) > "%VBS_LAUNCHER%"
wscript.exe "%VBS_LAUNCHER%"

powershell.exe -NoProfile -NonInteractive -Command "Start-Sleep -Milliseconds 1200" >nul 2>nul
call :CHECK_RUNNING
echo.
echo(%CB_STATE%| findstr /B "RUNNING:" >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    echo [SUCCESS] ClipBridge is now running silently in the background! ^(%CB_STATE%, port %CB_PORT%^)
) else (
    echo [WARNING] ClipBridge launcher executed, but server status is: %CB_STATE%
    echo           Run "python clipboard_bridge.py" in a terminal to check startup logs.
)
echo.
pause
goto MENU

:STATUS
call :CHECK_PYTHON
if errorlevel 1 goto MENU
call :READ_PORT
echo.
python "%PY_SCRIPT%" --register-only
echo.
echo Checking ClipBridge status ^(port %CB_PORT%^):
call :CHECK_RUNNING
echo(%CB_STATE%| findstr /B "RUNNING:" >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    echo   Server Status : ONLINE ^(%CB_STATE%, port %CB_PORT%^)
) else (
    echo(%CB_STATE%| findstr /B "OCCUPIED:" >nul 2>nul
    if %ERRORLEVEL% EQU 0 (
        echo   Server Status : STOPPED ^(Port %CB_PORT% is used by another process: %CB_STATE%^)
    ) else (
        echo   Server Status : STOPPED
    )
)
echo.
pause
goto MENU

:RESTART
call :CHECK_PYTHON
if errorlevel 1 goto MENU
call :READ_PORT
call :RESOLVE_RUNNER
echo.
echo Restarting ClipBridge background server...
call :STOP_PORT
if exist "%VBS_LAUNCHER%" (
    wscript.exe "%VBS_LAUNCHER%"
) else (
    powershell.exe -NoProfile -NonInteractive -Command "Start-Process -FilePath $env:PY_RUNNER -ArgumentList @($env:PY_SCRIPT) -WindowStyle Hidden"
)
powershell.exe -NoProfile -NonInteractive -Command "Start-Sleep -Milliseconds 1200" >nul 2>nul
call :CHECK_RUNNING
echo(%CB_STATE%| findstr /B "RUNNING:" >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    echo [SUCCESS] Server restarted! ^(%CB_STATE%, port %CB_PORT%^)
) else (
    echo [WARNING] Restart completed, but server status is: %CB_STATE%
)
pause
goto MENU

:STOP
echo.
echo Stopping ClipBridge background server...
call :STOP_PORT
echo [SUCCESS] ClipBridge background server stopped.
pause
goto MENU

:CLEANUP
call :CHECK_PYTHON
if errorlevel 1 goto MENU
echo.
echo Cleaning inactive temporary files ^(preserving active Windows clipboard files^)...
python "%PY_SCRIPT%" --cleanup
echo.
pause
goto MENU

:UNINSTALL
call :READ_PORT
echo.
echo Removing system integrations ^(config.json and saved Downloads are retained^)...
call :STOP_PORT
if exist "%VBS_LAUNCHER%" del /F /Q "%VBS_LAUNCHER%"
if exist "%STARTUP_DIR%\ClipboardBridge_SilentStartup.vbs" del /F /Q "%STARTUP_DIR%\ClipboardBridge_SilentStartup.vbs"

set "FW_WARN=0"
for %%R in ("ClipBridge_%CB_PORT%" "ClipBridge_5050" "ClipboardBridge_5050") do (
    netsh advfirewall firewall show rule name=%%R >nul 2>nul
    if not errorlevel 1 (
        netsh advfirewall firewall delete rule name=%%R >nul 2>nul
        if errorlevel 1 set "FW_WARN=1"
    )
)
if "%FW_WARN%"=="1" (
    echo [WARNING] One or more Windows Firewall rules could not be deleted without Administrator rights.
    echo           Re-run manage.bat as Administrator to delete ClipBridge firewall rules.
)

where python >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    python "%PY_SCRIPT%" --unregister
) else (
    powershell.exe -NoProfile -NonInteractive -Command "if (Test-Path 'HKCU:\Software\Classes\clipbridge') { Remove-Item -Path 'HKCU:\Software\Classes\clipbridge' -Recurse -Force }"
    echo Unregistered clipbridge:// protocol.
)
echo [SUCCESS] ClipBridge has been stopped and removed from Windows Startup.
echo [INFO] Configuration ^(config.json^) and Downloads were kept.
echo        If you also want to clean temporary files under %%TEMP%%\ClipBridge, use option [6] or delete that folder manually.
pause
goto MENU
