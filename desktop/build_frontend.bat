@echo off
setlocal EnableExtensions EnableDelayedExpansion
echo === Build Novel Analyzer Frontend ===
echo === Local disk build (avoids esbuild share limitation), syncing dist back ===
echo.

pushd "%~dp0\.."
set "SHARE_ROOT=%CD%"

set "RUN_LOG=%LOCALAPPDATA%\NovelAnalyzer\build.log"
if not exist "%LOCALAPPDATA%\NovelAnalyzer" mkdir "%LOCALAPPDATA%\NovelAnalyzer"
echo [%date% %time%] === build_frontend start === > "%RUN_LOG%"

rem ---- Locate npm ----
set "NPM_CMD=npm"
where npm >nul 2>nul
if errorlevel 1 (
  for /f "delims=" %%F in ('dir /b /s /a-d "%LOCALAPPDATA%\Microsoft\WinGet\Packages\npm.cmd" 2^>nul') do (
    if not defined FOUND_NPM ( set "NPM_CMD=%%F" & set "FOUND_NPM=1" )
  )
)
echo [%date% %time%] Using npm: %NPM_CMD% >> "%RUN_LOG%"

rem ---- 1) Copy frontend source to local disk ----
set "LOCAL_BUILD=%LOCALAPPDATA%\NovelAnalyzer\build"
if not exist "%LOCAL_BUILD%" mkdir "%LOCAL_BUILD%"
robocopy frontend "%LOCAL_BUILD%" /E /PURGE /XD node_modules dist .git .bak_* .tmp_verify /R:2 /W:2 /NFL /NDL >> "%RUN_LOG%"
if errorlevel 8 (
  echo [%date% %time%] Failed to sync frontend source >> "%RUN_LOG%"
  echo [ERROR] Failed to sync frontend source.
  popd
  pause
  exit /b 1
)

rem ---- 2) Install dependencies locally ----
echo Installing/updating frontend dependencies...
pushd "%LOCAL_BUILD%"
call %NPM_CMD% install >> "%RUN_LOG%" 2>&1
set NPM_ERR=!ERRORLEVEL!
popd
if not "!NPM_ERR!"=="0" (
  echo [%date% %time%] npm install failed >> "%RUN_LOG%"
  echo [ERROR] npm install failed. See "%RUN_LOG%".
  popd
  pause
  exit /b 1
)

rem ---- 3) Build locally ----
echo Building frontend on local disk...
pushd "%LOCAL_BUILD%"
call %NPM_CMD% run build >> "%RUN_LOG%" 2>&1
set BUILD_ERR=%ERRORLEVEL%
popd
if not "%BUILD_ERR%"=="0" (
  echo [%date% %time%] Frontend build failed >> "%RUN_LOG%"
  echo [ERROR] Frontend build failed. See "%RUN_LOG%".
  popd
  pause
  exit /b 1
)

rem ---- 4) Sync dist back to repo ----
robocopy "%LOCAL_BUILD%\dist" "frontend\dist" /E /PURGE /R:2 /W:2 /NFL /NDL >> "%RUN_LOG%"
if errorlevel 8 (
  echo [%date% %time%] Failed to sync dist back >> "%RUN_LOG%"
  echo [ERROR] Failed to sync frontend\dist. See "%RUN_LOG%".
  popd
  pause
  exit /b 1
)
echo [%date% %time%] === dist synced back to frontend/dist === >> "%RUN_LOG%"

echo.
echo Frontend build complete. You can now launch run_desktop.bat from the repo root.
popd
pause
