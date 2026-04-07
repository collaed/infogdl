@echo off
setlocal

where python >nul 2>&1
if %errorlevel% neq 0 (
    echo Python not found. Please install Python 3.10+ from https://www.python.org/downloads/
    exit /b 1
)

python -c "import sys; exit(0 if sys.version_info >= (3, 10) else 1)" 2>nul
if %errorlevel% neq 0 (
    echo Python 3.10+ required.
    python --version
    exit /b 1
)

echo Installing Python dependencies...
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo Failed to install Python packages.
    exit /b 1
)

where chromedriver >nul 2>&1
if %errorlevel% neq 0 (
    echo WARNING: chromedriver not found in PATH. Scraping mode requires it.
    echo Download from https://googlechromelabs.github.io/chrome-for-testing/
    echo Local mode (-i / -o) will work without it.
)

echo.
echo Setup complete. Usage:
echo   python infogdl.py -i INPUT_DIR -o OUTPUT_DIR
echo   python infogdl.py                              (scrape from config.json)
