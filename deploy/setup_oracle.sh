#!/bin/bash
# =============================================================================
# WEATHER EDGE — VPS provisioning (Ubuntu 22.04+, any provider)
# Runs ON the server as whoever logged in (root on Hetzner/DO/Vultr,
# ubuntu on Oracle/Lightsail) — user/home/sudo are all auto-detected.
# =============================================================================
set -euo pipefail

RUN_USER="$(id -un)"
DEPLOY_DIR="$HOME/limitless"
VENV_DIR="$DEPLOY_DIR/.venv"
LOG_DIR="/var/log/weather-edge"
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"   # root needs no sudo

echo "═══════════════════════════════════════════"
echo "  Weather Edge — Oracle Cloud Setup"
echo "═══════════════════════════════════════════"

# ─────────────────────────────────────────────
# 1. SYSTEM UPDATES & DEPENDENCIES
# ─────────────────────────────────────────────
echo "[1/7] System packages..."
$SUDO apt update && $SUDO apt upgrade -y
$SUDO apt install -y \
    python3 python3-pip python3-venv \
    chrony curl git \
    logrotate

# ─────────────────────────────────────────────
# 2. TIME SYNCHRONIZATION (critical for API signing)
# ─────────────────────────────────────────────
echo "[2/7] Time sync (chrony)..."
$SUDO systemctl enable chrony
$SUDO systemctl start chrony
$SUDO chronyc makestep 2>/dev/null || true

# Set timezone to ET for readable logs (cron still uses ET times)
$SUDO timedatectl set-timezone America/New_York
echo "  Timezone: $(timedatectl show --value -p Timezone)"
echo "  Time sync: $(chronyc tracking 2>/dev/null | grep 'System time' || echo 'OK')"

# ─────────────────────────────────────────────
# 3. NETWORK TUNING
# ─────────────────────────────────────────────
echo "[3/7] Network optimizations..."
$SUDO tee /etc/sysctl.d/99-weather-edge.conf > /dev/null << 'EOF'
# Weather Edge — network tuning for API latency
net.ipv4.tcp_keepalive_time=60
net.ipv4.tcp_keepalive_intvl=10
net.ipv4.tcp_keepalive_probes=6
net.ipv4.tcp_fastopen=3
net.core.rmem_max=16777216
net.core.wmem_max=16777216
EOF
$SUDO sysctl --system > /dev/null 2>&1

# ─────────────────────────────────────────────
# 4. CREATE PROJECT STRUCTURE
# ─────────────────────────────────────────────
echo "[4/7] Project structure..."
$SUDO mkdir -p "$LOG_DIR"
$SUDO chown $RUN_USER:$RUN_USER "$LOG_DIR"
mkdir -p "$DEPLOY_DIR"

if [ ! -d "$DEPLOY_DIR/.git" ] && [ ! -f "$DEPLOY_DIR/config.py" ]; then
    echo ""
    echo "  ⚠ No code found at $DEPLOY_DIR"
    echo "  Upload your code first with deploy.sh, then re-run this script."
    echo "  Or: scp -r /path/to/limitless/* $RUN_USER@<ip>:~/limitless/"
    echo ""
fi

# ─────────────────────────────────────────────
# 5. PYTHON VIRTUAL ENVIRONMENT
# ─────────────────────────────────────────────
echo "[5/7] Python environment..."
cd "$DEPLOY_DIR"

python3 --version
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
pip install --upgrade pip setuptools wheel -q
if [ -f requirements.txt ]; then
    pip install -r requirements.txt -q
    echo "  ✅ Dependencies installed"
else
    echo "  ⚠ requirements.txt not found — install deps after uploading code"
fi

# ─────────────────────────────────────────────
# 6. SYSTEMD SERVICES
# ─────────────────────────────────────────────
echo "[6/7] Installing systemd services..."

# Position Monitor — persistent service (every 5 min via timer)
$SUDO tee /etc/systemd/system/weather-edge-monitor.service > /dev/null << EOF
[Unit]
Description=Weather Edge Position Monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$RUN_USER
WorkingDirectory=$DEPLOY_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$VENV_DIR/bin/python3 $DEPLOY_DIR/position_monitor.py --once
StandardOutput=append:$LOG_DIR/position_monitor.log
StandardError=append:$LOG_DIR/position_monitor.log
TimeoutStartSec=120
EOF

$SUDO tee /etc/systemd/system/weather-edge-monitor.timer > /dev/null << EOF
[Unit]
Description=Weather Edge Position Monitor Timer (every 5 min)

[Timer]
OnCalendar=*:0/5
Persistent=true
RandomizedDelaySec=10

[Install]
WantedBy=timers.target
EOF

# Watchdog — persistent health check (every 15 min via timer)
$SUDO tee /etc/systemd/system/weather-edge-watchdog.service > /dev/null << EOF
[Unit]
Description=Weather Edge Watchdog
After=network-online.target

[Service]
Type=oneshot
User=$RUN_USER
WorkingDirectory=$DEPLOY_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$VENV_DIR/bin/python3 $DEPLOY_DIR/watchdog.py
StandardOutput=append:$LOG_DIR/watchdog.log
StandardError=append:$LOG_DIR/watchdog.log
TimeoutStartSec=60
EOF

$SUDO tee /etc/systemd/system/weather-edge-watchdog.timer > /dev/null << EOF
[Unit]
Description=Weather Edge Watchdog Timer (every 15 min)

[Timer]
OnCalendar=*:0/15
Persistent=true
RandomizedDelaySec=30

[Install]
WantedBy=timers.target
EOF

# Dashboard — always-on, localhost only (reach it via ssh tunnel:
#   ssh -L 8787:127.0.0.1:8787 $RUN_USER@<ip>  then open http://127.0.0.1:8787)
$SUDO tee /etc/systemd/system/weather-edge-dashboard.service > /dev/null << EOF
[Unit]
Description=Weather Edge Dashboard (localhost:8787)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$DEPLOY_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$VENV_DIR/bin/python3 $DEPLOY_DIR/dashboard_server.py
Restart=on-failure
RestartSec=10
StandardOutput=append:$LOG_DIR/dashboard.log
StandardError=append:$LOG_DIR/dashboard.log

[Install]
WantedBy=multi-user.target
EOF

$SUDO systemctl daemon-reload
$SUDO systemctl enable weather-edge-monitor.timer
$SUDO systemctl enable weather-edge-watchdog.timer
$SUDO systemctl enable weather-edge-dashboard.service

# ─────────────────────────────────────────────
# 7. CRON JOBS (for scheduled scans)
# ─────────────────────────────────────────────
echo "[7/7] Installing cron jobs..."

# Write crontab (preserves any existing non-weather-edge entries)
EXISTING_CRON=$(crontab -l 2>/dev/null | grep -v "weather-edge" | grep -v "auto_trader" | grep -v "backtest_collector" | grep -v "morning_check" | grep -v "^#.*Weather Edge" || true)

(
echo "$EXISTING_CRON"
cat << EOF

# ═══════════════════════════════════════════════════
# WEATHER EDGE — Automated Trading & Monitoring
# All times are ET (server timezone set to America/New_York)
# ═══════════════════════════════════════════════════

# Auto Trader at scan windows (scan-only by default since 2026-07 —
# order placement needs --execute or AUTO_TRADER_EXECUTE=true in .env)
0 6 * * *  $VENV_DIR/bin/python3 $DEPLOY_DIR/auto_trader.py >> $LOG_DIR/auto_trader.log 2>&1
0 8 * * *  $VENV_DIR/bin/python3 $DEPLOY_DIR/auto_trader.py >> $LOG_DIR/auto_trader.log 2>&1
0 10 * * * $VENV_DIR/bin/python3 $DEPLOY_DIR/auto_trader.py >> $LOG_DIR/auto_trader.log 2>&1
0 15 * * * $VENV_DIR/bin/python3 $DEPLOY_DIR/auto_trader.py >> $LOG_DIR/auto_trader.log 2>&1
0 16 * * * $VENV_DIR/bin/python3 $DEPLOY_DIR/auto_trader.py >> $LOG_DIR/auto_trader.log 2>&1
0 23 * * * $VENV_DIR/bin/python3 $DEPLOY_DIR/auto_trader.py >> $LOG_DIR/auto_trader.log 2>&1

# Evening scan with Discord alert
0 22 * * * $VENV_DIR/bin/python3 $DEPLOY_DIR/auto_scan.py --quiet >> $LOG_DIR/auto_scan.log 2>&1

# Peak Monitor — every 10 min during peak-formation hours
*/10 13-22 * * * $VENV_DIR/bin/python3 $DEPLOY_DIR/peak_monitor.py --once >> $LOG_DIR/peak_monitor.log 2>&1

# Dead-Bracket Sweeper — obs-killed brackets still holding bids (riskless class)
*/15 * * * * $VENV_DIR/bin/python3 $DEPLOY_DIR/dead_bracket_sweeper.py --once >> $LOG_DIR/dead_bracket_sweeper.log 2>&1

# CLI Sniper — race the NWS climate report to its own repricing
*/2 * * * * $VENV_DIR/bin/python3 $DEPLOY_DIR/cli_sniper.py --once >> $LOG_DIR/cli_sniper.log 2>&1

# Shadow Logger — dual-venue L2 depth capture (Poly gate data)
*/30 * * * * $VENV_DIR/bin/python3 $DEPLOY_DIR/shadow_logger.py --once >> $LOG_DIR/shadow_logger.log 2>&1

# Live Watch — read-only live-account journal + sell-into-strength pings
*/10 * * * * $VENV_DIR/bin/python3 $DEPLOY_DIR/live_watch.py --once >> $LOG_DIR/live_watch.log 2>&1

# Weekly Digest — per-strategy P&L + live summary + dead-bracket base rate
0 18 * * 0 $VENV_DIR/bin/python3 $DEPLOY_DIR/weekly_digest.py >> $LOG_DIR/weekly_digest.log 2>&1

# Morning Check — 6:30 AM (position evaluation)
30 6 * * * $VENV_DIR/bin/python3 $DEPLOY_DIR/morning_check.py >> $LOG_DIR/morning_check.log 2>&1

# Backtest Collector — 8:00 AM (after settlement)
0 8 * * * $VENV_DIR/bin/python3 $DEPLOY_DIR/backtest_collector.py >> $LOG_DIR/backtest_collector.log 2>&1

# Bias Collector — 8:30 AM (needs backtest_collector's row first)
30 8 * * * $VENV_DIR/bin/python3 $DEPLOY_DIR/bias_collector.py >> $LOG_DIR/bias_collector.log 2>&1
EOF
) | crontab -

echo "  ✅ Cron jobs installed (auto_trader is scan-only by default)"

# ─────────────────────────────────────────────
# LOG ROTATION
# ─────────────────────────────────────────────
$SUDO tee /etc/logrotate.d/weather-edge > /dev/null << EOF
$LOG_DIR/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 644 $RUN_USER $RUN_USER
}
EOF

# ─────────────────────────────────────────────
# VERIFICATION
# ─────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════"
echo "  SETUP COMPLETE"
echo "═══════════════════════════════════════════"
echo ""

if [ -f "$DEPLOY_DIR/.env" ] && [ -f "$DEPLOY_DIR/kalshi_private_key.pem" ]; then
    echo "  ✅ .env file found"
    echo "  ✅ Kalshi private key found"

    # Quick smoke test
    source "$VENV_DIR/bin/activate"
    if python3 -c "from config import STATIONS; print(f'  ✅ Config loaded: {len(STATIONS)} cities')" 2>/dev/null; then
        echo ""
    else
        echo "  ⚠ Config import failed — check code upload"
    fi
else
    echo "  ⚠ Missing files — upload with deploy.sh:"
    [ ! -f "$DEPLOY_DIR/.env" ] && echo "    - .env"
    [ ! -f "$DEPLOY_DIR/kalshi_private_key.pem" ] && echo "    - kalshi_private_key.pem"
fi

echo ""
echo "  NEXT STEPS:"
echo "  ─────────────────────────────────────────"
echo "  1. Upload code:      ./deploy/deploy.sh <server-ip>"
echo "  2. Verify dry-run:   ssh $RUN_USER@<ip> '$VENV_DIR/bin/python3 $DEPLOY_DIR/auto_trader.py --dry-run'"
echo "  3. Start monitors:   ssh $RUN_USER@<ip> 'sudo systemctl start weather-edge-monitor.timer weather-edge-watchdog.timer'"
echo "  4. Watch logs:       ssh $RUN_USER@<ip> 'tail -f $LOG_DIR/*.log'"
echo "  5. Go live:          Edit cron, remove --dry-run flags"
echo ""
echo "  EMERGENCY STOP:"
echo "  ssh $RUN_USER@<ip> 'touch $DEPLOY_DIR/PAUSE_TRADING'"
echo ""
echo "  VIEW STATUS:"
echo "  systemctl list-timers --all | grep weather"
echo "  crontab -l"
echo ""
