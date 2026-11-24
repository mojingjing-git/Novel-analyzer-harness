@echo off
setlocal

echo === Novel Analyzer Dev Mode ===
echo.

pushd "%~dp0"
set "PROJECT_ROOT=%CD%"
echo Project root: %PROJECT_ROOT%
echo.

echo Starting backend on port 8000...
start "backend" cmd /k "cd /d "%PROJECT_ROOT%" && python -m uvicorn backend.app:app --reload --port 8000"

echo Starting frontend on port 5173...
start "frontend" cmd /k "cd /d "%PROJECT_ROOT%\frontend" && npm run dev"

echo.
echo Backend:  http://localhost:8000
echo Frontend: http://localhost:5173
echo API docs: http://localhost:8000/docs
popd

pause
