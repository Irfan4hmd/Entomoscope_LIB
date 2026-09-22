::  Installation script to install ENIMAS Software

@echo off

set INSTALL_DIR=%USERPROFILE%\Desktop\ENIMAS_TEST

timeout /t 3 /nobreak


:: create Windows Programs Folder
if exist "%INSTALL_DIR%\" (
    echo ENIMAS is already installed. The program is being updated ...
    rmdir /s /q "%INSTALL_DIR%\src\"
    if exist "%INSTALL_DIR%\src\" (
        echo Updating ENIMAS failed. Please close all other processes that are using ENIMAS currently and then restart this script.
        pause 
        goto :EOF
    )
) else (
    echo Creating program folder: %INSTALL_DIR%\
    mkdir "%INSTALL_DIR%\" 
)

cd "%INSTALL_DIR%\"

echo.

:: install Python without user interaction in UI if python 3.10 isn't installed
if not exist "C:\Program Files\Python310\python.exe" (
    echo Installing Python 3.10 ...
    Powershell -command "curl https://www.python.org/ftp/python/3.10.8/python-3.10.8-amd64.exe -o python-installer.exe"
    if not exist .\python-installer.exe (
        echo Installation failed!
        echo Please make sure to have an internet connection.
        echo.
        pause
        exit
    )
    python-installer.exe /passive InstallAllUsers=1 PrependPath=1 Include_doc=0 Include_test=0 Include_tcltk=0
    del python-installer.exe
    echo Successfully installed Python 3.10
    echo.
) else ( echo Python 3.10 already installed. Skipping installation.)


::install git
echo Git installing

:: --- if installed, exit ---
git --version >nul 2>&1
if %errorlevel%==0 (
  echo git installed, version:
  git --version
  goto :end
)

:: try winget
where winget >nul 2>&1
if %errorlevel%==0 (
  echo found winget, trying to install Git...
  winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements
  goto verify
)

:: find Git*.exe in ./git folder
set "GIT_INSTALLER="
if exist "%~dp0git\" (
  dir /b "%~dp0git\Git*.exe" >nul 2>&1
  if %errorlevel%==0 (
    for %%I in ("%~dp0git\Git*.exe") do (
      if not defined GIT_INSTALLER set "GIT_INSTALLER=%%~fI"
    )
  )
)

if defined GIT_INSTALLER (
  echo found installer "%GIT_INSTALLER%"
  echo installing with default settings (/VERYSILENT /NORESTART /SP-)...
  "%GIT_INSTALLER%" /VERYSILENT /NORESTART /SP- 
  goto verify
)

:verify
echo verify Git...
timeout /t 2 /nobreak >nul
git --version >nul 2>&1
if %errorlevel%==0 (
  echo successful installed
  git --version
) else (
  echo git installation failed, you can install it manually https://git-scm.com/downloads/win
)

:end
echo.


if not exist "%INSTALL_DIR%\enimas-main" (
:: load the source code form gitlab
:: https://gitlab.kit.edu/kit/iai/ber/enimas2/-/archive/enimas2/enimas2-enimas2.zip
echo Loading source script from GitLab ...
PowerShell -command "curl https://gitlab.kit.edu/kit/iai/ber/enimas2/-/archive/enimas2/enimas2-enimas2.zip -o enimas.zip"
if not exist .\enimas.zip (
    echo Installation failed!
    echo Please make sure to have an internet connection.
    echo.
    pause
    exit
)
tar -xf enimas.zip
del enimas.zip
echo Successfully loaded source script.
)


:: create a virtual environment and install all dependencies
ren enimas2-enimas2 src
cd src\

echo Creating virtual environment ...
"C:\Program Files\Python310\python.exe" -m venv venv_test
echo Successfully created virtual environment

echo.
echo Installing python dependencies ...
.\venv_test\Scripts\pip install -r .\requirements.txt
echo All dependencies have been installed


:: install the Arducam driver:
:: from https://github.com/ArduCAM/ArduCAM_USB_Camera_Shield
echo Installing ArduCam Driver ...
echo.
if "%PROCESSOR_ARCHITECTURE%" == "x86" goto x86
if "%PROCESSOR_ARCHITECTURE%" == "AMD64" goto x64

:x64
echo %PROCESSOR_ARCHITECTURE% 
C:\Windows\System32\pnputil.exe /add-driver .\camera-driver\x64\cyusb3.inf /install
goto end

:x86
echo %PROCESSOR_ARCHITECTURE% 
C:\Windows\System32\pnputil.exe /add-driver .\camera-driver\x86\cyusb3.inf /install
goto end

:end
echo Driver installed successfully
echo.

:: install the VAImaging camera driver:
:: from https://gitlab.kit.edu/kit/iai/ber/va_imaging
echo Installing VAImaging Driver ...
echo.
set FILENAME=camera-driver/Vaimaging/Galaxy_Windows_EN_32bits-64bits_2.3.2410.9292.exe


if exist "%FILENAME%" (
    echo Installer found. Running...
    start "" /wait "%FILENAME%"
    echo Installation process finished.
) else (
    echo ERROR: Installer file %FILENAME% not found!
)
echo.

:: copy the existing ENIMAS_test.bat file to appropriate locations
cd ..
if exist ".\src\ENIMAS_test.bat" (
    >nul copy /y ".\src\ENIMAS_test.bat" .
    echo Copied ENIMAS_test.bat from repository.
) else (
    echo WARNING: ENIMAS_test.bat not found in repository. Creating a basic version...
    echo @echo off > ENIMAS_test.bat
    echo cls >> ENIMAS_test.bat
    echo echo Starting ENIMAS 2.0 application... >> ENIMAS_test.bat
    echo echo. >> ENIMAS_test.bat
    echo :: Change to the application directory >> ENIMAS_test.bat
    echo cd "%INSTALL_DIR%\src\" >> ENIMAS_test.bat
    echo :: Activate the virtual environment and run the application >> ENIMAS_test.bat
    echo call .\venv_test\Scripts\activate.bat ^&^& python main.py ^> ..\enimas-output.log 2^>^&1 >> ENIMAS_test.bat
    echo :: Display any errors >> ENIMAS_test.bat
    echo if %%ERRORLEVEL%% NEQ 0 ( >> ENIMAS_test.bat
    echo     echo. >> ENIMAS_test.bat
    echo     echo ERROR: The application could not start properly. >> ENIMAS_test.bat
    echo     echo Please check enimas-output.log for details: >> ENIMAS_test.bat
    echo     echo ---------------------------------------- >> ENIMAS_test.bat
    echo     type ..\enimas-output.log >> ENIMAS_test.bat
    echo     echo ---------------------------------------- >> ENIMAS_test.bat
    echo ) >> ENIMAS_test.bat
    echo echo. >> ENIMAS_test.bat
    echo echo Press any key to exit... >> ENIMAS_test.bat
    echo pause ^> nul >> ENIMAS_test.bat
)

:: Create desktop shortcut with hidden command window
echo Creating desktop shortcut with hidden command window...
echo Set oWS = WScript.CreateObject("WScript.Shell") > CreateShortcut.vbs
echo sLinkFile = "%USERPROFILE%\Desktop\ENIMAS2.0_TEST.lnk" >> CreateShortcut.vbs
echo Set oLink = oWS.CreateShortcut(sLinkFile) >> CreateShortcut.vbs
echo oLink.TargetPath = "wscript.exe" >> CreateShortcut.vbs
echo oLink.Arguments = """%INSTALL_DIR%\hidden_launcher.vbs""" >> CreateShortcut.vbs
echo oLink.WorkingDirectory = "%INSTALL_DIR%\" >> CreateShortcut.vbs
echo oLink.IconLocation = "%INSTALL_DIR%\src\UserInterface\imgs\enimas_Icon.ico" >> CreateShortcut.vbs
echo oLink.Save >> CreateShortcut.vbs
cscript //nologo CreateShortcut.vbs
del CreateShortcut.vbs

:: Create Start Menu shortcut with hidden command window
echo Creating Start Menu shortcut with hidden command window...
echo Set oWS = WScript.CreateObject("WScript.Shell") > CreateShortcut.vbs
echo sLinkFile = "%USERPROFILE%\Desktop\ENIMAS2.0_TEST_StartMenu.lnk" >> CreateShortcut.vbs
echo Set oLink = oWS.CreateShortcut(sLinkFile) >> CreateShortcut.vbs
echo oLink.TargetPath = "wscript.exe" >> CreateShortcut.vbs
echo oLink.Arguments = """%INSTALL_DIR%\hidden_launcher.vbs""" >> CreateShortcut.vbs
echo oLink.WorkingDirectory = "%INSTALL_DIR%\" >> CreateShortcut.vbs
echo oLink.IconLocation = "%INSTALL_DIR%\src\UserInterface\imgs\enimas_Icon.ico" >> CreateShortcut.vbs
echo oLink.Save >> CreateShortcut.vbs
cscript //nologo CreateShortcut.vbs
del CreateShortcut.vbs

:: Create the hidden launcher VBS file
echo Creating hidden launcher VBS file...
echo Option Explicit > hidden_launcher.vbs
echo. >> hidden_launcher.vbs
echo On Error Resume Next >> hidden_launcher.vbs
echo. >> hidden_launcher.vbs
echo ' Create shell object and file system object >> hidden_launcher.vbs
echo Dim shell, fso, logFile >> hidden_launcher.vbs
echo Set shell = CreateObject("WScript.Shell") >> hidden_launcher.vbs
echo Set fso = CreateObject("Scripting.FileSystemObject") >> hidden_launcher.vbs
echo. >> hidden_launcher.vbs
echo ' Create a log file for errors only if needed >> hidden_launcher.vbs
echo If Not fso.FileExists("%INSTALL_DIR%\ENIMAS_test.bat") Then >> hidden_launcher.vbs
echo     Set logFile = fso.CreateTextFile("%INSTALL_DIR%\launcher_error.log", True) >> hidden_launcher.vbs
echo     logFile.WriteLine "ERROR: ENIMAS_test.bat file not found at " ^& Now >> hidden_launcher.vbs
echo     logFile.Close >> hidden_launcher.vbs
echo     MsgBox "ENIMAS application not found. Please reinstall the application.", 16, "ENIMAS Error" >> hidden_launcher.vbs
echo     WScript.Quit 1 >> hidden_launcher.vbs
echo End If >> hidden_launcher.vbs
echo. >> hidden_launcher.vbs
echo ' Change directory to the correct location >> hidden_launcher.vbs
echo shell.CurrentDirectory = "%INSTALL_DIR%\" >> hidden_launcher.vbs
echo If Err.Number ^<^> 0 Then >> hidden_launcher.vbs
echo     Set logFile = fso.CreateTextFile("%INSTALL_DIR%\launcher_error.log", True) >> hidden_launcher.vbs
echo     logFile.WriteLine "ERROR changing directory: " ^& Err.Description >> hidden_launcher.vbs
echo     logFile.Close >> hidden_launcher.vbs
echo     MsgBox "Error starting ENIMAS. Please check your installation.", 16, "ENIMAS Error" >> hidden_launcher.vbs
echo     Err.Clear >> hidden_launcher.vbs
echo     WScript.Quit 1 >> hidden_launcher.vbs
echo End If >> hidden_launcher.vbs
echo. >> hidden_launcher.vbs
echo ' Run the batch file with window hidden (0=hidden) >> hidden_launcher.vbs
echo shell.Run """%INSTALL_DIR%\ENIMAS_test.bat""", 0, False >> hidden_launcher.vbs
echo If Err.Number ^<^> 0 Then >> hidden_launcher.vbs
echo     Set logFile = fso.CreateTextFile("%INSTALL_DIR%\launcher_error.log", True) >> hidden_launcher.vbs
echo     logFile.WriteLine "ERROR running batch file: " ^& Err.Description >> hidden_launcher.vbs
echo     logFile.Close >> hidden_launcher.vbs
echo     MsgBox "Error launching ENIMAS. Please check your installation.", 16, "ENIMAS Error" >> hidden_launcher.vbs
echo     Err.Clear >> hidden_launcher.vbs
echo     WScript.Quit 1 >> hidden_launcher.vbs
echo End If >> hidden_launcher.vbs
echo. >> hidden_launcher.vbs
echo Set fso = Nothing >> hidden_launcher.vbs
echo Set shell = Nothing >> hidden_launcher.vbs

if exist "%INSTALL_DIR%\lenses.json" (
    echo Old lenses file found. Replacing default lenses file with old file ...
    cd %INSTALL_DIR%\
    copy /y .\lenses.json .\src\
)

echo.
echo Installation complete.
echo.

pause
