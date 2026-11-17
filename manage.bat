@echo off
setlocal EnableDelayedExpansion
title ClipBridge v1.0.0 Manager — By Zeyad Ahmed

set "SCRIPT_DIR=%~dp0"
set "PY_SCRIPT=%SCRIPT_DIR%clipboard_bridge.py"
set "STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "VBS_LAUNCHER=%STARTUP_DIR%\ClipBridge_SilentStartup.vbs"

:MENU
cls
echo ======================================================================
echo   ClipBridge v1.0.0 — Windows ^<^> Android Clipboard ^& File Bridge
echo   Created by Zeyad Ahmed
echo ======================================================================
echo.
echo   [1] Install ^& Start Silently (Recommended First-Time Setup)
echo   [2] Show Connection Info, Secret Key ^& Android Import URL
echo   [3] Restart Background Server
echo   [4] Stop Background Server
echo   [5] Remove Integration (Startup, Firewall ^& Protocol; keep data)
echo   [0] Exit
echo.
set /p CHOICE="Select an option (0-5): "

if "%CHOICE%"=="1" goto INSTALL
if "%CHOICE%"=="2" goto STATUS
if "%CHOICE%"=="3" goto RESTART
if "%CHOICE%"=="4" goto STOP
if "%CHOICE%"=="5" goto UNINSTALL
if "%CHOICE%"=="0" exit /b 0
goto MENU

:CHECK_PYTHON
where python >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Python 3 was not found in your PATH. Please install Python 3 first.
    pause
    exit /b 1
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 (
    echo [ERROR] ClipBridge requires Python 3.10 or newer.
    exit /b 1
)
exit /b 0

:STOP_PORT
REM Stop only this installation's ClipBridge Python instance.
REM Never kill unrelated software that happens to listen on port 5050.
powershell.exe -NoProfile -NonInteractive -Command "$target = [IO.Path]::GetFullPath($env:PY_SCRIPT); Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -and $_.CommandLine.Contains($target) } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
exit /b 0

:INSTALL
call :CHECK_PYTHON
if errorlevel 1 goto MENU
echo.
echo [1/4] Stopping any existing ClipBridge instance...
call :STOP_PORT

echo [2/4] Registering Windows Toast protocol ^& generating config.json...
python "%PY_SCRIPT%" --register-only

echo [3/4] Allowing Port 5050 through Windows Firewall...
netsh advfirewall firewall delete rule name="ClipBridge_5050" >nul 2>nul
netsh advfirewall firewall delete rule name="ClipboardBridge_5050" >nul 2>nul
netsh advfirewall firewall add rule name="ClipBridge_5050" dir=in action=allow protocol=TCP localport=5050 profile=private remoteip=localsubnet >nul 2>nul
if errorlevel 1 (
    echo [WARNING] Firewall setup requires elevation and a trusted Private network.
)

echo [4/4] Creating silent Windows Startup launcher ^& starting server...
(
echo Set WshShell = CreateObject^("WScript.Shell"^)
echo WshShell.Run "python ""%PY_SCRIPT%""", 0, False
) > "%VBS_LAUNCHER%"
wscript.exe "%VBS_LAUNCHER%"

echo.
echo [SUCCESS] ClipBridge is now running silently in the background!
echo.
pause
goto MENU

:STATUS
call :CHECK_PYTHON
if errorlevel 1 goto MENU
echo.
python "%PY_SCRIPT%" --register-only
echo.
echo Checking port 5050 status:
netstat -aon | findstr ":5050" | findstr "LISTENING" >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    echo   Server Status : ONLINE ^(Listening on port 5050^)
) else (
    echo   Server Status : STOPPED
)
echo.
pause
goto MENU

:RESTART
echo.
echo Restarting ClipBridge background server...
call :STOP_PORT
if exist "%VBS_LAUNCHER%" (
    wscript.exe "%VBS_LAUNCHER%"
) else (
    start "" /B pythonw "%PY_SCRIPT%"
)
echo [SUCCESS] Server restarted!
pause
goto MENU

:STOP
echo.
echo Stopping ClipBridge background server...
call :STOP_PORT
echo [SUCCESS] Server stopped.
pause
goto MENU

:UNINSTALL
echo.
echo Removing system integrations (configuration and transfers are retained)...
call :STOP_PORT
if exist "%VBS_LAUNCHER%" del /F /Q "%VBS_LAUNCHER%"
if exist "%STARTUP_DIR%\ClipboardBridge_SilentStartup.vbs" del /F /Q "%STARTUP_DIR%\ClipboardBridge_SilentStartup.vbs"
netsh advfirewall firewall delete rule name="ClipBridge_5050" >nul 2>nul
netsh advfirewall firewall delete rule name="ClipboardBridge_5050" >nul 2>nul
python "%PY_SCRIPT%" --unregister
echo [SUCCESS] ClipBridge has been stopped and removed from Windows Startup.
pause
goto MENU
