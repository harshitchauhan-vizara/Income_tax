@echo off
title TAX Holobox - Project Launcher
color 0A

echo ========================================
echo      TAX Holobox Project Launcher
echo ========================================
echo.

:: Set paths relative to this batch file's location
set "ROOT=%~dp0"
set "BACKEND=%ROOT%new_tax\backend"
set "FRONTEND=%ROOT%new_tax\frontend"

:: Check Node.js
echo [1/4] Checking Node.js...
node --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Node.js not found. Please install Node.js and try again.
    pause
    exit /b 1
)
echo       Node.js found.

:: Check virtual environment
echo [2/4] Checking Python virtual environment...
if not exist "%BACKEND%\venv\Scripts\activate.bat" (
    echo.
    echo [ERROR] Virtual environment not found!
    echo  Create it with Python 3.11:
    echo  python3.11 -m venv "%BACKEND%\venv"
    echo.
    pause
    exit /b 1
)
echo       Virtual environment found.

:: Install backend dependencies if needed
echo [3/4] Checking backend dependencies...
if exist "%BACKEND%\.deps_installed" goto :deps_done
    echo       Installing backend packages (first time only)...
    call "%BACKEND%\venv\Scripts\activate.bat"
    pip install -r "%BACKEND%\requirements.txt"
    echo installed > "%BACKEND%\.deps_installed"
    echo       Installation complete.
:deps_done
echo       Backend dependencies ready.

:: Install frontend dependencies if needed
echo [4/4] Checking frontend dependencies...
if exist "%FRONTEND%\node_modules" goto :npm_done
    echo       Installing frontend packages (first time only)...
    pushd "%FRONTEND%"
    npm install
    popd
:npm_done
echo       Frontend dependencies ready.

echo.
echo ========================================
echo  Starting servers in separate windows...
echo ========================================
echo.

:: Start Backend using venv
echo  ^>^> Backend  : http://localhost:8111
start "TAX Backend (FastAPI)" cmd /k "cd /d "%BACKEND%" && call venv\Scripts\activate.bat && python -m uvicorn app.main:app --reload --port 8111"

:: Small delay so backend starts first
timeout /t 3 /nobreak >nul

:: Start Frontend
echo  ^>^> Frontend : http://localhost:3123
start "TAX Frontend (Vite)" cmd /k "cd /d "%FRONTEND%" && npm run dev"

:: Wait for Vite to be ready then open browser
echo.
echo  Waiting for frontend to start...
timeout /t 4 /nobreak >nul
start "" "http://localhost:3123"

echo.
echo  Browser opened at http://localhost:3123
echo  Close the server windows (or press Ctrl+C in each) to stop.
echo.
pause