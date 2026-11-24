@echo off
setlocal EnableExtensions
setlocal EnableDelayedExpansion
echo === Novel Analyzer Desktop Mode ===
echo.

rem Enter project root (supports Chinese paths and network drives)
pushd "%~dp0"
set "SHARE_ROOT=!CD!"

rem ---- Diagnostic log: all step outputs go to disk for post-mortem ----
set "RUN_LOG=%LOCALAPPDATA%\NovelAnalyzer\run.log"
if not exist "%LOCALAPPDATA%\NovelAnalyzer" mkdir "%LOCALAPPDATA%\NovelAnalyzer"
echo [%date% %time%] === run_desktop start (root=%SHARE_ROOT%) === > "%RUN_LOG%"

rem ---- Locate pythonw: prefer project .venv, fall back to PATH ----
if exist "%SHARE_ROOT%\.venv\Scripts\pythonw.exe" (
  set "PYTHON_W=%SHARE_ROOT%\.venv\Scripts\pythonw.exe"
) else (
  for /f "delims=" %%P in ('where pythonw 2^>nul') do set "PYTHON_W=%%P"
  if not defined PYTHON_W (
    for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do (
      for %%D in ("%%~dpP") do set "PYTHON_W=%%~dpDpythonw.exe"
    )
  )
)
echo [%date% %time%] Using pythonw: %PYTHON_W% >> "%RUN_LOG%"
if not defined PYTHON_W (
  echo [%date% %time%] ERROR: pythonw not found >> "%RUN_LOG%"
  echo [ERROR] pythonw not found. Install Python 3.11+ and add it to PATH.
  popd
  pause
  exit /b 1
)

rem ---- Frontend dist must exist (built by desktop\build_frontend.bat) ----
if not exist "frontend\dist\index.html" (
  echo [%date% %time%] ERROR: frontend/dist missing >> "%RUN_LOG%"
  echo [ERROR] frontend/dist missing. Run desktop\build_frontend.bat first.
  popd
  pause
  exit /b 1
)
echo [%date% %time%] frontend/dist found >> "%RUN_LOG%"

rem ---- Assemble local runtime copy (WebView2 needs process path on local drive) ----
set "LOCAL_APP=%LOCALAPPDATA%\NovelAnalyzer\app"
if not exist "%LOCAL_APP%" mkdir "%LOCAL_APP%"
if not exist "%LOCAL_APP%\desktop" mkdir "%LOCAL_APP%\desktop"
echo [%date% %time%] Assembling local runtime at %LOCAL_APP% >> "%RUN_LOG%"
robocopy backend "%LOCAL_APP%\backend" /E /PURGE /XD __pycache__ /R:2 /W:2 /NFL /NDL >> "%RUN_LOG%"
if exist "%LOCAL_APP%\desktop.py" del /F /Q "%LOCAL_APP%\desktop.py" >> "%RUN_LOG%"
if exist "%LOCAL_APP%\config.json" del /F /Q "%LOCAL_APP%\config.json" >> "%RUN_LOG%"
copy /Y desktop\desktop.py "%LOCAL_APP%\desktop\desktop.py" >> "%RUN_LOG%"
if errorlevel 1 (
  echo [%date% %time%] ERROR: failed to copy desktop\desktop.py >> "%RUN_LOG%"
  echo [ERROR] Failed to copy desktop\desktop.py to local runtime.
  popd
  pause
  exit /b 1
)

rem ---- Load .NET and WebView2 assemblies from the local runtime ----
for %%P in ("!PYTHON_W!") do set "PYTHON_EXE=%%~dpPpython.exe"
set "NATIVE_MANIFEST=!LOCAL_APP!\native-!RANDOM!-!RANDOM!.txt"
"!PYTHON_EXE!" -X utf8 -c "import importlib.metadata as m; pairs=[('pywebview','webview'),('pythonnet','pythonnet'),('clr-loader','clr_loader')]; packages=[(name,m.distribution(dist)) for dist,name in pairs]; print('\n'.join(name+'|'+dist.version+'|'+str(dist.locate_file(name)) for name,dist in packages))" > "!NATIVE_MANIFEST!" 2>> "!RUN_LOG!"
if errorlevel 1 goto :native_error
set "NATIVE_PATH="
set /a NATIVE_COUNT=0
for /f "usebackq tokens=1,2,* delims=|" %%A in ("!NATIVE_MANIFEST!") do (
  set "NATIVE_PACKAGE=!LOCAL_APP!\native\%%A-%%B"
  robocopy "%%C" "!NATIVE_PACKAGE!\%%A" /E /XD __pycache__ /R:1 /W:1 /NFL /NDL >> "!RUN_LOG%"
  if errorlevel 8 goto :native_error
  if defined NATIVE_PATH (set "NATIVE_PATH=!NATIVE_PATH!;!NATIVE_PACKAGE!") else set "NATIVE_PATH=!NATIVE_PACKAGE!"
  set /a NATIVE_COUNT+=1
)
if not "!NATIVE_COUNT!"=="3" goto :native_error
if exist "!NATIVE_MANIFEST!" del "!NATIVE_MANIFEST!"
if defined PYTHONPATH (set "PYTHONPATH=!NATIVE_PATH!;!PYTHONPATH!") else set "PYTHONPATH=!NATIVE_PATH!"
echo [%date% %time%] Local desktop dependencies ready >> "%RUN_LOG%"

rem ---- Launch from local copy; runtime data and dist resolve back via env vars ----
set "NOVEL_ROOT=%SHARE_ROOT%"
set "NOVEL_RUNTIME_DIR=%SHARE_ROOT%\runtime"
echo [%date% %time%] === launching: %PYTHON_W% "%LOCAL_APP%\desktop\desktop.py" (NOVEL_ROOT=%SHARE_ROOT%, NOVEL_RUNTIME_DIR=%NOVEL_RUNTIME_DIR%) === >> "%RUN_LOG%"

rem ---- Silent launch: pythonw runs detached, no console window ----
if not exist "%RUN_LOG%" type nul > "%RUN_LOG%"
start "" "%PYTHON_W%" "%LOCAL_APP%\desktop\desktop.py"

popd
exit /b 0

:native_error
if exist "!NATIVE_MANIFEST!" del "!NATIVE_MANIFEST!"
echo [%date% %time%] ERROR: Could not prepare local desktop dependencies >> "%RUN_LOG%"
echo [ERROR] Could not prepare local desktop dependencies. See "%RUN_LOG%".
popd
pause
exit /b 1
