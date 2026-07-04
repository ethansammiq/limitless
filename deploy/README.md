# Weather Edge — Oracle Cloud Free Tier Deployment

## Why Oracle Cloud?
- **$0/month forever** (not a trial — truly always-free)
- 4 ARM cores, 24 GB RAM, 200 GB storage
- Ashburn, VA region: 5-15ms to Kalshi (same coast)
- 99.9% uptime SLA

## What Gets Deployed

```
SYSTEMD TIMERS / SERVICES
  position_monitor    → Every 5 min — exits, settlement, paper ledger
  watchdog            → Every 15 min — health checks + self-healing
  dashboard_server    → Always on, localhost:8787 (reach via ssh tunnel)

CRON (ET)
  auto_trader         → 6/8/10/15/16/23 — SCAN-ONLY by default (exec opt-in)
  auto_scan --quiet   → 22:00 — evening Discord scan
  peak_monitor        → */10, 13-22 — peak formation tracking
  dead_bracket_sweeper→ */15 — obs-killed brackets still holding bids
  shadow_logger       → */30 — dual-venue L2 depth capture
  morning_check       → 6:30 — pre-settlement position evaluation
  backtest_collector  → 8:00 — settlement data collection
  bias_collector      → 8:30 — model bias rows (needs backtest row first)
```

**The whole bot moves — the Mac becomes a dev machine.** Cron on an
always-on VPS is the fix for the entire sleep-related incident class
(missed 8AM jobs, drifting catch-ups, stale heartbeats).

---

## Step-by-Step Setup

### 1. Create Oracle Cloud Account

1. Go to https://www.oracle.com/cloud/free/
2. Sign up (credit card required for identity verification — never charged for free tier)
3. Select **Home Region: US East (Ashburn)** — lowest latency to Kalshi

### 2. Create ARM Instance

In the Oracle Cloud Console:

```
Compute → Instances → Create Instance

Name:           weather-edge
Image:          Ubuntu 22.04 (or 24.04)
Shape:          VM.Standard.A1.Flex (Ampere ARM)
  OCPUs:        1 (free tier allows up to 4)
  Memory:       6 GB (free tier allows up to 24 GB)
Networking:     Create new VCN + public subnet
  Public IP:    Yes
Boot volume:    50 GB (free tier allows up to 200 GB)
SSH key:        Paste your public key (~/.ssh/id_rsa.pub)
```

> **Tip:** If you get "Out of host capacity," try a different Availability Domain
> or try again later (ARM instances are popular). Scripts exist to auto-retry.

### 3. Open SSH Port

```
Networking → Virtual Cloud Networks → [your VCN]
  → Security Lists → Default → Ingress Rules
  → Add: Source 0.0.0.0/0, TCP, Port 22
```

### 4. SSH In & Run Setup

```bash
# From your Mac
ssh ubuntu@<oracle-instance-ip>

# On the server — one-time setup
mkdir -p ~/limitless
```

### 5. Migrate — ORDER MATTERS (no dual writers)

The paper ledger is file-based state. Two machines running
position_monitor against separate copies will diverge silently — the
exact corruption class the 2026-06-25 rebuild fixed. Migrate in this
order:

```bash
# a. STOP the Mac's writers FIRST
crontab -l > ~/mac_crontab_backup.txt   # keep a copy
crontab -r                              # Mac cron off
pkill -f dashboard_server.py            # stop local dashboard

# b. Ship everything (code + secrets + state + setup + tests)
chmod +x deploy/deploy.sh
./deploy/deploy.sh <instance-ip> --full
```

`--full` rsyncs code (state excluded), scps `.env` + key, runs the
one-time `--state` migration (ledger, heartbeats, backtest data, shadow
books), executes `setup_oracle.sh` (Python, chrony, systemd, cron —
jobs go live at this point), then runs the test suite remotely.

### 6. Verify

```bash
ssh ubuntu@<ip>
crontab -l
systemctl list-timers --all | grep weather
cd ~/limitless && .venv/bin/python3 heartbeat.py --status   # all fresh within the hour
tail -f /var/log/weather-edge/*.log
```

Then from the Mac, tunnel to the dashboard:

```bash
ssh -L 8787:127.0.0.1:8787 ubuntu@<ip>
# open http://127.0.0.1:8787 — radar + heartbeats should be live
```

### 7. New deploy model

Cron no longer runs from the Mac's working tree. Code changes flow:

```bash
# On the Mac: commit + push as usual, then
./deploy/deploy.sh <ip>        # code-only rsync — NEVER touches state
# or on the server: cd ~/limitless && git pull
```

Never run `--state` again once the server is live — it would overwrite
the live ledger with stale Mac copies.

---

## Day-to-Day Operations

### View logs
```bash
ssh ubuntu@<ip> 'tail -f /var/log/weather-edge/auto_trader.log'
ssh ubuntu@<ip> 'tail -f /var/log/weather-edge/position_monitor.log'
```

### Emergency stop
```bash
ssh ubuntu@<ip> 'touch ~/limitless/PAUSE_TRADING'     # HALT
ssh ubuntu@<ip> 'rm ~/limitless/PAUSE_TRADING'         # RESUME
```

### Push code updates
```bash
./deploy/deploy.sh <ip>           # Fast: code + deps + tests only
./deploy/deploy.sh <ip> --secrets  # Upload just .env + key
```

### Check positions
```bash
ssh ubuntu@<ip> 'cat ~/limitless/positions.json | python3 -m json.tool'
```

### Run a manual scan
```bash
ssh ubuntu@<ip> 'cd ~/limitless && source .venv/bin/activate && python3 auto_trader.py --dry-run --city NYC'
```

### Service status
```bash
ssh ubuntu@<ip> 'systemctl list-timers --all | grep weather'
ssh ubuntu@<ip> 'crontab -l'
```

---

## Architecture on Server

```
/home/ubuntu/limitless/
├── .env                    # Secrets (never rsync'd)
├── .venv/                  # Python venv
├── kalshi_private_key.pem  # RSA key (chmod 600)
├── positions.json          # Live positions (auto-created)
├── config.py               # All settings
├── auto_trader.py          # Main scan+trade loop (cron)
├── position_monitor.py     # Exit rules (systemd timer)
├── watchdog.py             # Health checks (systemd timer)
├── morning_check.py        # Pre-settlement (cron)
├── backtest_collector.py   # Data collection (cron)
├── edge_scanner_v2.py      # KDE + ensemble scanner
├── kalshi_client.py        # API client
├── execute_trade.py        # Order execution
├── position_store.py       # Atomic file store
├── trading_guards.py       # Safety checks
├── notifications.py        # Discord alerts
└── deploy/
    ├── setup_oracle.sh     # One-time server setup
    ├── deploy.sh           # Push code updates
    └── .env.example        # Template

/var/log/weather-edge/
├── auto_trader.log
├── position_monitor.log
├── watchdog.log
├── backtest_collector.log
└── morning_check.log       # 14-day rotation
```

---

## Cost Comparison

| Provider | Spec | Latency to Kalshi | Cost |
|----------|------|-------------------|------|
| **Oracle Cloud (Ashburn)** | 1 ARM core, 6 GB | 5-15ms | **$0/mo forever** |
| AWS EC2 t3.micro | 2 vCPU, 1 GB | <5ms | $0 for 12mo, then $8/mo |
| DigitalOcean (NYC) | 1 vCPU, 1 GB | 5-10ms | $6/mo |
| Vultr (NJ) | 1 vCPU, 1 GB | 5-10ms | $5/mo |

Oracle Ashburn is the best option: zero cost, sufficient specs, acceptable latency.
Latency doesn't matter much for this system — we place limit orders at scan time
(5x daily), not HFT. 10ms vs 5ms is irrelevant for a cron-based scanner.

---

## Troubleshooting

### "Out of host capacity" when creating instance
ARM instances are popular. Solutions:
- Try a different Availability Domain (AD-1, AD-2, AD-3)
- Try during off-peak hours (early morning US time)
- Use an auto-retry script: search "oracle cloud instance creation script"

### Tests fail on server
```bash
ssh ubuntu@<ip> 'cd ~/limitless && source .venv/bin/activate && python3 -m pytest tests/ -v'
```
Common issues: missing numpy on ARM (install via apt: `sudo apt install python3-numpy`)

### Discord alerts not working
```bash
ssh ubuntu@<ip> 'grep DISCORD ~/limitless/.env'
# Verify webhook URL is set
```

### Cron jobs not running
```bash
ssh ubuntu@<ip> 'grep CRON /var/log/syslog | tail -20'
# Check timezone
ssh ubuntu@<ip> 'timedatectl'
```

### Position monitor not running
```bash
ssh ubuntu@<ip> 'systemctl status weather-edge-monitor.timer'
ssh ubuntu@<ip> 'systemctl status weather-edge-monitor.service'
ssh ubuntu@<ip> 'journalctl -u weather-edge-monitor -n 20'
```
