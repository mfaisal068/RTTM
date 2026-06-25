@echo off
echo.
echo ==========================================
echo   RTTM Engine - INTECH Process Auto.
echo ==========================================
echo.

echo [1/3] Installing dependencies...
python -m pip install -r requirements.txt --quiet
if %errorlevel% neq 0 (
    echo ERROR: pip install failed. Check Python version.
    pause & exit /b 1
)

echo [2/3] Dependencies installed.
echo [3/3] Starting RTTM API on http://localhost:8000
echo.
echo   Dashboard : open dashboard.html in your browser
echo   API docs  : http://localhost:8000/docs
echo   Health    : http://localhost:8000/health
echo.
echo Press Ctrl+C to stop.
echo.

python -m uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload
pause
