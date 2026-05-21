#!/bin/bash
# ─────────────────────────────────────────────────────
# NetEco Tool — Local startup script (staging/dev)
# ─────────────────────────────────────────────────────
cd "$(dirname "$0")"

# Install dependencies if not present
if ! python3 -c "import flask" 2>/dev/null; then
  echo "Installing dependencies..."
  pip3 install -r requirements.txt --break-system-packages -q
fi

echo ""
echo "================================================"
echo "  NetEco Tool (Development Mode)"
echo "  Open: http://localhost:8080"
echo "================================================"
echo ""

DEBUG=true python3 app.py
