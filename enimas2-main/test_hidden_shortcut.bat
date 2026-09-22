@echo off
echo Testing shortcut creation with hidden window...

:: Define paths
set "BAT_FILE=%~dp0ENIMAS.bat"
set "ICON_FILE=%~dp0UserInterface\imgs\enimas_Icon.ico"
set "VBS_FILE=%TEMP%\hidden_launcher_test.vbs"

:: Verify that the batch file exists
if not exist "%BAT_FILE%" (
    echo ERROR: ENIMAS.bat file not found at %BAT_FILE%
    echo Please make sure the file exists before proceeding.
    pause
    exit /b 1
)

:: Verify that the icon file exists
if not exist "%ICON_FILE%" (
    echo ERROR: Icon file not found at %ICON_FILE%
    echo Please make sure the file exists before proceeding.
    pause
    exit /b 1
)

:: Create the hidden launcher VBS file
echo Creating hidden launcher VBS file...
echo Option Explicit > "%VBS_FILE%"
echo. >> "%VBS_FILE%"
echo ' Create shell object >> "%VBS_FILE%"
echo Dim shell >> "%VBS_FILE%"
echo Set shell = CreateObject("WScript.Shell") >> "%VBS_FILE%"
echo. >> "%VBS_FILE%"
echo ' Run the batch file with window hidden (0=hidden) >> "%VBS_FILE%"
echo shell.Run """%BAT_FILE%""", 0, False >> "%VBS_FILE%"
echo. >> "%VBS_FILE%"
echo Set shell = Nothing >> "%VBS_FILE%"

:: Create a temporary VBScript to create the shortcut
echo Creating test shortcut with hidden window...
echo Set oWS = WScript.CreateObject("WScript.Shell") > "%TEMP%\CreateTestShortcut.vbs"
echo sLinkFile = "%USERPROFILE%\Desktop\ENIMAS2.0_Hidden_Test.lnk" >> "%TEMP%\CreateTestShortcut.vbs"
echo Set oLink = oWS.CreateShortcut(sLinkFile) >> "%TEMP%\CreateTestShortcut.vbs"
echo oLink.TargetPath = "wscript.exe" >> "%TEMP%\CreateTestShortcut.vbs"
echo oLink.Arguments = """%VBS_FILE%""" >> "%TEMP%\CreateTestShortcut.vbs"
echo oLink.IconLocation = "%ICON_FILE%" >> "%TEMP%\CreateTestShortcut.vbs"
echo oLink.Save >> "%TEMP%\CreateTestShortcut.vbs"

:: Run the VBScript to create the shortcut
cscript //nologo "%TEMP%\CreateTestShortcut.vbs"
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Failed to create shortcut.
    pause
    exit /b 1
)

:: Clean up
del "%TEMP%\CreateTestShortcut.vbs"

:: Success message
echo.
echo Success! A test shortcut has been created on your desktop.
echo The shortcut should:
echo  1. Display the ENIMAS icon
echo  2. Launch the ENIMAS application WITHOUT showing a command window
echo.
echo Please check your desktop for "ENIMAS_Hidden_Test.lnk".
echo.
echo If you click the shortcut, it should run the ENIMAS application
echo without displaying a command prompt window.
echo.
echo Note: The test VBS file is saved at %VBS_FILE% and will be used
echo by the shortcut. Do not delete this file if you want to keep
echo using the test shortcut.
echo.

pause
