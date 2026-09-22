@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "INSTALL_ROOT=C:\Program Files\ENIMAS"
set "SYSTEM_PYTHON=C:\Program Files\Python310\python.exe"
set "CONTROL_PARENT=C:\ProgramData\ENIMAS"
set "CONTROL_ROOT=%CONTROL_PARENT%\admin"
set "PROTECTED_INSTALLER=%CONTROL_ROOT%\install.bat"
set "STABLE_UPDATER=%INSTALL_ROOT%\updates\bin\enimas_updater.py"
set "BOOTSTRAP_UPDATER=%INSTALL_ROOT%\updates\bootstrap\enimas_updater.py"
set "CANONICAL_MANIFEST_URL=https://gitlab.kit.edu/kit/iai/ber/enimas2/-/raw/main/update_manifest.json"
set "INSTALLER_REVISION=20260812.5"
set "OFFICIAL_INSTALLER_URL=https://gitlab.kit.edu/kit/iai/ber/enimas2/-/raw/main/install.bat?inline=false"
set "RUNNING_PROTECTED=0"
set "INTERNAL_ADMIN_REQUEST=0"
set "ELEVATION_TEST_MODE=0"
set "ELEVATION_TEST_DROP_ENV=0"
set "UPDATER=%BOOTSTRAP_UPDATER%"
if /I "%~1"=="--test-pnputil-exit" goto test_pnputil_exit
if /I "%~1"=="--test-galaxy-environment" goto test_galaxy_environment
if /I "%~1"=="--test-installer-identity" goto test_installer_identity
if /I "%~1"=="--test-elevation-child" exit /b 42
if /I "%~1"=="--test-elevation-broker" goto test_elevation_broker
if /I "%~1"=="--test-elevation-broker-no-env" goto test_elevation_broker_no_env
if /I "%~1"=="--test-install-user-sid" goto test_install_user_sid
if "%~1"=="" call :show_installer_identity
if /I "%~1"=="--repair" call :show_installer_identity

for %%I in ("%~dp0..\..") do set "SELF_CONTROL_ROOT=%%~fI"
if /I "!SELF_CONTROL_ROOT!"=="%CONTROL_ROOT%" (
    set "RUNNING_PROTECTED=1"
    set "UPDATER=%~dp0enimas_updater.py"
)
if "!RUNNING_PROTECTED!"=="1" set "ENIMAS_MANIFEST_URL=%CANONICAL_MANIFEST_URL%"
if /I "%~1"=="--admin-prepare-fresh" set "ENIMAS_MANIFEST_URL=%CANONICAL_MANIFEST_URL%"
if /I "%~1"=="--admin-prepare-repair" set "ENIMAS_MANIFEST_URL=%CANONICAL_MANIFEST_URL%"
if /I "%~1"=="--admin-apply-staged-fresh" set "ENIMAS_MANIFEST_URL=%CANONICAL_MANIFEST_URL%"
if /I "%~1"=="--admin-apply-staged-repair" set "ENIMAS_MANIFEST_URL=%CANONICAL_MANIFEST_URL%"
if /I "%~1"=="--admin-grant-access" set "ENIMAS_MANIFEST_URL=%CANONICAL_MANIFEST_URL%"
if /I "%~1"=="--admin-finalize" set "ENIMAS_MANIFEST_URL=%CANONICAL_MANIFEST_URL%"
if /I "%~1"=="--admin-bootstrap-control" set "ENIMAS_MANIFEST_URL=%CANONICAL_MANIFEST_URL%"
if /I "%~1"=="--admin-prepare-fresh" set "INTERNAL_ADMIN_REQUEST=1"
if /I "%~1"=="--admin-prepare-repair" set "INTERNAL_ADMIN_REQUEST=1"
if /I "%~1"=="--admin-apply-staged-fresh" set "INTERNAL_ADMIN_REQUEST=1"
if /I "%~1"=="--admin-apply-staged-repair" set "INTERNAL_ADMIN_REQUEST=1"
if /I "%~1"=="--admin-grant-access" set "INTERNAL_ADMIN_REQUEST=1"
if /I "%~1"=="--admin-finalize" set "INTERNAL_ADMIN_REQUEST=1"
if /I "%~1"=="--admin-bootstrap-control" set "INTERNAL_ADMIN_REQUEST=1"
if not defined ENIMAS_MANIFEST_URL set "ENIMAS_MANIFEST_URL=%CANONICAL_MANIFEST_URL%"

set "ENIMAS_INSTALL_USER_SID="
if "!RUNNING_PROTECTED!"=="1" if exist "%CONTROL_ROOT%\owner.sid" set /p "ENIMAS_INSTALL_USER_SID="<"%CONTROL_ROOT%\owner.sid"
if not defined ENIMAS_INSTALL_USER_SID if /I "%~2"=="--caller-sid" set "ENIMAS_INSTALL_USER_SID=%~3"
if not defined ENIMAS_INSTALL_USER_SID for /f "delims=" %%S in ('powershell -NoProfile -Command "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value"') do set "ENIMAS_INSTALL_USER_SID=%%S"
call :validate_install_user
if errorlevel 1 (
    set "OWNER_EXIT=!ERRORLEVEL!"
    if "!INTERNAL_ADMIN_REQUEST!"=="1" (
        call :record_admin_failure "identifying the ENIMAS installation owner" "!OWNER_EXIT!"
        exit /b !OWNER_EXIT!
    )
    set "OWNER_OPERATION=installation"
    if /I "%~1"=="--repair" set "OWNER_OPERATION=Repair"
    call :report_repair_failure "identifying the ENIMAS installation owner" "!OWNER_EXIT!" "!OWNER_OPERATION!"
    exit /b !OWNER_EXIT!
)

if /I "%~1"=="--admin-prepare-fresh" (
    call :admin_prepare_fresh
    set "ADMIN_EXIT=!ERRORLEVEL!"
    if not "!ADMIN_EXIT!"=="0" call :record_admin_failure "administrator camera system preparation" "!ADMIN_EXIT!"
    exit /b !ADMIN_EXIT!
)
if /I "%~1"=="--admin-prepare-repair" (
    call :admin_prepare_repair
    set "ADMIN_EXIT=!ERRORLEVEL!"
    if not "!ADMIN_EXIT!"=="0" call :record_admin_failure "administrator Repair preparation" "!ADMIN_EXIT!"
    exit /b !ADMIN_EXIT!
)
if /I "%~1"=="--admin-apply-staged-fresh" (
    call :admin_apply_staged_fresh
    set "ADMIN_EXIT=!ERRORLEVEL!"
    if not "!ADMIN_EXIT!"=="0" call :record_admin_failure "camera driver and SDK installation" "!ADMIN_EXIT!"
    exit /b !ADMIN_EXIT!
)
if /I "%~1"=="--admin-apply-staged-repair" (
    call :admin_apply_staged_repair
    set "ADMIN_EXIT=!ERRORLEVEL!"
    if not "!ADMIN_EXIT!"=="0" call :record_admin_failure "camera driver and SDK repair" "!ADMIN_EXIT!"
    exit /b !ADMIN_EXIT!
)
if /I "%~1"=="--admin-grant-access" (
    call :admin_grant_access
    set "ADMIN_EXIT=!ERRORLEVEL!"
    if not "!ADMIN_EXIT!"=="0" call :record_admin_failure "installation folder access repair" "!ADMIN_EXIT!"
    exit /b !ADMIN_EXIT!
)
if /I "%~1"=="--admin-finalize" (
    call :admin_finalize
    set "ADMIN_EXIT=!ERRORLEVEL!"
    if not "!ADMIN_EXIT!"=="0" call :record_admin_failure "protected installer activation" "!ADMIN_EXIT!"
    exit /b !ADMIN_EXIT!
)
if /I "%~1"=="--admin-bootstrap-control" (
    call :admin_bootstrap_control
    set "ADMIN_EXIT=!ERRORLEVEL!"
    if not "!ADMIN_EXIT!"=="0" call :record_admin_failure "protected updater preparation" "!ADMIN_EXIT!"
    exit /b !ADMIN_EXIT!
)
if "!RUNNING_PROTECTED!"=="1" (
    echo Protected ENIMAS installer accepts only authenticated administrator broker operations.
    exit /b 1
)
if /I "%~1"=="--repair" goto user_repair

call :is_admin
if not errorlevel 1 (
    echo For security, run install.bat normally instead of using "Run as administrator".
    echo ENIMAS will request elevation only for authenticated system-maintenance steps.
    pause
    exit /b 1
)

:: Existing-install detection intentionally happens before any UAC request.
if exist "%INSTALL_ROOT%\src\constants.py" if exist "%INSTALL_ROOT%\src\venv\Scripts\python.exe" goto existing_update
if exist "%INSTALL_ROOT%\src" goto damaged_install
if exist "%INSTALL_ROOT%" goto damaged_install
goto user_fresh

:existing_update
call :select_python
if errorlevel 1 goto existing_failed
:existing_retry
call :ensure_updater
if errorlevel 1 goto existing_failed
"%PYTHON_EXE%" "%UPDATER%" --install-root "%INSTALL_ROOT%" migration-status
set "MIGRATION_EXIT=!ERRORLEVEL!"
if "!MIGRATION_EXIT!"=="22" goto legacy_repair_required
if not "!MIGRATION_EXIT!"=="0" goto existing_failed
call :ensure_protected_control
if errorlevel 1 goto existing_failed
"%PYTHON_EXE%" "%UPDATER%" --install-root "%INSTALL_ROOT%" installation-status
set "STATUS_EXIT=!ERRORLEVEL!"
if "!STATUS_EXIT!"=="20" goto user_repair
if not "!STATUS_EXIT!"=="0" goto existing_failed
"%PYTHON_EXE%" "%UPDATER%" --install-root "%INSTALL_ROOT%" --manifest-url "%ENIMAS_MANIFEST_URL%" bootstrap
set "UPDATE_EXIT=!ERRORLEVEL!"
if "!UPDATE_EXIT!"=="0" (
    echo ENIMAS was updated successfully.
    exit /b 0
)
if "!UPDATE_EXIT!"=="12" (
    echo ENIMAS is already current. No files were changed.
    exit /b 0
)
if "!UPDATE_EXIT!"=="11" (
    echo ENIMAS or another updater is active. Close ENIMAS and retry.
    exit /b 11
)
if "!UPDATE_EXIT!"=="21" goto user_repair
if "!UPDATE_EXIT!"=="22" goto legacy_repair_required
goto existing_failed

:legacy_repair_required
echo.
echo This ENIMAS installation cannot be updated incrementally with verified safety.
echo Repair preserves detected configuration, lenses, models, plugins, outputs, and compatible environment data.
choice /C RE /N /T 30 /D E /M "Choose [R]epair or [E]xit (default: Exit): "
if errorlevel 2 exit /b 22
goto user_repair

:existing_failed
echo.
echo The installed ENIMAS application was not changed.
choice /C RPAE /N /M "[R]etry, full re[P]air, repair folder [A]ccess, or [E]xit? "
if errorlevel 4 exit /b 1
if errorlevel 3 (
    call :elevate_wait --admin-grant-access
    if errorlevel 1 (
        set "REPAIR_EXIT=!ERRORLEVEL!"
        call :report_repair_failure "repairing installation folder access" "!REPAIR_EXIT!"
        exit /b !REPAIR_EXIT!
    )
    goto existing_retry
)
if errorlevel 2 goto user_repair
goto existing_retry

:damaged_install
echo.
echo ENIMAS files are present, but the installation is incomplete or damaged.
echo User configuration, lenses, models, plugins, and outputs will not be erased.
choice /C RE /N /T 30 /D E /M "Choose [R]epair or [E]xit (default: Exit): "
if errorlevel 2 exit /b 1
goto user_repair

:user_fresh
call :require_unelevated_user
if errorlevel 1 exit /b 1
call :elevate_wait --admin-prepare-fresh
if errorlevel 1 (
    echo Authenticated ENIMAS system preparation failed. Application files were not activated.
    pause
    exit /b 1
)
call :configure_galaxy_environment "C:\Program Files\Daheng Imaging\GalaxySDK\GenICam"
if errorlevel 1 (
    echo Galaxy camera support was not activated. Application files were not installed.
    pause
    exit /b 1
)
set "INSTALL_REPAIR_SWITCH="
goto user_source_setup

:user_repair
call :require_unelevated_user
if errorlevel 1 (
    set "REPAIR_EXIT=!ERRORLEVEL!"
    call :report_repair_failure "checking the non-administrator Repair session" "!REPAIR_EXIT!"
    exit /b !REPAIR_EXIT!
)
set "REPAIR_SYSTEM_PREPARED=0"
call :ensure_install_write_access
if errorlevel 1 (
    call :elevate_wait --admin-grant-access
    if errorlevel 1 (
        set "REPAIR_EXIT=!ERRORLEVEL!"
        call :report_repair_failure "repairing installation folder access" "!REPAIR_EXIT!"
        exit /b !REPAIR_EXIT!
    )
    call :ensure_install_write_access
    if errorlevel 1 (
        set "REPAIR_EXIT=!ERRORLEVEL!"
        call :report_repair_failure "verifying repaired installation folder access" "!REPAIR_EXIT!"
        exit /b !REPAIR_EXIT!
    )
)
call :select_python
if errorlevel 1 (
    echo System Python is missing. A fresh system preparation is required.
    call :elevate_wait --admin-prepare-fresh
    if errorlevel 1 (
        set "REPAIR_EXIT=!ERRORLEVEL!"
        call :report_repair_failure "installing the supported system Python and camera components" "!REPAIR_EXIT!"
        exit /b !REPAIR_EXIT!
    )
    set "REPAIR_SYSTEM_PREPARED=1"
    call :select_python
    if errorlevel 1 (
        set "REPAIR_EXIT=!ERRORLEVEL!"
        call :report_repair_failure "verifying the supported system Python" "!REPAIR_EXIT!"
        exit /b !REPAIR_EXIT!
    )
)
call :ensure_updater
if errorlevel 1 (
    set "REPAIR_EXIT=!ERRORLEVEL!"
    call :report_repair_failure "downloading and verifying the Repair updater" "!REPAIR_EXIT!"
    exit /b !REPAIR_EXIT!
)
call :ensure_protected_control
if errorlevel 1 (
    set "REPAIR_EXIT=!ERRORLEVEL!"
    call :report_repair_failure "preparing the protected Repair service" "!REPAIR_EXIT!"
    exit /b !REPAIR_EXIT!
)
"%PYTHON_EXE%" "%UPDATER%" --install-root "%INSTALL_ROOT%" application-status
set "APP_STATUS_EXIT=!ERRORLEVEL!"
if "!APP_STATUS_EXIT!"=="11" (
    call :report_repair_failure "waiting for ENIMAS to close" "!APP_STATUS_EXIT!"
    exit /b !APP_STATUS_EXIT!
)
if not "!APP_STATUS_EXIT!"=="0" (
    call :report_repair_failure "checking whether ENIMAS is running" "!APP_STATUS_EXIT!"
    exit /b !APP_STATUS_EXIT!
)
if "!REPAIR_SYSTEM_PREPARED!"=="0" (
    echo.
    echo Full Repair will verify and reinstall the ENIMAS camera system components.
    echo Keep this window open after approving the Windows administrator prompt.
    call :elevate_wait --admin-prepare-fresh
    if errorlevel 1 (
        set "REPAIR_EXIT=!ERRORLEVEL!"
        call :report_repair_failure "repairing the camera drivers and Galaxy SDK" "!REPAIR_EXIT!"
        exit /b !REPAIR_EXIT!
    )
)
call :configure_galaxy_environment "C:\Program Files\Daheng Imaging\GalaxySDK\GenICam"
if errorlevel 1 (
    set "REPAIR_EXIT=!ERRORLEVEL!"
    call :report_repair_failure "verifying the repaired Galaxy camera SDK" "!REPAIR_EXIT!"
    exit /b !REPAIR_EXIT!
)
set "INSTALL_REPAIR_SWITCH=--repair"
goto user_source_setup

:user_source_setup
call :select_python
if errorlevel 1 goto user_setup_failed
call :ensure_updater
if errorlevel 1 goto user_setup_failed
"%PYTHON_EXE%" "%UPDATER%" --install-root "%INSTALL_ROOT%" --manifest-url "%ENIMAS_MANIFEST_URL%" fresh-install %INSTALL_REPAIR_SWITCH%
set "SOURCE_PREP_EXIT=!ERRORLEVEL!"
if "!SOURCE_PREP_EXIT!"=="11" (
    echo ENIMAS or another installer is active. Close it and retry.
    exit /b 11
)
if not "!SOURCE_PREP_EXIT!"=="0" goto user_setup_failed

if exist "%INSTALL_ROOT%\src\venv" if not exist "%INSTALL_ROOT%\src\venv\Scripts\python.exe" rmdir /s /q "%INSTALL_ROOT%\src\venv"
if not exist "%INSTALL_ROOT%\src\venv\Scripts\python.exe" (
    echo Creating the ENIMAS virtual environment...
    "%SYSTEM_PYTHON%" -m venv "%INSTALL_ROOT%\src\venv"
    if errorlevel 1 goto user_setup_failed
)
echo Preparing verified Python dependencies...
pushd "%INSTALL_ROOT%\src"
"%INSTALL_ROOT%\src\venv\Scripts\python.exe" -m pip install --no-cache-dir --require-hashes -r "%INSTALL_ROOT%\src\requirements.lock"
set "DEPENDENCY_EXIT=!ERRORLEVEL!"
popd
if not "!DEPENDENCY_EXIT!"=="0" (
    set "SETUP_EXIT=!DEPENDENCY_EXIT!"
    goto user_setup_failed_known
)
"%PYTHON_EXE%" "%UPDATER%" --install-root "%INSTALL_ROOT%" sync-dependencies
if errorlevel 1 goto user_setup_failed

"%PYTHON_EXE%" "%UPDATER%" --install-root "%INSTALL_ROOT%" validate-install
if errorlevel 1 goto user_setup_failed
"%PYTHON_EXE%" "%UPDATER%" --install-root "%INSTALL_ROOT%" complete-install
if errorlevel 1 goto user_setup_failed

call :create_launchers
if errorlevel 1 echo Warning: ENIMAS is healthy, but one or more shortcuts could not be refreshed.
if exist "%CONTROL_ROOT%\pending.version" (
    call :elevate_wait --admin-finalize
    if errorlevel 1 echo Warning: protected installer activation is pending; ENIMAS itself is installed.
)
"%PYTHON_EXE%" "%UPDATER%" --install-root "%INSTALL_ROOT%" cleanup-install

echo.
echo ============================================================
echo ENIMAS installation completed and passed its health check.
echo Existing user-owned data was preserved.
echo ============================================================
pause
exit /b 0

:user_setup_failed
set "SETUP_EXIT=!ERRORLEVEL!"
:user_setup_failed_known
echo.
echo ENIMAS setup failed. Restoring the previous application when available...
if exist "%PYTHON_EXE%" if exist "%UPDATER%" "%PYTHON_EXE%" "%UPDATER%" --install-root "%INSTALL_ROOT%" abort-install --error "Installer step failed with exit !SETUP_EXIT!"
echo No user data was intentionally removed. See "%INSTALL_ROOT%\updates\update.log".
pause
exit /b 1

:admin_prepare_fresh
call :require_admin
if errorlevel 1 exit /b 1
call :prepare_admin_roots
if errorlevel 1 exit /b 1
call :ensure_system_python
if errorlevel 1 exit /b 1
set "PYTHON_EXE=%SYSTEM_PYTHON%"
set "UPDATER=%CONTROL_ROOT%\bootstrap\enimas_updater.py"
call :ensure_updater
if errorlevel 1 exit /b 1
"%SYSTEM_PYTHON%" "%UPDATER%" --install-root "%INSTALL_ROOT%" --manifest-url "%CANONICAL_MANIFEST_URL%" prepare-system --control-root "%CONTROL_ROOT%" --fresh
if errorlevel 1 exit /b 1
call :protect_admin_payload_dir "%CONTROL_ROOT%"
if errorlevel 1 exit /b 1
call :chain_staged_admin --admin-apply-staged-fresh
exit /b !ERRORLEVEL!

:admin_bootstrap_control
call :require_admin
if errorlevel 1 exit /b 1
call :prepare_admin_roots
if errorlevel 1 exit /b 1
call :validate_existing_system_python
if errorlevel 1 (
    echo The existing ENIMAS system Python is unavailable. Use full Repair instead.
    exit /b 1
)
set "PYTHON_EXE=%SYSTEM_PYTHON%"
set "UPDATER=%CONTROL_ROOT%\bootstrap\enimas_updater.py"
call :ensure_updater
if errorlevel 1 exit /b 1
"%SYSTEM_PYTHON%" "%UPDATER%" --install-root "%INSTALL_ROOT%" --manifest-url "%CANONICAL_MANIFEST_URL%" prepare-system --control-root "%CONTROL_ROOT%" --control-only
if errorlevel 1 exit /b 1
call :protect_admin_payload_dir "%CONTROL_ROOT%"
if errorlevel 1 exit /b 1
call :load_pending_control_version
if errorlevel 1 exit /b 1
"%SYSTEM_PYTHON%" "%CONTROL_VERSION_ROOT%\enimas_updater.py" --install-root "%INSTALL_ROOT%" activate-control-plane --control-root "%CONTROL_ROOT%"
if errorlevel 1 exit /b 1
call :protect_admin_payload_dir "%CONTROL_ROOT%"
if errorlevel 1 exit /b 1
if not exist "%PROTECTED_INSTALLER%" exit /b 1
exit /b 0

:admin_prepare_repair
call :require_admin
if errorlevel 1 exit /b 1
call :prepare_admin_roots
if errorlevel 1 exit /b 1
call :ensure_system_python
if errorlevel 1 exit /b 1
set "PYTHON_EXE=%SYSTEM_PYTHON%"
set "UPDATER=%CONTROL_ROOT%\bootstrap\enimas_updater.py"
call :ensure_updater
if errorlevel 1 exit /b 1
"%SYSTEM_PYTHON%" "%UPDATER%" --install-root "%INSTALL_ROOT%" --manifest-url "%CANONICAL_MANIFEST_URL%" prepare-system --control-root "%CONTROL_ROOT%"
if errorlevel 1 exit /b 1
call :protect_admin_payload_dir "%CONTROL_ROOT%"
if errorlevel 1 exit /b 1
call :chain_staged_admin --admin-apply-staged-repair
exit /b !ERRORLEVEL!

:admin_apply_staged_fresh
call :require_admin
if errorlevel 1 exit /b 1
set "SYSTEM_MODE_FRESH=1"
goto admin_apply_system

:admin_apply_staged_repair
call :require_admin
if errorlevel 1 exit /b 1
set "SYSTEM_MODE_FRESH=0"
goto admin_apply_system

:admin_apply_system
call :load_pending_control_version
if errorlevel 1 exit /b 1
set "INSTALLER_CACHE=%CONTROL_ROOT%\installers"
if not exist "%INSTALLER_CACHE%" mkdir "%INSTALLER_CACHE%"
if errorlevel 1 exit /b 1

call :ensure_system_python
if errorlevel 1 exit /b 1

call :install_vcredist x64
if errorlevel 1 exit /b 1
call :install_vcredist x86
if errorlevel 1 exit /b 1

set "SYSTEM_PAYLOAD_ROOT=%CONTROL_VERSION_ROOT%\payloads"
if /I "%PROCESSOR_ARCHITECTURE%"=="x86" (
    set "ARDU_CAT=%SYSTEM_PAYLOAD_ROOT%\camera-driver\x86\cyusb3.cat"
    set "ARDU_INF=%SYSTEM_PAYLOAD_ROOT%\camera-driver\x86\cyusb3.inf"
) else (
    set "ARDU_CAT=%SYSTEM_PAYLOAD_ROOT%\camera-driver\x64\cyusb3.cat"
    set "ARDU_INF=%SYSTEM_PAYLOAD_ROOT%\camera-driver\x64\cyusb3.inf"
)
call :verify_signature "!ARDU_CAT!" "eyesDx, Inc"
if errorlevel 1 exit /b 1
pnputil.exe /add-driver "!ARDU_INF!" /install
set "PNP_EXIT=!ERRORLEVEL!"
if "!PNP_EXIT!"=="3010" set "REBOOT_REQUIRED=1"
if "!PNP_EXIT!"=="1641" set "REBOOT_REQUIRED=1"
call :is_pnputil_success "!PNP_EXIT!"
if errorlevel 1 exit /b 1

set "VA_INSTALLER=%SYSTEM_PAYLOAD_ROOT%\camera-driver\Vaimaging\Galaxy_Windows_EN_32bits-64bits_2.3.2410.9292.exe"
call :verify_hash "%VA_INSTALLER%" "57F89B3977781BBE62A0FD7C5C1359BCF256DDFC98C2DB7296D5A0851B8EA863"
if errorlevel 1 (
    echo The unsigned VAImaging vendor installer did not match its approved release digest.
    exit /b 1
)
call :configure_galaxy_environment "C:\Program Files\Daheng Imaging\GalaxySDK\GenICam" >nul 2>&1
if not errorlevel 1 goto galaxy_sdk_ready
start "" /wait "%VA_INSTALLER%"
set "VA_EXIT=!ERRORLEVEL!"
if "!VA_EXIT!"=="3010" set "REBOOT_REQUIRED=1"
if not "!VA_EXIT!"=="0" if not "!VA_EXIT!"=="3010" exit /b 1
call :configure_galaxy_environment "C:\Program Files\Daheng Imaging\GalaxySDK\GenICam"
if errorlevel 1 (
    echo The Galaxy SDK installer completed, but its required runtime files are missing.
    exit /b 1
)
:galaxy_sdk_ready
setx GALAXY_GENICAM_ROOT "%GALAXY_GENICAM_ROOT%" /M >nul 2>&1
if errorlevel 1 exit /b 1
if defined REBOOT_REQUIRED echo Windows reports that a reboot is required to finish driver maintenance.
exit /b 0

:admin_grant_access
call :require_admin
if errorlevel 1 exit /b 1
call :prepare_admin_roots
exit /b !ERRORLEVEL!

:admin_finalize
call :require_admin
if errorlevel 1 exit /b 1
if "!RUNNING_PROTECTED!"=="1" (
    set "UPDATER=%~dp0enimas_updater.py"
) else (
    set "UPDATER=%CONTROL_ROOT%\bootstrap\enimas_updater.py"
)
if not exist "%UPDATER%" exit /b 1
"%SYSTEM_PYTHON%" "%UPDATER%" --install-root "%INSTALL_ROOT%" activate-control-plane --control-root "%CONTROL_ROOT%"
if errorlevel 1 exit /b 1
call :protect_admin_payload_dir "%CONTROL_ROOT%"
exit /b !ERRORLEVEL!

:prepare_admin_roots
if not exist "%INSTALL_ROOT%" mkdir "%INSTALL_ROOT%"
if errorlevel 1 exit /b 1
call :reject_reparse_point "%INSTALL_ROOT%"
if errorlevel 1 exit /b 1
if not exist "%CONTROL_PARENT%" mkdir "%CONTROL_PARENT%"
if errorlevel 1 exit /b 1
call :reject_reparse_point "%CONTROL_PARENT%"
if errorlevel 1 exit /b 1
if not exist "%CONTROL_ROOT%" mkdir "%CONTROL_ROOT%"
if errorlevel 1 exit /b 1
call :reject_reparse_point "%CONTROL_ROOT%"
if errorlevel 1 exit /b 1
call :protect_admin_payload_dir "%CONTROL_ROOT%"
if errorlevel 1 exit /b 1
>"%CONTROL_ROOT%\owner.sid" echo %ENIMAS_INSTALL_USER_SID%
call :protect_admin_payload_dir "%CONTROL_ROOT%"
if errorlevel 1 exit /b 1
call :protect_install_tree
if errorlevel 1 exit /b 1
call :verify_install_acl
if errorlevel 1 exit /b 1
>"%CONTROL_ROOT%\install-acl.version" echo 1
call :protect_admin_payload_dir "%CONTROL_ROOT%"
exit /b !ERRORLEVEL!

:load_pending_control_version
if not exist "%CONTROL_ROOT%\pending.version" exit /b 1
set /p "PENDING_VERSION="<"%CONTROL_ROOT%\pending.version"
echo(!PENDING_VERSION!| findstr /R /X "v[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*" >nul
if errorlevel 1 exit /b 1
set "CONTROL_VERSION_ROOT=%CONTROL_ROOT%\versions\!PENDING_VERSION!"
if not exist "!CONTROL_VERSION_ROOT!\install.bat" exit /b 1
exit /b 0

:chain_staged_admin
call :load_pending_control_version
if errorlevel 1 exit /b 1
set "CANDIDATE_INSTALLER=%CONTROL_VERSION_ROOT%\install.bat"
if /I "%~f0"=="!CANDIDATE_INSTALLER!" (
    if /I "%~1"=="--admin-apply-staged-fresh" goto admin_apply_staged_fresh
    if /I "%~1"=="--admin-apply-staged-repair" goto admin_apply_staged_repair
    exit /b 1
)
call "!CANDIDATE_INSTALLER!" "%~1" "--caller-sid" "%ENIMAS_INSTALL_USER_SID%"
exit /b !ERRORLEVEL!

:ensure_install_write_access
if not exist "%INSTALL_ROOT%" exit /b 1
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=Join-Path $env:INSTALL_ROOT ('.enimas-write-'+[Guid]::NewGuid().ToString('N')); try{[IO.File]::WriteAllText($p,'test'); Remove-Item -LiteralPath $p -Force; exit 0}catch{exit 1}" >nul 2>&1
exit /b !ERRORLEVEL!

:ensure_protected_control
if exist "%PROTECTED_INSTALLER%" if exist "%CONTROL_ROOT%\current.version" if exist "%CONTROL_ROOT%\install-acl.version" (
    call :verify_install_acl
    if not errorlevel 1 exit /b 0
)
echo.
echo This older ENIMAS installation needs one-time Windows administrator approval
echo to secure the updater and installation permissions.
echo This does not reinstall ENIMAS, Python packages, camera drivers, or the virtual environment.
echo The update process is active. After approving the Windows prompt, preparation
echo may take a few minutes; keep this window open and ENIMAS will continue automatically.
call :run_elevated "%~f0" "--admin-bootstrap-control"
set "CONTROL_BOOTSTRAP_EXIT=!ERRORLEVEL!"
if not "!CONTROL_BOOTSTRAP_EXIT!"=="0" (
    echo The updater security migration was cancelled or failed. ENIMAS was not updated.
    exit /b !CONTROL_BOOTSTRAP_EXIT!
)
echo One-time security preparation completed. Starting the incremental update...
if not exist "%PROTECTED_INSTALLER%" exit /b 1
if not exist "%CONTROL_ROOT%\current.version" exit /b 1
if not exist "%CONTROL_ROOT%\install-acl.version" exit /b 1
call :verify_install_acl
exit /b !ERRORLEVEL!

:select_python
set "PYTHON_EXE="
if exist "%SYSTEM_PYTHON%" set "PYTHON_EXE=%SYSTEM_PYTHON%"
if not defined PYTHON_EXE for /f "delims=" %%P in ('where python 2^>nul') do if not defined PYTHON_EXE set "PYTHON_EXE=%%P"
if not defined PYTHON_EXE exit /b 1
"%PYTHON_EXE%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
exit /b !ERRORLEVEL!

:ensure_updater
if not defined PYTHON_EXE (
    call :select_python
    if errorlevel 1 exit /b 1
)
for %%D in ("%UPDATER%") do set "UPDATER_DIR=%%~dpD"
if not exist "!UPDATER_DIR!" mkdir "!UPDATER_DIR!" >nul 2>&1
set "BOOTSTRAP_TMP=%UPDATER%.!RANDOM!.part"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $m=Invoke-RestMethod -Uri $env:ENIMAS_MANIFEST_URL -TimeoutSec 30; if($m.schema_version -ne 1){throw 'Unsupported manifest'}; $expected=([string]$m.updater.sha256).ToLowerInvariant(); if(Test-Path -LiteralPath $env:UPDATER){$current=(Get-FileHash -Algorithm SHA256 -LiteralPath $env:UPDATER).Hash.ToLowerInvariant(); if($current -eq $expected){exit 0}}; $ref=[string]$m.source_ref; $path=[string]$m.updater.path; if($ref -notmatch '^[A-Za-z0-9._-]+$' -or $path -notmatch '^[A-Za-z0-9_./-]+$' -or $path -match '(^|/)\.\.(/|$)'){throw 'Unsafe updater metadata'}; $parts=$path.Split('/') | ForEach-Object {[Uri]::EscapeDataString($_)}; $base=([string]$m.repository_raw_url).Replace('{ref}',[Uri]::EscapeDataString($ref)).TrimEnd('/'); $url=$base+'/'+($parts -join '/'); Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $env:BOOTSTRAP_TMP -TimeoutSec 60; $hash=(Get-FileHash -Algorithm SHA256 -LiteralPath $env:BOOTSTRAP_TMP).Hash.ToLowerInvariant(); if($hash -ne $expected){throw 'Updater checksum mismatch'}; Move-Item -Force -LiteralPath $env:BOOTSTRAP_TMP -Destination $env:UPDATER"
set "BOOTSTRAP_EXIT=!ERRORLEVEL!"
if exist "%BOOTSTRAP_TMP%" del /q "%BOOTSTRAP_TMP%" >nul 2>&1
if not "!BOOTSTRAP_EXIT!"=="0" exit /b !BOOTSTRAP_EXIT!
"%PYTHON_EXE%" -m py_compile "%UPDATER%" >nul 2>&1
exit /b !ERRORLEVEL!

:install_vcredist
set "VC_ARCH=%~1"
reg query "HKLM\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\%VC_ARCH%" /v Version >nul 2>&1
if not errorlevel 1 exit /b 0
set "VC_FILE=%INSTALLER_CACHE%\vc_redist.%VC_ARCH%.exe"
curl.exe -L --fail --retry 3 --retry-delay 2 -C - "https://aka.ms/vs/17/release/vc_redist.%VC_ARCH%.exe" -o "%VC_FILE%"
if errorlevel 1 exit /b 1
call :verify_signature "%VC_FILE%" "Microsoft Corporation"
if errorlevel 1 exit /b 1
"%VC_FILE%" /install /quiet /norestart
set "VC_EXIT=!ERRORLEVEL!"
if "!VC_EXIT!"=="0" exit /b 0
if "!VC_EXIT!"=="1638" exit /b 0
if "!VC_EXIT!"=="3010" exit /b 0
exit /b 1

:ensure_system_python
set "PYTHON_NEEDS_INSTALL=1"
if exist "%SYSTEM_PYTHON%" (
    "%SYSTEM_PYTHON%" -c "import sys; raise SystemExit(0 if (3,10,11) <= sys.version_info[:3] < (3,11,0) else 1)" >nul 2>&1
    if not errorlevel 1 set "PYTHON_NEEDS_INSTALL=0"
)
if "!PYTHON_NEEDS_INSTALL!"=="0" exit /b 0
set "INSTALLER_CACHE=%CONTROL_ROOT%\installers"
if not exist "%INSTALLER_CACHE%" mkdir "%INSTALLER_CACHE%"
if errorlevel 1 exit /b 1
echo Downloading the final supported Python 3.10 Windows installer...
curl.exe -L --fail --retry 3 --retry-delay 2 -C - "https://www.python.org/ftp/python/3.10.11/python-3.10.11-amd64.exe" -o "%INSTALLER_CACHE%\python-3.10.11.exe"
if errorlevel 1 exit /b 1
call :verify_signature "%INSTALLER_CACHE%\python-3.10.11.exe" "Python Software Foundation"
if errorlevel 1 exit /b 1
"%INSTALLER_CACHE%\python-3.10.11.exe" /passive InstallAllUsers=1 TargetDir="C:\Program Files\Python310" PrependPath=1 Include_doc=0 Include_test=0 Include_tcltk=0
if errorlevel 1 exit /b 1
if not exist "%SYSTEM_PYTHON%" exit /b 1
exit /b 0

:validate_existing_system_python
if not exist "%SYSTEM_PYTHON%" exit /b 1
"%SYSTEM_PYTHON%" -c "import sys; raise SystemExit(0 if (3,10,0) <= sys.version_info[:3] < (3,11,0) else 1)" >nul 2>&1
exit /b !ERRORLEVEL!

:verify_signature
set "SIGN_FILE=%~1"
set "SIGNER_PATTERN=%~2"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=Get-AuthenticodeSignature -LiteralPath $env:SIGN_FILE; if($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notlike ('*'+$env:SIGNER_PATTERN+'*')){exit 1}"
exit /b !ERRORLEVEL!

:verify_hash
set "HASH_FILE=%~1"
set "EXPECTED_HASH=%~2"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$h=(Get-FileHash -Algorithm SHA256 -LiteralPath $env:HASH_FILE).Hash; if($h -ne $env:EXPECTED_HASH){exit 1}"
exit /b !ERRORLEVEL!

:test_galaxy_environment
call :configure_galaxy_environment "%~2"
exit /b !ERRORLEVEL!

:test_pnputil_exit
call :is_pnputil_success "%~2"
exit /b !ERRORLEVEL!

:test_install_user_sid
set "SID_CANDIDATE=%~2"
call :is_supported_install_user_sid
exit /b !ERRORLEVEL!

:is_pnputil_success
if "%~1"=="0" exit /b 0
if "%~1"=="259" exit /b 0
if "%~1"=="3010" exit /b 0
if "%~1"=="1641" exit /b 0
exit /b 1

:configure_galaxy_environment
set "GALAXY_GENICAM_ROOT=%~1"
if not defined GALAXY_GENICAM_ROOT goto galaxy_environment_incomplete
for %%D in ("%GALAXY_GENICAM_ROOT%\..") do set "GALAXY_SDK_ROOT=%%~fD"
if not exist "%GALAXY_GENICAM_ROOT%\bin\Win32_i86" goto galaxy_environment_incomplete
if not exist "%GALAXY_SDK_ROOT%\APIDll\Win32\GxIAPI.dll" goto galaxy_environment_incomplete
if /I "%PROCESSOR_ARCHITECTURE%"=="x86" if not defined PROCESSOR_ARCHITEW6432 exit /b 0
if not exist "%GALAXY_GENICAM_ROOT%\bin\Win64_x64" goto galaxy_environment_incomplete
if not exist "%GALAXY_SDK_ROOT%\APIDll\Win64\GxIAPI.dll" goto galaxy_environment_incomplete
exit /b 0

:galaxy_environment_incomplete
echo Galaxy SDK installation is incomplete at "%GALAXY_GENICAM_ROOT%".
set "GALAXY_GENICAM_ROOT="
set "GALAXY_SDK_ROOT="
exit /b 1

:create_launchers
for /f "delims=" %%P in ('powershell -NoProfile -Command "(Get-ItemProperty -LiteralPath ('Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\'+$env:ENIMAS_INSTALL_USER_SID) -Name ProfileImagePath).ProfileImagePath"') do set "INSTALL_USER_PROFILE=%%P"
if not defined INSTALL_USER_PROFILE exit /b 1
if not exist "%INSTALL_USER_PROFILE%\Desktop" mkdir "%INSTALL_USER_PROFILE%\Desktop"
if not exist "%INSTALL_USER_PROFILE%\AppData\Roaming\Microsoft\Windows\Start Menu\Programs" mkdir "%INSTALL_USER_PROFILE%\AppData\Roaming\Microsoft\Windows\Start Menu\Programs"
copy /y "%INSTALL_ROOT%\src\install.bat" "%INSTALL_ROOT%\install.bat" >nul
if errorlevel 1 exit /b 1
copy /y "%INSTALL_ROOT%\src\ENIMAS.bat" "%INSTALL_ROOT%\ENIMAS.bat" >nul
if errorlevel 1 exit /b 1
(
echo Option Explicit
echo Dim shell
echo Set shell = CreateObject("WScript.Shell"^)
echo shell.CurrentDirectory = "%INSTALL_ROOT%"
echo shell.Run """%INSTALL_ROOT%\ENIMAS.bat""", 0, False
) > "%INSTALL_ROOT%\hidden_launcher.vbs"
(
echo Set shell = CreateObject("WScript.Shell"^)
echo Set link = shell.CreateShortcut("%INSTALL_USER_PROFILE%\Desktop\ENIMAS2.0.lnk"^)
echo link.TargetPath = "wscript.exe"
echo link.Arguments = """%INSTALL_ROOT%\hidden_launcher.vbs"""
echo link.WorkingDirectory = "%INSTALL_ROOT%"
echo link.IconLocation = "%INSTALL_ROOT%\src\UserInterface\imgs\enimas_Icon.ico"
echo link.Save
echo Set link = shell.CreateShortcut("%INSTALL_USER_PROFILE%\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\ENIMAS2.0.lnk"^)
echo link.TargetPath = "wscript.exe"
echo link.Arguments = """%INSTALL_ROOT%\hidden_launcher.vbs"""
echo link.WorkingDirectory = "%INSTALL_ROOT%"
echo link.IconLocation = "%INSTALL_ROOT%\src\UserInterface\imgs\enimas_Icon.ico"
echo link.Save
) > "%INSTALL_ROOT%\updates\create_shortcuts.vbs"
cscript //nologo "%INSTALL_ROOT%\updates\create_shortcuts.vbs"
exit /b !ERRORLEVEL!

:show_installer_identity
echo.
echo ENIMAS installer revision: %INSTALLER_REVISION%
echo Official installer: %OFFICIAL_INSTALLER_URL%
exit /b 0

:test_installer_identity
call :show_installer_identity
exit /b !ERRORLEVEL!

:report_repair_failure
set "INSTALLER_FAILURE_STEP=%~1"
set "INSTALLER_FAILURE_EXIT=%~2"
set "INSTALLER_FAILURE_OPERATION=%~3"
if not defined INSTALLER_FAILURE_EXIT set "INSTALLER_FAILURE_EXIT=1"
if not defined INSTALLER_FAILURE_OPERATION set "INSTALLER_FAILURE_OPERATION=Repair"
call :record_installer_failure "%INSTALLER_FAILURE_STEP%" "%INSTALLER_FAILURE_EXIT%"
echo.
echo ============================================================
echo ENIMAS %INSTALLER_FAILURE_OPERATION% did not complete.
echo Failed step: %INSTALLER_FAILURE_STEP% ^(exit %INSTALLER_FAILURE_EXIT%^)
if "%INSTALLER_FAILURE_EXIT%"=="1223" echo Windows administrator approval was cancelled or could not be started.
if /I "%INSTALLER_FAILURE_STEP%"=="waiting for ENIMAS to close" echo Close ENIMAS, then run this installer again.
echo Existing ENIMAS files and user data were not removed.
if defined INSTALLER_FAILURE_LOG echo Diagnostic log for this failure: "%INSTALLER_FAILURE_LOG%"
if not defined INSTALLER_FAILURE_LOG echo A diagnostic log could not be written; send a photo of this window.
echo Installer revision: %INSTALLER_REVISION%
echo Keep this window open and send the failed step, revision, and diagnostic log for support.
echo To retry with a newly downloaded installer, use:
echo %OFFICIAL_INSTALLER_URL%
echo ============================================================
pause
exit /b 0

:record_installer_failure
set "INSTALLER_FAILURE_STEP=%~1"
set "INSTALLER_FAILURE_EXIT=%~2"
set "INSTALLER_FAILURE_LOG="
if not defined INSTALLER_FAILURE_EXIT set "INSTALLER_FAILURE_EXIT=1"
call :is_admin
if not errorlevel 1 (
    call :record_admin_failure "%INSTALLER_FAILURE_STEP%" "%INSTALLER_FAILURE_EXIT%"
    exit /b 0
)
for /f "usebackq delims=" %%L in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$line=('{0:o} ERROR ENIMAS installer {1}: {2} (exit {3})' -f [DateTime]::UtcNow,$env:INSTALLER_REVISION,$env:INSTALLER_FAILURE_STEP,$env:INSTALLER_FAILURE_EXIT); $targets=@((Join-Path $env:INSTALL_ROOT 'updates\update.log'),(Join-Path $env:ProgramData 'ENIMAS\installer-fallback.log'),(Join-Path $env:LOCALAPPDATA 'ENIMAS\installer-fallback.log')); foreach($target in $targets){try{$null=[IO.Directory]::CreateDirectory((Split-Path -Parent $target)); [IO.File]::AppendAllText($target,$line+[Environment]::NewLine); Write-Output $target; exit 0}catch{}}; exit 1" 2^>nul`) do if not defined INSTALLER_FAILURE_LOG set "INSTALLER_FAILURE_LOG=%%L"
exit /b 0

:record_admin_failure
set "INSTALLER_FAILURE_STEP=%~1"
set "INSTALLER_FAILURE_EXIT=%~2"
set "INSTALLER_FAILURE_LOG="
if not exist "%CONTROL_ROOT%\install-acl.version" exit /b 0
call :reject_reparse_point "%CONTROL_ROOT%"
if errorlevel 1 exit /b 0
if exist "%CONTROL_ROOT%\installer-failure.log" (
    call :reject_reparse_point "%CONTROL_ROOT%\installer-failure.log"
    if errorlevel 1 exit /b 0
)
powershell -NoProfile -ExecutionPolicy Bypass -Command "$line=('{0:o} ERROR ENIMAS installer {1}: {2} (exit {3})' -f [DateTime]::UtcNow,$env:INSTALLER_REVISION,$env:INSTALLER_FAILURE_STEP,$env:INSTALLER_FAILURE_EXIT); try{[IO.File]::AppendAllText((Join-Path $env:CONTROL_ROOT 'installer-failure.log'),$line+[Environment]::NewLine); exit 0}catch{exit 1}" >nul 2>&1
if not errorlevel 1 set "INSTALLER_FAILURE_LOG=%CONTROL_ROOT%\installer-failure.log"
exit /b 0

:elevate_wait
set "ELEVATE_TARGET=%~f0"
if exist "%PROTECTED_INSTALLER%" set "ELEVATE_TARGET=%PROTECTED_INSTALLER%"
echo Waiting for Windows administrator approval. Keep this window open...
call :run_elevated "%ELEVATE_TARGET%" "%~1"
set "ELEVATE_EXIT=!ERRORLEVEL!"
exit /b !ELEVATE_EXIT!

:test_elevation_broker
set "ELEVATION_TEST_MODE=1"
call :run_elevated "%~f0" "--test-elevation-child"
exit /b !ERRORLEVEL!

:test_elevation_broker_no_env
set "ELEVATION_TEST_MODE=1"
set "ELEVATION_TEST_DROP_ENV=1"
call :run_elevated "%~f0" "--test-elevation-child"
exit /b !ERRORLEVEL!

:run_elevated
set "ELEVATE_TARGET=%~1"
set "ELEVATE_OPERATION=%~2"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $pipeName='ENIMAS-'+[Guid]::NewGuid().ToString('N'); $pipeSecurity=[IO.Pipes.PipeSecurity]::new(); $pipeRights=[IO.Pipes.PipeAccessRights]::ReadWrite -bor [IO.Pipes.PipeAccessRights]::CreateNewInstance; $allow=[Security.AccessControl.AccessControlType]::Allow; $callerRule=[IO.Pipes.PipeAccessRule]::new([Security.Principal.WindowsIdentity]::GetCurrent().User,$pipeRights,$allow); $adminRule=[IO.Pipes.PipeAccessRule]::new([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'),$pipeRights,$allow); $null=$pipeSecurity.AddAccessRule($callerRule); $null=$pipeSecurity.AddAccessRule($adminRule); $server=[IO.Pipes.NamedPipeServerStream]::new($pipeName,[IO.Pipes.PipeDirection]::In,1,[IO.Pipes.PipeTransmissionMode]::Byte,[IO.Pipes.PipeOptions]::Asynchronous,4096,4096,$pipeSecurity); $payload=@($pipeName,[string]$env:ELEVATE_TARGET,[string]$env:ELEVATE_OPERATION,[string]$env:ENIMAS_INSTALL_USER_SID,[string]$env:ELEVATION_TEST_DROP_ENV) | ConvertTo-Json -Compress; $payload64=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($payload)); $clientScript='$ErrorActionPreference=''Stop''; $values=ConvertFrom-Json ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String(''<PAYLOAD>''))); $pipeName=[string]$values[0]; $target=[string]$values[1]; $operation=[string]$values[2]; $callerSid=[string]$values[3]; if([string]$values[4] -eq ''1''){Remove-Item Env:ENIMAS_ELEVATION_PIPE,Env:ELEVATE_TARGET,Env:ELEVATE_OPERATION,Env:ENIMAS_INSTALL_USER_SID -ErrorAction SilentlyContinue}; $pipe=[IO.Pipes.NamedPipeClientStream]::new(''.'',$pipeName,[IO.Pipes.PipeDirection]::Out); $pipe.Connect(15000); $writer=[IO.StreamWriter]::new($pipe); $writer.AutoFlush=$true; $writer.WriteLine(''STARTED''); $command=''""{0}" "{1}" "--caller-sid" "{2}""'' -f $target,$operation,$callerSid; $cmd=Join-Path $env:SystemRoot ''System32\cmd.exe''; & $cmd /d /s /c $command; $code=$LASTEXITCODE; $writer.WriteLine([string]$code); $writer.Dispose(); exit $code'; $clientScript=$clientScript.Replace('<PAYLOAD>',$payload64); $encoded=[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($clientScript)); $arguments=@('-NoProfile','-ExecutionPolicy','Bypass','-EncodedCommand',$encoded); $wait=$server.BeginWaitForConnection($null,$null); $launchError=$null; try{if($env:ELEVATION_TEST_MODE -eq '1'){Start-Process -FilePath ($PSHOME+'\powershell.exe') -ArgumentList $arguments -NoNewWindow | Out-Null}else{Start-Process -FilePath ($PSHOME+'\powershell.exe') -ArgumentList $arguments -Verb RunAs | Out-Null}}catch{$launchError=$_}; if(-not $wait.AsyncWaitHandle.WaitOne(15000)){$server.Dispose(); if($launchError){Write-Host ('Could not start administrator maintenance: '+$launchError.Exception.Message) -ForegroundColor Yellow; if($launchError.Exception.NativeErrorCode -eq 1223){exit 1223}}else{Write-Host 'Administrator maintenance did not establish its private completion channel.' -ForegroundColor Yellow}; exit 1}; $server.EndWaitForConnection($wait); $reader=[IO.StreamReader]::new($server); $started=$reader.ReadLineAsync(); if(-not $started.Wait(5000) -or $started.Result -ne 'STARTED'){$reader.Dispose(); exit 1}; $result=$reader.ReadLineAsync(); if(-not $result.Wait(1800000)){$reader.Dispose(); Write-Host 'Administrator maintenance did not finish within 30 minutes.' -ForegroundColor Yellow; exit 1}; $code=0; if(-not [int]::TryParse($result.Result,[ref]$code)){$reader.Dispose(); exit 1}; $reader.Dispose(); exit $code"
exit /b !ERRORLEVEL!

:require_unelevated_user
call :is_admin
if errorlevel 1 exit /b 0
echo For security, application files and health checks must run without administrator rights.
echo Close this window and run install.bat normally.
exit /b 1

:require_admin
call :is_admin
if not errorlevel 1 exit /b 0
echo This internal system-maintenance step requires administrator approval.
exit /b 1

:is_admin
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent()); if($p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){exit 0}else{exit 1}" >nul 2>&1
exit /b !ERRORLEVEL!

:validate_install_user
set "SID_CANDIDATE=%ENIMAS_INSTALL_USER_SID%"
call :is_supported_install_user_sid
if errorlevel 1 goto install_user_invalid
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=(Get-ItemProperty -LiteralPath ('Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\'+$env:ENIMAS_INSTALL_USER_SID) -Name ProfileImagePath -ErrorAction Stop).ProfileImagePath; if(-not (Test-Path -LiteralPath $p)){exit 1}" >nul 2>&1
if errorlevel 1 goto install_user_invalid
exit /b 0

:install_user_invalid
echo Could not identify a safe per-user ENIMAS installation owner.
exit /b 1

:is_supported_install_user_sid
powershell -NoProfile -ExecutionPolicy Bypass -Command "$sid=[string]$env:SID_CANDIDATE; if($sid -notmatch '^(S-1-5-21-\d+-\d+-\d+-\d+|S-1-12-1-\d+-\d+-\d+-\d+)$'){exit 1}; try{$null=[Security.Principal.SecurityIdentifier]::new($sid); exit 0}catch{exit 1}" >nul 2>&1
exit /b !ERRORLEVEL!

:protect_admin_payload_dir
if not exist "%~1" exit /b 0
icacls "%~1" /q /setowner "*S-1-5-32-544" /T /C >nul 2>&1
if errorlevel 1 exit /b 1
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $root=Get-Item -LiteralPath '%~1' -Force; $items=@($root)+@(Get-ChildItem -LiteralPath $root.FullName -Force -Recurse); if($items | Where-Object {$_.Attributes -band [IO.FileAttributes]::ReparsePoint}){throw 'Reparse point in privileged control tree'}; $system=New-Object Security.Principal.SecurityIdentifier('S-1-5-18'); $admins=New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544'); $owner=New-Object Security.Principal.SecurityIdentifier($env:ENIMAS_INSTALL_USER_SID); foreach($item in ($items | Sort-Object {$_.FullName.Length} -Descending)){if($item.PSIsContainer){$acl=New-Object Security.AccessControl.DirectorySecurity; $inherit=[Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'}else{$acl=New-Object Security.AccessControl.FileSecurity; $inherit=[Security.AccessControl.InheritanceFlags]::None}; $acl.SetAccessRuleProtection($true,$false); $acl.SetOwner($admins); $prop=[Security.AccessControl.PropagationFlags]::None; $allow=[Security.AccessControl.AccessControlType]::Allow; $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($system,'FullControl',$inherit,$prop,$allow))); $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($admins,'FullControl',$inherit,$prop,$allow))); $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($owner,'ReadAndExecute',$inherit,$prop,$allow))); Set-Acl -LiteralPath $item.FullName -AclObject $acl}; $allowed=@('S-1-5-18','S-1-5-32-544',$env:ENIMAS_INSTALL_USER_SID); foreach($item in $items){$acl=Get-Acl -LiteralPath $item.FullName; foreach($ace in $acl.Access){$sid=$ace.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value; if($allowed -notcontains $sid){throw ('Unexpected control-plane trustee: '+$sid)}; if($sid -eq $env:ENIMAS_INSTALL_USER_SID -and ($ace.FileSystemRights -band [Security.AccessControl.FileSystemRights]::Write)){throw 'Control-plane owner has write access'}}}" >nul 2>&1
exit /b !ERRORLEVEL!

:protect_install_tree
if not exist "%INSTALL_ROOT%" exit /b 1
icacls "%INSTALL_ROOT%" /q /setowner "*S-1-5-32-544" /T /C >nul 2>&1
if errorlevel 1 exit /b 1
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $root=Get-Item -LiteralPath $env:INSTALL_ROOT -Force; $items=@($root)+@(Get-ChildItem -LiteralPath $root.FullName -Force -Recurse); if($items | Where-Object {$_.Attributes -band [IO.FileAttributes]::ReparsePoint}){throw 'Reparse point in ENIMAS installation tree'}; $system=New-Object Security.Principal.SecurityIdentifier('S-1-5-18'); $admins=New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544'); $owner=New-Object Security.Principal.SecurityIdentifier($env:ENIMAS_INSTALL_USER_SID); foreach($item in ($items | Sort-Object {$_.FullName.Length} -Descending)){if($item.PSIsContainer){$acl=New-Object Security.AccessControl.DirectorySecurity; $inherit=[Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'}else{$acl=New-Object Security.AccessControl.FileSecurity; $inherit=[Security.AccessControl.InheritanceFlags]::None}; $acl.SetAccessRuleProtection($true,$false); $acl.SetOwner($admins); $prop=[Security.AccessControl.PropagationFlags]::None; $allow=[Security.AccessControl.AccessControlType]::Allow; $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($system,'FullControl',$inherit,$prop,$allow))); $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($admins,'FullControl',$inherit,$prop,$allow))); $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($owner,'Modify',$inherit,$prop,$allow))); Set-Acl -LiteralPath $item.FullName -AclObject $acl}" >nul 2>&1
exit /b !ERRORLEVEL!

:reject_reparse_point
powershell -NoProfile -ExecutionPolicy Bypass -Command "$item=Get-Item -LiteralPath '%~1' -Force -ErrorAction Stop; if($item.Attributes -band [IO.FileAttributes]::ReparsePoint){exit 1}" >nul 2>&1
exit /b !ERRORLEVEL!

:verify_install_acl
powershell -NoProfile -ExecutionPolicy Bypass -Command "$bad=(Get-Acl -LiteralPath $env:INSTALL_ROOT).Access | Where-Object {$_.AccessControlType -eq 'Allow' -and @('S-1-2-0','S-1-5-32-545') -contains $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value -and ($_.FileSystemRights -band [System.Security.AccessControl.FileSystemRights]::Modify)}; if($bad){exit 1}" >nul 2>&1
exit /b !ERRORLEVEL!
