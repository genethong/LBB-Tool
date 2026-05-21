@echo off
setlocal enabledelayedexpansion

set "DIR=%~dp0"
set "PY_DIR=%DIR%python-embed"
set "PY_EXE=%PY_DIR%\python.exe"
set "PIP_EXE=%PY_DIR%\Scripts\pip.exe"

echo.
echo ================================================
echo   NetEco Tool ^| Windows Launcher
echo ================================================
echo.

REM ── Step 1: Download portable Python if not present ──
if not exist "%PY_EXE%" (
    echo [1/3] First-time setup: downloading portable Python...
    powershell -Command "Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip' -OutFile '%DIR%py_embed.zip' -UseBasicParsing"
    if errorlevel 1 (
        echo ERROR: Could not download Python. Check your internet connection.
        pause & exit /b 1
    )
    powershell -Command "Expand-Archive -Path '%DIR%py_embed.zip' -DestinationPath '%PY_DIR%' -Force"
    del "%DIR%py_embed.zip"

    REM Enable pip support (uncomment 'import site' in the ._pth file)
    powershell -Command "Get-ChildItem '%PY_DIR%' -Filter '*.pth' | ForEach-Object { (Get-Content $_.FullName) -replace '#import site','import site' | Set-Content $_.FullName }"

    REM Bootstrap pip
    echo     Installing pip...
    powershell -Command "Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile '%DIR%get-pip.py' -UseBasicParsing"
    "%PY_EXE%" "%DIR%get-pip.py" --no-warn-script-location -q
    del "%DIR%get-pip.py"
    echo     Python ready.
    echo.
)

REM ── Step 2: Install requirements if flask not present ──
"%PY_EXE%" -c "import flask" 2>nul
if errorlevel 1 (
    echo [2/3] Installing dependencies (one-time^)...
    "%PIP_EXE%" install -r "%DIR%requirements.txt" --target="%PY_DIR%\Lib\site-packages" --no-warn-script-location -q
    if errorlevel 1 (
        echo ERROR: Failed to install dependencies.
        echo Try right-clicking start.bat and selecting "Run as administrator".
        pause & exit /b 1
    )
    echo     Dependencies ready.
    echo.
)

REM ── Step 3: Check config exists ──
if not exist "%DIR%instance\config.json" (
    echo WARNING: instance\config.json not found.
    echo.
    echo Please create the file:
    echo   %DIR%instance\config.json
    echo.
    echo With this content ^(fill in your password^):
    echo   {
    echo     "neteco_url": "https://190.92.203.75:32102",
    echo     "username": "Webservice_NBI_User",
    echo     "password": "YOUR_PASSWORD_HERE"
    echo   }
    echo.
    pause & exit /b 1
)

REM ── Step 4: Launch ──
echo [3/3] Starting NetEco Tool...
echo.
echo   Browser: http://localhost:8080
echo   Login:   admin / admin123
echo.
echo   Press Ctrl+C to stop.
echo ================================================
echo.

cd /d "%DIR%"
"%PY_EXE%" app.py

pause
