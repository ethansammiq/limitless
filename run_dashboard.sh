#!/usr/bin/env bash
# Launch the Weather Edge live dashboard (localhost only).
# Usage: ./run_dashboard.sh   then open http://127.0.0.1:8787
set -euo pipefail
cd "$(dirname "$0")"
exec .venv/bin/python3 dashboard_server.py
