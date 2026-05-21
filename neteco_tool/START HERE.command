#!/bin/bash
# ── NetEco Tool Launcher ──────────────────────────────
# Double-click this file to start the tool.

cd "/Users/jinthongteoh/Documents/Claude/Projects/LBB Tools/neteco_tool"

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║        NetEco Tool — LBB Group Eng       ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── Check Python 3 ───────────────────────────────────
if ! command -v python3 &>/dev/null; then
  echo "❌  Python 3 not found."
  echo ""
  echo "  Please install Python 3 from: https://www.python.org/downloads/"
  echo "  Then double-click this file again."
  echo ""
  read -p "Press Enter to close..."
  exit 1
fi

echo "✅  Python 3 found: $(python3 --version)"

# ── Install dependencies (first run only) ────────────
if ! python3 -c "import flask" 2>/dev/null; then
  echo ""
  echo "📦  Installing required packages (first run only — takes ~30 seconds)..."
  pip3 install flask requests pandas openpyxl gunicorn urllib3 --break-system-packages -q
  if [ $? -ne 0 ]; then
    pip3 install flask requests pandas openpyxl gunicorn urllib3 -q
  fi
  echo "✅  Packages installed."
fi

# ── Create instance folder ────────────────────────────
mkdir -p instance

# ── Start Flask in background, log to file ────────────
LOG="/tmp/neteco_tool.log"
echo "" > "$LOG"
echo "🚀  Starting NetEco Tool..."
python3 app.py > "$LOG" 2>&1 &
FLASK_PID=$!

# ── Wait until Flask is ready (up to 30s) ────────────
echo "    Waiting for server to start..."
READY=0
for i in $(seq 1 30); do
  sleep 1
  if curl -s http://localhost:8080/ > /dev/null 2>&1; then
    READY=1
    break
  fi
  # Check if Python crashed
  if ! kill -0 $FLASK_PID 2>/dev/null; then
    break
  fi
  echo "    ... ($i)"
done

if [ $READY -eq 1 ]; then
  echo ""
  echo "✅  NetEco Tool is running on http://localhost:8080"
  echo "    Opening browser..."
  echo ""
  echo "    To STOP the tool, press Ctrl+C in this window"
  echo "    or close this Terminal window."
  echo ""
  echo "────────────────────────────────────────────────"
  open "http://localhost:8080"
  wait $FLASK_PID
else
  echo ""
  echo "❌  Server failed to start. Error details:"
  echo "────────────────────────────────────────────────"
  cat "$LOG"
  echo "────────────────────────────────────────────────"
  echo ""
  read -p "Press Enter to close..."
fi
