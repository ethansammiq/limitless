#!/usr/bin/env python3
"""Pre-window briefing — one Discord post before the money hour opens.

The Eastern/Central afternoon CLI floors print ~16:35-17:45 ET, the only
window where settlement documents land while books are still liquid and a
human can act. This job fires shortly before it opens and answers the three
questions the trader needs walking in: what's the account state, is the
sniper alive, and is anything already brewing in today's journal.

Read-only. Never trades (scripts/take.py is the only order path, human-run).

Cron (ET box):
    27 16 * * * $VENV $PROJ/scripts/pre_window_briefing.py >> /var/log/weather-edge/pre_window_briefing.log 2>&1
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

from heartbeat import write_heartbeat  # noqa: E402
from notifications import send_discord_alert  # noqa: E402

HEARTBEAT_FILE = PROJECT_ROOT / "heartbeats.json"
LIVE_ACCOUNT = PROJECT_ROOT / "logs" / "live_account.json"
JOURNAL_DIR = PROJECT_ROOT / "logs" / "cli_sniper"
SNIPER_STALE_S = 600  # cron */2 — anything beyond 10 min means the window is blind


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def account_lines() -> list[str]:
    acct = _read_json(LIVE_ACCOUNT)
    if not acct:
        return ["⚠️ live_account.json unreadable — check live_watch"]
    lines = [f"**Balance:** ${acct.get('balance', '?')}"]
    open_pos = acct.get("open_positions") or []
    if open_pos:
        for p in open_pos:
            lines.append(f"**Open:** {p.get('ticker')} qty {p.get('qty')} "
                         f"(exposure ${p.get('exposure')})")
    else:
        lines.append("**Open positions:** none — full stack available")
    cutoff = datetime.now(timezone.utc).timestamp() - 24 * 3600
    fills = [f for f in acct.get("recent_fills") or []
             if datetime.fromisoformat(f["ts"].replace("Z", "+00:00")).timestamp() > cutoff]
    if fills:
        lines.append(f"**Fills last 24h:** " + "; ".join(
            f"{f['action']} {f['ticker']} @{f['price_c']}¢×{float(f['count']):.0f}"
            for f in fills[:4]))
    return lines


def journal_lines(today: str) -> list[str]:
    path = JOURNAL_DIR / f"{today}.jsonl"
    if not path.exists():
        return ["**Journal today:** no products parsed yet"]
    kinds: dict[str, int] = {}
    conflicts = []
    for line in path.read_text().splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        for f in entry.get("findings", []):
            kinds[f["kind"]] = kinds.get(f["kind"], 0) + 1
            if f["kind"] == "dsm_veto":
                conflicts.append(
                    f"⚡ {f['ticker']}: CLI {f['printed']} vs DSM "
                    f"{f.get('dsm_extreme')} @ {f.get('dsm_time_lst')} LST — revision side")
    summary = ", ".join(f"{v} {k}" for k, v in sorted(kinds.items())) or "none"
    return [f"**Findings today:** {summary}"] + conflicts


def heartbeat_line() -> str:
    beats = _read_json(HEARTBEAT_FILE)
    ts = (beats.get("cli_sniper") or {}).get("timestamp")
    if not ts:
        return "🔴 cli_sniper heartbeat MISSING — window is blind, investigate NOW"
    age = datetime.now(timezone.utc).timestamp() - datetime.fromisoformat(ts).timestamp()
    if age > SNIPER_STALE_S:
        return f"🔴 cli_sniper heartbeat {age / 60:.0f} min stale — window is blind, investigate NOW"
    return f"✅ sniper alive (beat {age:.0f}s ago)"


def main() -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    body_lines = (
        account_lines()
        + journal_lines(today)
        + [heartbeat_line(),
           "_Window opens 16:35 ET — alerts arrive here with the take.py command._"]
    )
    body = "\n".join(body_lines)
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {body}")
    asyncio.run(send_discord_alert(
        title="🕟 Pre-window briefing — money hour in 8 min",
        description=body[:4096],
        color=0x3498DB,
        context="pre_window_briefing",
    ))
    write_heartbeat("pre_window_briefing")


if __name__ == "__main__":
    main()
