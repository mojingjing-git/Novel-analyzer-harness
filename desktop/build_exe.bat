@echo off
rem One-click Windows single-file exe build via PyInstaller
rem Output: dist\novel-analyzer.exe

setlocal
pushd "%~dp0\.."

echo ===============================================
echo Novel Analyzer - desktop\build_exe.bat
echo ===============================================

if exist build (
    echo [1/4] Cleaning build/ ...
    rmdir /s /q build
)
if exist dist (
    echo [1/4] Cleaning dist/ ...
    rmdir /s /q dist
)

echo [2/4] Building frontend ...
call npm --prefix frontend run build
if errorlevel 1 (
    echo [ERROR] Frontend build failed
    popd
    exit /b 1
)

echo [3/4] Running PyInstaller ...
call .venv\Scripts\python.exe -m PyInstaller --noconfirm --clean desktop\desktop.spec
if errorlevel 1 (
    echo [ERROR] PyInstaller failed
    popd
    exit /b 1
)

echo [4/4] Verifying artifact ...
if exist dist\novel-analyzer.exe (
    for %%A in (dist\novel-analyzer.exe) do echo   novel-analyzer.exe = %%~zA bytes
    echo.
    echo ===============================================
    echo [OK] Build complete: dist\novel-analyzer.exe
    echo ===============================================
) else (
    echo [ERROR] Artifact dist\novel-analyzer.exe not found
    popd
    exit /b 1
)

popd
endlocal
