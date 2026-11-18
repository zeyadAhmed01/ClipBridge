@echo off
setlocal
title ClipBridge v1.0.0 Manager - By Zeyad Ahmed

set "SCRIPT_DIR=%~dp0"
set "PY_SCRIPT=%SCRIPT_DIR%clipboard_bridge.py"
set "CB_CONFIG=%SCRIPT_DIR%config.json"
set "STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"

:MENU
cls
echo ======================================================================
echo   ClipBridge v1.0.0 - Windows ^<^> Android Clipboard ^& File Bridge
echo   Created by Zeyad Ahmed
echo ======================================================================
echo.
echo   [1] Install ^& Start Silently (Recommended First-Time Setup)
echo   [2] Show Connection Info, Secret Key ^& Android Import URL (Read-Only)
echo   [3] Restart Background Server
echo   [4] Stop Background Server
echo   [5] Remove Integration (Owned Startup, Firewall ^& Protocol; keep data)
echo   [6] Clean Temporary Storage (Safe / Non-Destructive)
echo   [7] Configure Private LAN Firewall Only (Administrator)
echo   [0] Exit
echo.
set "CHOICE="
set /p CHOICE="Select an option (0-7): "

if "%CHOICE%"=="1" goto INSTALL
if "%CHOICE%"=="2" goto STATUS
if "%CHOICE%"=="3" goto RESTART
if "%CHOICE%"=="4" goto STOP
if "%CHOICE%"=="5" goto UNINSTALL
if "%CHOICE%"=="6" goto CLEANUP
if "%CHOICE%"=="7" goto FIREWALL
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

:CHECK_ELEVATED
REM Fail closed: if PowerShell cannot identify the token, do not start the server.
set "CB_ELEVATED=UNKNOWN"
powershell.exe -NoProfile -NonInteractive -Command "if (([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { exit 0 } else { exit 21 }" >nul 2>nul
set "CB_CHECK_CODE=%ERRORLEVEL%"
if "%CB_CHECK_CODE%"=="0" set "CB_ELEVATED=1"
if "%CB_CHECK_CODE%"=="21" set "CB_ELEVATED=0"
exit /b 0

:READ_PORT
set "CB_PORT=5050"
where python >nul 2>nul
if errorlevel 1 exit /b 0
for /f "usebackq delims=" %%P in (`python -c "import json, os, pathlib; p = pathlib.Path(os.environ['CB_CONFIG']); d = json.loads(p.read_text(encoding='utf-8')) if p.exists() else {}; v = int(d.get('port', 5050)); print(v if 1 <= v <= 65535 else 5050)" 2^>nul`) do set "CB_PORT=%%P"
exit /b 0

:READ_INSTALL_ID
set "CB_INSTALL_ID="
set "PY_SCRIPT_CANON="
where python >nul 2>nul
if errorlevel 1 exit /b 0
for /f "usebackq delims=" %%I in (`python "%PY_SCRIPT%" --install-id 2^>nul`) do set "CB_INSTALL_ID=%%I"
for /f "usebackq delims=" %%C in (`python -c "import os, pathlib; print(os.path.normcase(str(pathlib.Path(os.environ['PY_SCRIPT']).resolve())))" 2^>nul`) do set "PY_SCRIPT_CANON=%%C"
if not "%CB_INSTALL_ID%"=="" (
    set "VBS_LAUNCHER=%STARTUP_DIR%\ClipBridge_%CB_INSTALL_ID%.vbs"
    set "CB_FW_RULE=ClipBridge_%CB_PORT%_%CB_INSTALL_ID%"
) else (
    set "VBS_LAUNCHER="
    set "CB_FW_RULE="
)
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
  "$procs = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^(?:python(?:w|3(?:\.\d+)?)?|pyw?)\.exe$' -and $_.CommandLine -and ($_.CommandLine -match $pat) -and ($_.CommandLine -notmatch '(?i)\s--(?:status|info|install-id|register-only|unregister|install-startup|uninstall-startup|uninstall-firewall|check-firewall|protocol|cleanup(?:-force)?)\b') }); " ^
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
  "$procs = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^(?:python(?:w|3(?:\.\d+)?)?|pyw?)\.exe$' -and $_.CommandLine -and ($_.CommandLine -match $pat) -and ($_.CommandLine -notmatch '(?i)\s--(?:status|info|install-id|register-only|unregister|install-startup|uninstall-startup|uninstall-firewall|check-firewall|protocol|cleanup(?:-force)?)\b') }); " ^
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
call :CHECK_ELEVATED
if not "%CB_ELEVATED%"=="0" (
    echo [ERROR] Never launch the ClipBridge background server from an elevated manager.
    echo         Reopen manage.bat normally and select [1] to install and start.
    echo         For the firewall only, launch manage.bat as Administrator and select [7].
    pause
    goto MENU
)
call :CHECK_PYTHON
if errorlevel 1 goto MENU
echo.
echo [1/4] Stopping any existing ClipBridge instance for this installation...
call :STOP_PORT

echo [2/4] Registering Windows Toast protocol ^& generating config.json...
python "%PY_SCRIPT%" --register-only
call :READ_PORT
call :READ_INSTALL_ID
call :RESOLVE_RUNNER

echo [3/4] Checking Windows Firewall for port %CB_PORT% (Private / LocalSubnet)...
netsh advfirewall firewall show rule name="%CB_FW_RULE%" >nul 2>nul
if errorlevel 1 (
    echo [INFO] No ClipBridge firewall rule "%CB_FW_RULE%" is configured for port %CB_PORT%.
    echo        If Android cannot connect on your trusted Private LAN, use [7]
    echo        from a separate Administrator manager session to configure it.
) else (
    echo   Firewall rule "%CB_FW_RULE%" exists; review its Private / LocalSubnet scope.
)

echo [4/4] Creating silent Windows Startup launcher ^& starting server...
python "%PY_SCRIPT%" --install-startup
if "%VBS_LAUNCHER%"=="" (
    echo [ERROR] Could not resolve Startup launcher path.
    pause
    goto MENU
)
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
call :READ_INSTALL_ID
echo.
python "%PY_SCRIPT%" --status
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
call :CHECK_ELEVATED
if not "%CB_ELEVATED%"=="0" (
    echo [ERROR] Restart the background server from a normal, non-elevated manager.
    echo         For firewall configuration only, use [7].
    pause
    goto MENU
)
call :CHECK_PYTHON
if errorlevel 1 goto MENU
call :READ_PORT
call :READ_INSTALL_ID
call :RESOLVE_RUNNER
echo.
echo Restarting ClipBridge background server...
call :STOP_PORT
set "LAUNCHED=0"
if not "%VBS_LAUNCHER%"=="" if exist "%VBS_LAUNCHER%" (
    wscript.exe "%VBS_LAUNCHER%"
    set "LAUNCHED=1"
)
if "%LAUNCHED%"=="0" (
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

:FIREWALL
call :CHECK_ELEVATED
if not "%CB_ELEVATED%"=="1" (
    echo [ERROR] The firewall-only action requires Administrator privileges.
    echo         Close this manager, reopen it as Administrator, and choose [7].
    echo         Then close the elevated window and run [1] normally.
    pause
    goto MENU
)
call :CHECK_PYTHON
if errorlevel 1 goto MENU
call :READ_PORT
call :READ_INSTALL_ID
if "%CB_FW_RULE%"=="" (
    echo [ERROR] Could not resolve installation ID for firewall rule ownership.
    pause
    goto MENU
)
echo.
echo Configuring ClipBridge Windows Firewall for port %CB_PORT%...
echo Only the trusted Private network profile and local subnet are allowed.
python "%PY_SCRIPT%" --check-firewall >nul 2>nul
set "FW_CHK=%ERRORLEVEL%"
if "%FW_CHK%"=="2" (
    echo [ERROR] Firewall rule "%CB_FW_RULE%" exists with unverified or foreign ownership. Aborting safely.
    pause
    goto MENU
)
if "%FW_CHK%"=="0" (
    netsh advfirewall firewall delete rule name="%CB_FW_RULE%" >nul 2>nul
)
netsh advfirewall firewall add rule name="%CB_FW_RULE%" dir=in action=allow protocol=TCP localport=%CB_PORT% profile=private remoteip=localsubnet description="ClipBridge [id=%CB_INSTALL_ID%] path=%PY_SCRIPT_CANON%" >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Could not configure the firewall rule.
) else (
    echo [SUCCESS] Firewall rule "%CB_FW_RULE%" configured. No server was started with elevated privileges.
)
echo.
pause
goto MENU

:CLEANUP
call :CHECK_PYTHON
if errorlevel 1 goto MENU
echo.
echo Cleaning inactive temporary files ^(preserving active Windows clipboard files^)...
python "%PY_SCRIPT%" --cleanup
if errorlevel 1 (
    echo [INFO] Offline cleanup was skipped/aborted safely.
)
echo.
pause
goto MENU

:UNINSTALL
call :READ_PORT
call :READ_INSTALL_ID
echo.
echo Removing integrations owned by this installation ^(config.json and Downloads are retained^)...
call :STOP_PORT

where python >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    python "%PY_SCRIPT%" --uninstall-startup
    python "%PY_SCRIPT%" --uninstall-firewall
    python "%PY_SCRIPT%" --unregister
) else (
    echo [WARNING] Python was not found; verifying startup and protocol ownership with PowerShell...
    powershell.exe -NoProfile -NonInteractive -Command ^
      "$target = [IO.Path]::GetFullPath($env:PY_SCRIPT); " ^
      "$esc = [regex]::Escape($target); " ^
      "$q = [char]34; $sq = [char]39; " ^
      "$pat = '(?i)(?:^|\s|' + $q + '|' + $sq + ')' + $esc + '(?:' + $q + '|' + $sq + '|\s|$)'; " ^
      "Get-ChildItem -Path $env:STARTUP_DIR -Filter 'ClipBridge*.vbs' -ErrorAction SilentlyContinue | ForEach-Object { " ^
      "  $txt = Get-Content -LiteralPath $_.FullName -Raw -ErrorAction SilentlyContinue; " ^
      "  if ($txt -and ($txt -match $pat)) { Remove-Item -LiteralPath $_.FullName -Force; Write-Output ('[REMOVED] Startup launcher: ' + $_.Name) } " ^
      "  else { Write-Output ('[SKIPPED] Startup launcher (not owned): ' + $_.Name) } " ^
      "}; " ^
      "$cmdKey = 'HKCU:\Software\Classes\clipbridge\shell\open\command'; " ^
      "if (Test-Path $cmdKey) { " ^
      "  $cmd = (Get-ItemProperty -Path $cmdKey -Name '(Default)' -ErrorAction SilentlyContinue).'(Default)'; " ^
      "  if ($cmd -and ($cmd -match $pat)) { Remove-Item -Path 'HKCU:\Software\Classes\clipbridge' -Recurse -Force; Write-Output '[REMOVED] Registered clipbridge:// protocol.' } " ^
      "  else { Write-Output '[SKIPPED] clipbridge:// protocol is owned by another installation.' } " ^
      "}"
    echo [SKIPPED] Firewall rule ownership could not be verified without Python. Inspect Windows Firewall manually if needed.
)
echo [SUCCESS] Completed ownership-verified integration cleanup for this installation.
echo [INFO] Configuration ^(config.json^), foreign installations, and Downloads were preserved.
pause
goto MENU
