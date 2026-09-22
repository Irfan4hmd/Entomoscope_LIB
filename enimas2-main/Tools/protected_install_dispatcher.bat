@echo off
setlocal EnableExtensions EnableDelayedExpansion
set "CONTROL_ROOT=%~dp0"
set "CURRENT_FILE=%CONTROL_ROOT%current.version"
if not exist "%CURRENT_FILE%" (
    echo ENIMAS protected installer is not initialized.
    exit /b 1
)
set /p "CONTROL_VERSION="<"%CURRENT_FILE%"
echo(!CONTROL_VERSION!| findstr /R /X "v[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*" >nul
if errorlevel 1 (
    echo ENIMAS protected installer pointer is invalid.
    exit /b 1
)
set "VERSIONED_INSTALLER=%CONTROL_ROOT%versions\!CONTROL_VERSION!\install.bat"
if not exist "!VERSIONED_INSTALLER!" (
    echo ENIMAS protected installer !CONTROL_VERSION! is incomplete.
    exit /b 1
)
call "!VERSIONED_INSTALLER!" %*
exit /b !ERRORLEVEL!
