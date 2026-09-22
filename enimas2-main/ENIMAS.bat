@echo off
setlocal EnableExtensions EnableDelayedExpansion
cls
echo Starting ENIMAS 2.0 application...
echo.

:: Recover an interrupted transaction before loading any application files.
if exist "C:\Program Files\ENIMAS\updates\bin\enimas_updater.py" if exist "C:\Program Files\Python310\python.exe" (
    "C:\Program Files\Python310\python.exe" "C:\Program Files\ENIMAS\updates\bin\enimas_updater.py" --install-root "C:\Program Files\ENIMAS" startup-check
    set "UPDATE_CHECK_EXIT=!ERRORLEVEL!"
    if "!UPDATE_CHECK_EXIT!"=="11" (
        powershell -NoProfile -WindowStyle Hidden -Command "Add-Type -AssemblyName PresentationFramework; [System.Windows.MessageBox]::Show('An ENIMAS update is currently in progress. Please wait for it to finish.','ENIMAS Update')" >nul 2>&1
        exit /b 42
    )
    if not "!UPDATE_CHECK_EXIT!"=="0" (
        powershell -NoProfile -WindowStyle Hidden -Command "Add-Type -AssemblyName PresentationFramework; [System.Windows.MessageBox]::Show('ENIMAS could not safely recover its installation. Run install.bat to continue or repair it.','ENIMAS Recovery Required')" >nul 2>&1
        exit /b 1
    )
)

:: Enable more detailed output
echo [%TIME%] Checking prerequisites...
echo [%TIME%] Python version:
python --version 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Python not found or not in PATH
    echo Please ensure Python is installed and in your PATH
    goto :error
)

:: Change to the application directory
echo [%TIME%] Changing to application directory...
cd "C:\Program Files\ENIMAS\src\"
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Could not change to application directory
    echo Please ensure the path exists: C:\Program Files\ENIMAS\src\
    goto :error
)

:: Check if virtual environment exists
echo [%TIME%] Checking virtual environment...
if not exist .\venv\Scripts\activate.bat (
    echo ERROR: Virtual environment not found
    echo Please run install.bat first to set up the virtual environment
    goto :error
)

:: Activate the virtual environment
echo [%TIME%] Activating virtual environment...
call .\venv\Scripts\activate.bat
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Failed to activate virtual environment
    goto :error
)

:: Check for required packages
echo [%TIME%] Checking for required packages...
python -c "import sys; print('Python executable:', sys.executable)"
python -c "import sys; print('Python path:', sys.path)" > ..\python-path.log

:: Run the application with more detailed logging
echo [%TIME%] Running main.py...
python main.py > ..\enimas-output.log 2>&1
set APP_EXIT=%ERRORLEVEL%

:: Exit code 42 = update or recovery handoff; leave immediately.
if %APP_EXIT% EQU 42 (
    echo [%TIME%] Update initiated. Exiting for update...
    cd /d "C:\Program Files\ENIMAS"
    exit
)

:: Display any errors
if %APP_EXIT% NEQ 0 (
    echo.
    echo ERROR: The application could not start properly.
    echo Please check enimas-output.log for details:
    echo ----------------------------------------
    type ..\enimas-output.log
    echo ----------------------------------------
    goto :error
)

echo [%TIME%] Application started successfully.
goto :end

:error
echo.
echo.
echo Error details:
echo - Check that all required Python packages are installed
echo - Check hardware connections (camera, motor controller, etc.)
echo - Verify configuration files (config.txt, lenses.json)
echo - Check system requirements (Python 3.10 or higher recommended)
echo.
echo Diagnostic information has been saved to:
echo - ..\enimas-output.log (Application output)
echo - ..\python-path.log (Python environment details)

:end
echo.
echo Press any key to exit...
pause > nul
