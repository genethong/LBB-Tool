#!/bin/bash
# ─────────────────────────────────────────────────────
# NetEco Tool — Production startup (GCP VM)
# Runs on port 8080 with gunicorn (2 workers, fits 1GB RAM)
# ─────────────────────────────────────────────────────
cd "$(dirname "$0")"

if ! python3 -c "import gunicorn" 2>/dev/null; then
  echo "Installing dependencies..."
  pip3 install -r requirements.txt --break-system-packages -q
fi

echo ""
echo "================================================"
echo "  NetEco Tool (Production Mode)"
echo "  Running on port 8080"
echo "================================================"
echo ""

export DEBUG=false
export SECRET_KEY="${SECRET_KEY:-$(python3 -c 'import secrets; print(secrets.token_hex(32))')}"

exec gunicorn app:app \
  --bind 0.0.0.0:8080 \
  --workers 2 \
  --threads 2 \
  --timeout 120 \
  --access-logfile - \
  --error-logfile -
