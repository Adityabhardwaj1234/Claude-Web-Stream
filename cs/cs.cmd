@echo off
rem Claude Stream short command for Windows. Runs from the repository root.
rem Add the app folder to PATH once: setx PATH "%PATH%;C:\path\to\repo\app"
setlocal
pushd "%~dp0.."
python -m app.server %*
set "EXITCODE=%ERRORLEVEL%"
popd
exit /b %EXITCODE%
