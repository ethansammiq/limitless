# Weather Edge — VPS Deployment (provider-agnostic)

## What Gets Deployed

```
SYSTEMD TIMERS / SERVICES
  position_monitor    → Every 5 min — exits, settlement, paper ledger
  watchdog            → Every 15 min — health checks + self-healing
  dashboard_server    → Always on, localhost:8787 (reach via ssh tunnel)

CRON (ET)
  auto_trader         → 15:00 — ONE daily scan (KDE measured -EV; feeds the
                        dashboard opportunities panel only; exec opt-in)
  peak_monitor        → */10, 13-22 — peak formation tracking
  cli_sniper          → */2 — race the NWS climate report to its repricing
  dead_bracket_sweeper→ */15 — obs-killed brackets (all 40 ladders)
  shadow_logger       → */30 — dual-venue L2 depth capture
  live_watch          → */10 — live-account journal + sell-into-strength ping
  backtest_collector  → 8:00 — settlement ground truth (daily_data.jsonl)
  audit_coverage      → Sun 17:30 — series-drift / parse-health self-audit
  sniper_scorecard    → Sun 17:45 — sniper journal → settlement scorecard
  weekly_digest       → Sun 18:00 — per-strategy P&L + base-rate report

  (2026-07-05 consolidation: auto_scan / morning_check / bias_collector
  retired with the demoted KDE forecasting path.)
```

Ad-hoc (human-run, not cron): `scripts/take.py` (the only order-placing
entry point — alerts print the exact command) and
`backtest/poly_gate_analyzer.py` (the Polymarket go/no-go verdict).

**The whole bot moves — the Mac becomes a dev machine.** Cron on an
always-on VPS is the fix for the entire sleep-related incident class
(missed 8AM jobs, drifting catch-ups, stale heartbeats).

---

## Provider choice (minmax)

This workload is tiny — 13 cron services that fire and exit, one small
dashboard, peak RAM a few hundred MB. The floor is **1 GB RAM / 1 shared vCPU
/ ~10 GB**. US-East is a nice-to-have, not a need: the VPS is alert-only
(orders are human-run from the Mac via take.py), so ~90ms EU latency is
irrelevant next to the ~10-min repricing windows it races. Deployed reality
(2026-07-05): Hetzner CX23 Helsinki, $7.09/mo — CAX/CX stock was EU-only and
Hetzner's US tier started at $23.59.

| Provider | ~$/mo | specs | login user | note |
|----------|-------|-------|------------|------|
| **Hetzner CAX11** (Ashburn) | ~4 | 2 ARM / 4 GB | `root` | **best value** — 4× RAM at the 1 GB price |
| Oracle Always Free (Ashburn) | 0 | 4 ARM / 24 GB | `ubuntu` | free forever, but ARM capacity lottery |
| DigitalOcean (NYC) | ~6 | 1 / 1 GB | `root` | cleanest one-click |
| Vultr / Linode (NJ/Newark) | ~5 | 1 / 1 GB | `root` | fine |

The deploy scripts are **provider-agnostic**: pass `REMOTE_USER=root` for
Hetzner/DO/Vultr/Linode; omit it (defaults to `ubuntu`) for Oracle/Lightsail.
`setup_oracle.sh` auto-detects user/home/sudo on the server.

## Step-by-Step Setup

### 1. Create the instance (any provider above)

- **Image:** Ubuntu 22.04 or 24.04
- **Region:** US-East (Ashburn / NYC / NJ) — low Kalshi latency
- **Size:** the cheapest tier (1 GB is plenty)
- **SSH key:** paste `~/.ssh/id_ed25519_ethansammiq.pub`

Oracle-specific (free tier): Home Region **US East (Ashburn)**, Shape
**VM.Standard.A1.Flex** 1 OCPU / 6 GB; if "Out of host capacity," retry another
Availability Domain. Managed providers open port 22 by default; on Oracle add
an ingress rule (Source 0.0.0.0/0, TCP, port 22) to the VCN's default security
list.

### 2. Migrate — ORDER MATTERS (no dual writers)

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
REMOTE_USER=root ./deploy/deploy.sh <instance-ip> --full   # root: Hetzner/DO/Vultr
#            ./deploy/deploy.sh <instance-ip> --full        # ubuntu: Oracle/Lightsail
```

`--full` rsyncs code (state excluded), scps `.env` + key, runs the
one-time `--state` migration (ledger, heartbeats, backtest data, shadow
books), executes `setup_oracle.sh` (Python, chrony, systemd, cron —
jobs go live at this point), then runs the test suite remotely.

### 3. Verify

```bash
ssh <user>@<ip>
crontab -l
systemctl list-timers --all | grep weather
cd ~/limitless && .venv/bin/python3 heartbeat.py --status   # all fresh within the hour
tail -f /var/log/weather-edge/*.log
```

Then from the Mac, tunnel to the dashboard:

```bash
ssh -L 8787:127.0.0.1:8787 <user>@<ip>    # root@ on Hetzner/DO/Vultr
# open http://127.0.0.1:8787 — radar + heartbeats should be live
```

### 4. New deploy model

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
