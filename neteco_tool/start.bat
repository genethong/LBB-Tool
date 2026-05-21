@echo off
setlocal enabledelayedexpansion

set "DIR=%~dp0"
set "PY_DIR=%DIR%python-embed"
set "PY_EXE=%PY_DIR%\python.exe"
set "PKGS=%PY_DIR%\Lib\site-packages"

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

    REM Create site-packages folder
    mkdir "%PKGS%" 2>nul

    REM Edit the ._pth file: enable site + add Lib\site-packages to path
    for %%f in ("%PY_DIR%\python*.pth") do (
        powershell -Command "(Get-Content '%%f' -Raw) -replace '#import site','Lib\site-packages\nimport site' | Set-Content '%%f'"
    )

    REM Unblock downloaded files so Windows security doesn't block them
    powershell -Command "Get-ChildItem '%PY_DIR%' -Recurse | ForEach-Object { try { Unblock-File $_.FullName } catch {} }"

    REM Download pip.pyz (self-contained pip, no installation needed)
    echo     Downloading pip...
    powershell -Command "Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/pip/pip.pyz' -OutFile '%DIR%pip.pyz' -UseBasicParsing"

    echo     Python ready.
    echo.
)

REM Always set PYTHONPATH so embedded Python finds packages
set "PYTHONPATH=%PKGS%"

REM ── Step 2: Install requirements if flask not present ──
"%PY_EXE%" -c "import flask" 2>nul
if errorlevel 1 (
    echo [2/3] Installing dependencies (one-time^)...
    "%PY_EXE%" "%DIR%pip.pyz" install -r "%DIR%requirements-windows.txt" --target="%PKGS%" --no-warn-script-location -q
    if errorlevel 1 (
        echo.
        echo ERROR: Failed to install dependencies.
        echo Your company security policy may be blocking this.
        echo Please contact your IT team or try from a personal laptop.
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
