#!/bin/bash
# =============================================================================
# WEATHER EDGE — Push code to remote server via rsync
#
# Usage:
#   ./deploy/deploy.sh <server-ip>              # Code only (fast)
#   ./deploy/deploy.sh <server-ip> --full       # Code + secrets + setup
#   ./deploy/deploy.sh <server-ip> --secrets     # Upload .env + key only
#
# Requirements:
#   - SSH key-based auth to <server-ip> (REMOTE_USER=ubuntu default, or root)
#   - Server already provisioned with setup_oracle.sh (or setup manually)
# =============================================================================
set -euo pipefail

# ─── Config ─────────────────────────────────
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"  # Project root
# REMOTE_USER: Oracle/Lightsail log in as 'ubuntu'; Hetzner/DO/Vultr/Linode as
# 'root'. Override with REMOTE_USER=root ./deploy/deploy.sh <ip> --full
REMOTE_USER="${REMOTE_USER:-ubuntu}"
if [ "$REMOTE_USER" = "root" ]; then
    REMOTE_DIR="/root/limitless"
else
    REMOTE_DIR="/home/$REMOTE_USER/limitless"
fi

# SSH key: honor $SSH_KEY, else id_rsa, else the first ed25519 key present
# (this Mac has only ed25519 keys, so the old id_rsa default always missed).
if [ -n "${SSH_KEY:-}" ]; then
    :
elif [ -f "$HOME/.ssh/id_rsa" ]; then
    SSH_KEY="$HOME/.ssh/id_rsa"
else
    SSH_KEY="$(ls "$HOME"/.ssh/id_ed25519* 2>/dev/null | grep -v '\.pub$' | head -1)"
fi
if [ -z "${SSH_KEY:-}" ] || [ ! -f "$SSH_KEY" ]; then
    echo "No SSH private key found. Set SSH_KEY=/path/to/key and retry." >&2
    exit 1
fi
echo "Using SSH key: $SSH_KEY"

# ─── Args ───────────────────────────────────
if [ $# -lt 1 ]; then
    echo "Usage: $0 <server-ip> [--full|--secrets|--state]"
    echo ""
    echo "  <server-ip>    SSH to this host"
    echo "  --full         Upload code + secrets + state + run setup"
    echo "  --secrets      Upload .env + private key only"
    echo "  --state        Upload live state files (ONE-TIME migration —"
    echo "                 overwrites the server's ledger; never run this"
    echo "                 after the server's cron jobs have gone live)"
    echo ""
    echo "  Set SSH_KEY env var to use a custom SSH key"
    exit 1
fi

# Live state — the position ledger and friends. Code deploys must NEVER
# touch these on a running server (a stale Mac copy would clobber the live
# ledger); they move only via an explicit --state migration.
STATE_FILES=(
    positions_paper.json paper_balance.json paper_orders.json
    heartbeats.json peak_state.json stale_price_state.json
    alert_state.json dead_bracket_state.json watchdog_catchup.json
    live_watch_state.json cli_sniper_state.json
    price_history.json temp_history.json dashboard_day_anchor.json
    weather_edge.db model_bias_corrections.json
)

SERVER="$1"
MODE="${2:-code}"

SSH_CMD="ssh -i $SSH_KEY -o StrictHostKeyChecking=no"
SCP_CMD="scp -i $SSH_KEY -o StrictHostKeyChecking=no"
RSYNC_SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=no"

echo "═══════════════════════════════════════════"
echo "  Weather Edge — Deploy to $SERVER"
echo "  Mode: $MODE"
echo "═══════════════════════════════════════════"

# ─── Upload secrets ─────────────────────────
upload_secrets() {
    echo ""
    echo "[secrets] Uploading .env and private key..."
    $SSH_CMD "$REMOTE_USER@$SERVER" "mkdir -p $REMOTE_DIR"

    if [ -f "$LOCAL_DIR/.env" ]; then
        $SCP_CMD "$LOCAL_DIR/.env" "$REMOTE_USER@$SERVER:$REMOTE_DIR/.env"
        # The Mac .env carries a Mac-absolute key path; rewrite it for the
        # server or every authenticated call silently 401s (live_watch then
        # journals balance $0.00 and position_monitor preflight-fails).
        $SSH_CMD "$REMOTE_USER@$SERVER" "sed -i 's|^KALSHI_PRIVATE_KEY_PATH=.*|KALSHI_PRIVATE_KEY_PATH=$REMOTE_DIR/kalshi_private_key.pem|' $REMOTE_DIR/.env"
        echo "  ✅ .env uploaded (key path rewritten for $REMOTE_DIR)"
    else
        echo "  ⚠ .env not found at $LOCAL_DIR/.env"
    fi

    if [ -f "$LOCAL_DIR/kalshi_private_key.pem" ]; then
        $SCP_CMD "$LOCAL_DIR/kalshi_private_key.pem" "$REMOTE_USER@$SERVER:$REMOTE_DIR/kalshi_private_key.pem"
        $SSH_CMD "$REMOTE_USER@$SERVER" "chmod 600 $REMOTE_DIR/kalshi_private_key.pem"
        echo "  ✅ Private key uploaded (chmod 600)"
    else
        echo "  ⚠ kalshi_private_key.pem not found"
    fi
}

# ─── Upload code ────────────────────────────
upload_code() {
    echo ""
    echo "[code] Syncing code to $SERVER..."

    rsync -avz --progress \
        --exclude '.git' \
        --exclude '.venv' \
        --exclude '__pycache__' \
        --exclude '*.pyc' \
        --exclude '.env' \
        --exclude 'kalshi_private_key.pem' \
        --exclude 'positions.json' \
        --exclude '.positions.lock' \
        --exclude '.positions_paper.lock' \
        --exclude '.heartbeats.lock' \
        --exclude 'alerts_fallback.jsonl' \
        --exclude 'PAUSE_TRADING' \
        --exclude 'heartbeats/' \
        --exclude 'backtest/*.json' \
        --exclude 'backtest/*.jsonl*' \
        --exclude 'scan_logs/' \
        --exclude 'logs/' \
        --exclude '.pytest_cache' \
        --exclude '.ruff_cache' \
        --exclude '.claude/' \
        "${STATE_FILES[@]/#/--exclude=}" \
        -e "$RSYNC_SSH" \
        "$LOCAL_DIR/" "$REMOTE_USER@$SERVER:$REMOTE_DIR/"

    echo "  ✅ Code synced (state files excluded)"
}

# ─── Upload live state (one-time migration) ─
upload_state() {
    echo ""
    echo "[state] Migrating live state to $SERVER..."
    echo "  ⚠ This OVERWRITES the server's ledger. Only do this before the"
    echo "    server's cron jobs go live, with the Mac's crontab disabled."
    $SSH_CMD "$REMOTE_USER@$SERVER" "mkdir -p $REMOTE_DIR/backtest $REMOTE_DIR/logs/shadow_books"
    for f in "${STATE_FILES[@]}"; do
        if [ -f "$LOCAL_DIR/$f" ]; then
            $SCP_CMD "$LOCAL_DIR/$f" "$REMOTE_USER@$SERVER:$REMOTE_DIR/$f"
            echo "  ✅ $f"
        fi
    done
    rsync -az -e "$RSYNC_SSH" "$LOCAL_DIR/backtest/" "$REMOTE_USER@$SERVER:$REMOTE_DIR/backtest/"
    for d in shadow_books dead_brackets cli_sniper; do
        [ -d "$LOCAL_DIR/logs/$d" ] && rsync -az -e "$RSYNC_SSH" "$LOCAL_DIR/logs/$d/" "$REMOTE_USER@$SERVER:$REMOTE_DIR/logs/$d/"
    done
    for f in live_fills.jsonl live_positions.jsonl live_balance.jsonl trade_events.jsonl; do
        [ -f "$LOCAL_DIR/logs/$f" ] && $SCP_CMD "$LOCAL_DIR/logs/$f" "$REMOTE_USER@$SERVER:$REMOTE_DIR/logs/$f"
    done
    echo "  ✅ backtest data + journals synced"
}

# ─── Remote setup ───────────────────────────
run_remote_setup() {
    echo ""
    echo "[setup] Running remote setup..."
    $SSH_CMD "$REMOTE_USER@$SERVER" "cd $REMOTE_DIR && chmod +x deploy/setup_oracle.sh && bash deploy/setup_oracle.sh"
}

# ─── Install deps ───────────────────────────
install_deps() {
    echo ""
    echo "[deps] Installing Python dependencies on server..."
    $SSH_CMD "$REMOTE_USER@$SERVER" "
        cd $REMOTE_DIR
        if [ ! -d .venv ]; then
            python3 -m venv .venv
        fi
        source .venv/bin/activate
        pip install --upgrade pip -q
        pip install -r requirements.txt -q
    "
    echo "  ✅ Dependencies installed"
}

# ─── Run tests ──────────────────────────────
run_remote_tests() {
    echo ""
    echo "[test] Running tests on server..."
    $SSH_CMD "$REMOTE_USER@$SERVER" "
        cd $REMOTE_DIR
        source .venv/bin/activate
        python3 -m pytest tests/ -v --tb=short 2>&1 | tail -20
    "
}

# ─── Verify ─────────────────────────────────
verify_deployment() {
    echo ""
    echo "[verify] Quick smoke test..."
    $SSH_CMD "$REMOTE_USER@$SERVER" "
        cd $REMOTE_DIR
        source .venv/bin/activate
        python3 -c \"
from config import STATIONS
print(f'  ✅ Config: {len(STATIONS)} cities')
from kalshi_client import KalshiClient
ts = KalshiClient._monotonic_ts_ms()
print(f'  ✅ Kalshi client: monotonic ts={ts}')
from trading_guards import check_kill_switch
ok, reason = check_kill_switch()
print(f'  ✅ Kill switch: {reason}')
print('  ✅ All imports clean')
\"
    "
}

# ─── Execute mode ───────────────────────────
case "$MODE" in
    --secrets)
        upload_secrets
        ;;
    --state)
        upload_state
        ;;
    --full)
        upload_code
        upload_secrets
        upload_state
        run_remote_setup
        run_remote_tests
        verify_deployment
        ;;
    *)
        upload_code
        install_deps
        run_remote_tests
        verify_deployment
        ;;
esac

echo ""
echo "═══════════════════════════════════════════"
echo "  ✅ Deploy complete"
echo "═══════════════════════════════════════════"
echo ""
echo "  Quick commands:"
echo "  ssh $REMOTE_USER@$SERVER 'tail -f /var/log/weather-edge/*.log'"
echo "  ssh $REMOTE_USER@$SERVER 'cd $REMOTE_DIR && .venv/bin/python3 heartbeat.py --status'"
echo "  ssh $REMOTE_USER@$SERVER 'touch $REMOTE_DIR/PAUSE_TRADING'  # Emergency stop"
echo ""
