#!/usr/bin/env bash
set -e

if ! command -v python3 &>/dev/null; then
    echo "Python not found. Install Python 3.10+."
    exit 1
fi

if ! python3 -c "import sys; exit(0 if sys.version_info >= (3, 10) else 1)" 2>/dev/null; then
    echo "Python 3.10+ required."
    python3 --version
    exit 1
fi

echo "Installing Python dependencies..."
pip install -r requirements.txt

if ! command -v chromedriver &>/dev/null; then
    echo "WARNING: chromedriver not found. Scraping mode requires it."
    echo "Download from https://googlechromelabs.github.io/chrome-for-testing/"
    echo "Local mode (-i / -o) will work without it."
fi

echo ""
echo "Setup complete. Usage:"
echo "  python3 infogdl.py -i INPUT_DIR -o OUTPUT_DIR"
echo "  python3 infogdl.py                              (scrape from config.json)"
