@echo off
rem ==========================================================================
rem  photo-browser launcher (Windows)
rem
rem  WHY .cmd and NOT .ps1
rem  -------------------
rem    PowerShell 5.1 refuses to run .ps1 files when the machine execution
rem    policy is Restricted / AllSigned / RemoteSigned-without-trust. The
rem    symptom is a SecurityError / UnauthorizedAccess saying scripts are
rem    forbidden on this system (the wording is localized, hence no quote).
rem
rem    That policy is a machine/user security setting -- we must NOT weaken it
rem    from inside a project script (Set-ExecutionPolicy in a README would be
rem    telling the user to lower their machine's security posture for our
rem    convenience). A .cmd file is executed by cmd.exe and is NOT subject to
rem    the PowerShell execution policy, so it runs on a locked-down machine,
rem    from PowerShell, from cmd.exe, and by double-click.
rem
rem  ARGUMENTS
rem    Passed straight through to code/src/tools/serve.py, so the flag
rem    vocabulary is the Python one (--dev / --check-only / --port / --db).
rem    One vocabulary, not two: an earlier .ps1 used -Dev while the Python used
rem    --dev, and the mismatch was a trap waiting to happen.
rem      start.cmd
rem      start.cmd --dev
rem      start.cmd --check-only
rem      start.cmd --port 8799 --db d:\PhotoLib\db\step11-verify.db
rem
rem  ENCODING
rem    This file is deliberately ASCII-only. cmd.exe reads .cmd in the OEM
rem    code page (936 here), so any Chinese in this file would be mojibake.
rem    All human-facing messages live in serve.py (UTF-8 by definition).
rem    test_serve_launcher.py asserts the ASCII invariant -- keep it that way.
rem ==========================================================================

setlocal

rem ---- always run from the repo root, no matter where we were invoked ----
rem %~dp0 ends with a backslash; without -d a UNC path would silently
rem switch to C:\Windows.
cd /d "%~dp0"

rem ---- locate the venv interpreter ----
rem Do NOT fall back to bare `python`: on Windows that resolves to the
rem WindowsApps base interpreter whose site-packages live under the user
rem profile and differ from the venv. It "works" and then behaves strangely.
set "PY=%~dp0code\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo.
    echo   [BLOCKED] venv not found: %PY%
    echo.
    echo   Create it once ^(the official PyPI source is mandatory on this
    echo   machine -- the default mirror cannot find "vobject"^):
    echo.
    echo     py -3.13 -m venv code\.venv
    echo     code\.venv\Scripts\python.exe -m pip install -i https://pypi.org/simple -r requirements.txt
    echo     code\.venv\Scripts\python.exe -m pip uninstall -y opencv-python
    echo.
    pause
    exit /b 1
)

set "SERVE=%~dp0code\src\tools\serve.py"
if not exist "%SERVE%" (
    echo   [BLOCKED] launcher not found: %SERVE%
    pause
    exit /b 1
)

"%PY%" "%SERVE%" %*
set "RC=%ERRORLEVEL%"

rem ---- keep the window open when startup FAILED ----
rem Double-clicking start.cmd with a broken config would otherwise flash the
rem error message and close, and the user would see literally nothing.
rem On success we do NOT pause: the server is holding the window, and Ctrl+C
rem (or closing it) is the intended way to stop.
if not "%RC%"=="0" pause

exit /b %RC%