@echo off
REM Refresh all Jira platform webhook subscriptions for this app.
REM Invoked by Windows Task Scheduler on a ~25-day cadence so the 30-day
REM expiry never catches up. Logs to ..\logs\refresh.log (rolling append).

setlocal
set "PROJECT_DIR=%~dp0.."
pushd "%PROJECT_DIR%" || exit /b 1

set "STAMP=%DATE% %TIME%"
echo [%STAMP%] starting refresh >> "logs\refresh.log"

".venv\Scripts\python.exe" -m app.admin.register_webhook refresh >> "logs\refresh.log" 2>&1
set "RC=%ERRORLEVEL%"

set "STAMP=%DATE% %TIME%"
echo [%STAMP%] finished refresh rc=%RC% >> "logs\refresh.log"
echo. >> "logs\refresh.log"

popd
endlocal & exit /b %RC%
