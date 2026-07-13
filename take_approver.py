#!/usr/bin/env python3
"""TAKE APPROVER — one-tap Discord approval for staged sniper commands.

The 2026-07-12 MSP T91 alert (18¢, drift 88%) had a measured ~10-15 min
fillable window and went unbought: the human leg (notice → shell → take.py)
is the latency, not detection. This job collapses it to a phone tap while
keeping the repo's Core Rule intact — a human still individually authorizes
every order, and scripts/take.py remains the ONLY order-placing entry point
(this job is its subprocess caller, never a second implementation).

Per run (cron */1, one-shot — Principle 8: no resident daemon):
  1. expire queue entries past TAKE_APPROVE_TTL_MIN (default 15)
  2. post new entries to DISCORD_TAKE_CHANNEL_ID with a ✅ self-reaction
  3. for posted entries, read ✅ reactors; a tap by an allow-listed user
     re-checks the live book (buys: ask ≤ staged price; sells: bid ≥ staged
     price — else "repriced", no order), then runs take.py --ioc --yes and
     edits the message with the fill result
Guardrails: allow-list only (DISCORD_TAKE_APPROVER_IDS), at-most-once (the
"executing" status persists BEFORE the subprocess; a crash mid-order is
surfaced, never retried), IOC only (automation never leaves resting
orders), notional capped at enqueue AND re-validated by take.py itself.

Unconfigured (no DISCORD_BOT_TOKEN) the job exits cleanly and the queue
drains by TTL — the feature is strictly additive to the existing alerts.

.env keys:
    DISCORD_BOT_TOKEN=...            # bot with View/Send/Add Reactions/History
    DISCORD_TAKE_CHANNEL_ID=...      # channel the TAKE? prompts post to
    DISCORD_TAKE_APPROVER_IDS=1,2    # Discord user ids allowed to fire
    TAKE_APPROVE_TTL_MIN=15          # optional
Suggested crontab (NOT auto-installed):
    * * * * * $VENV $PROJ/take_approver.py --once >> /tmp/take_approver.log 2>&1
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from core import take_queue  # noqa: E402
from heartbeat import write_heartbeat  # noqa: E402
from log_setup import get_logger  # noqa: E402
from scripts.take import order_cost_dollars  # noqa: E402

logger = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
TAKE_SCRIPT = PROJECT_ROOT / "scripts" / "take.py"
DISCORD_API = "https://discord.com/api/v10"
CHECK = "✅"  # ✅
SUBPROCESS_TIMEOUT_S = 90


def _config() -> dict | None:
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    channel = os.getenv("DISCORD_TAKE_CHANNEL_ID", "").strip()
    approvers = {u.strip() for u in
                 os.getenv("DISCORD_TAKE_APPROVER_IDS", "").split(",") if u.strip()}
    if not (token and channel and approvers):
        return None
    return {"token": token, "channel": channel, "approvers": approvers}


def format_prompt(entry: dict, ttl_min: int) -> str:
    """The TAKE? message. 💰 REAL up front — this button costs money."""
    cost = order_cost_dollars(entry["action"], entry["side"],
                              entry["count"], entry["price_c"])
    expires = int(datetime.fromisoformat(entry["ts"]).timestamp()) + ttl_min * 60
    lines = [f"💰 REAL · **TAKE?** `{entry['ticker']}` {entry['action']} "
             f"{entry['side']} {entry['count']}× @ {entry['price_c']}¢ — "
             f"worst-case ${cost:.2f} [{entry['source']}]"]
    if entry.get("summary"):
        lines.append(entry["summary"])
    lines.append(f"React {CHECK} to fire (IOC via take.py) — expires <t:{expires}:R>")
    return "\n".join(lines)


def decide(entry: dict, now_utc: datetime, reactor_ids: set[str],
           approver_ids: set[str], live_px: int | None,
           ttl_min: int) -> tuple[str, str]:
    """(verdict, reason) for one posted entry. Pure — the whole guardrail
    stack in one testable place.

    verdicts: "expire" | "wait" | "reprice" | "execute"
    """
    if take_queue.is_expired(entry, now_utc, ttl_min):
        return "expire", f"no approval within {ttl_min} min"
    if not (reactor_ids & approver_ids):
        return "wait", "no allow-listed reaction yet"
    if live_px is None:
        # Book unreadable — fail closed on the order, retry inside the TTL.
        return "wait", "book unreadable — retrying"
    if entry["action"] == "buy" and live_px > entry["price_c"]:
        return "reprice", f"ask now {live_px}¢ > staged {entry['price_c']}¢"
    if entry["action"] == "sell" and live_px < entry["price_c"]:
        return "reprice", f"bid now {live_px}¢ < staged {entry['price_c']}¢"
    return "execute", "approved"


def build_take_argv(entry: dict) -> list[str]:
    """argv for the staged order — list form, no shell, no injection surface."""
    return [sys.executable, str(TAKE_SCRIPT), entry["ticker"], entry["action"],
            entry["side"], str(entry["count"]), str(entry["price_c"]),
            "--ioc", "--yes"]


def run_take(entry: dict) -> tuple[bool, str]:
    """Execute take.py; (ok, output tail). Never raises — the caller must
    always get something to persist and post."""
    try:
        proc = subprocess.run(build_take_argv(entry), capture_output=True,
                              text=True, timeout=SUBPROCESS_TIMEOUT_S,
                              cwd=PROJECT_ROOT)
        out = (proc.stdout + proc.stderr).strip()
        return proc.returncode == 0, out[-600:]
    except subprocess.TimeoutExpired:
        return False, f"take.py timed out after {SUBPROCESS_TIMEOUT_S}s — CHECK FILLS"
    except OSError as exc:
        return False, f"take.py could not start: {exc}"


# --- Discord REST (bot token; the webhook can't read reactions) -------------

async def _discord(session, method: str, path: str, token: str,
                   json_body: dict | None = None):
    """One Discord REST call → parsed JSON (or None). 429 honors retry_after
    once; other failures log and return None — one API hiccup must not kill
    the run (the next */1 tick retries)."""
    import aiohttp

    url = f"{DISCORD_API}{path}"
    headers = {"Authorization": f"Bot {token}"}
    for attempt in (0, 1):
        try:
            async with session.request(
                    method, url, headers=headers, json=json_body,
                    timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 429 and attempt == 0:
                    data = await resp.json()
                    await asyncio.sleep(min(float(data.get("retry_after", 1)), 5))
                    continue
                if resp.status in (200, 201, 204):
                    return await resp.json() if resp.status != 204 else {}
                body = await resp.text()
                logger.warning(f"discord {method} {path} → {resp.status}: {body[:200]}")
                return None
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            logger.warning(f"discord {method} {path} failed: {exc}")
            return None
    return None


async def post_prompt(session, cfg: dict, entry: dict, ttl_min: int) -> str | None:
    msg = await _discord(session, "POST", f"/channels/{cfg['channel']}/messages",
                         cfg["token"], {"content": format_prompt(entry, ttl_min)})
    if not msg or "id" not in msg:
        return None
    # Self-react so approval is a single tap on an existing reaction.
    await _discord(session, "PUT",
                   f"/channels/{cfg['channel']}/messages/{msg['id']}"
                   f"/reactions/{quote(CHECK)}/@me", cfg["token"])
    return msg["id"]


async def get_reactors(session, cfg: dict, message_id: str) -> set[str] | None:
    users = await _discord(session, "GET",
                           f"/channels/{cfg['channel']}/messages/{message_id}"
                           f"/reactions/{quote(CHECK)}?limit=100", cfg["token"])
    if users is None:
        return None  # read failed ≠ nobody reacted
    return {u.get("id", "") for u in users}


async def edit_message(session, cfg: dict, message_id: str, content: str) -> None:
    await _discord(session, "PATCH",
                   f"/channels/{cfg['channel']}/messages/{message_id}",
                   cfg["token"], {"content": content[:1900]})


# --- live book re-check ------------------------------------------------------

async def fetch_live_px(client, entry: dict) -> int | None:
    """Current entry-side price: buys → best ask, sells → best bid."""
    try:
        book = await client.get_orderbook(entry["ticker"])
    except Exception as exc:  # noqa: BLE001 — fail closed via None
        logger.warning(f"{entry['ticker']}: book fetch failed: {exc}")
        return None
    yes_bids = sorted((book or {}).get("yes") or [], key=lambda lv: -lv[0])
    no_bids = sorted((book or {}).get("no") or [], key=lambda lv: -lv[0])
    if entry["action"] == "buy":
        return 100 - no_bids[0][0] if no_bids else None
    return yes_bids[0][0] if yes_bids else None


async def run(dry_run: bool) -> None:
    cfg = _config()
    if cfg is None:
        logger.info("take approver: not configured (DISCORD_BOT_TOKEN / "
                    "DISCORD_TAKE_CHANNEL_ID / DISCORD_TAKE_APPROVER_IDS) — idle")
        return

    import aiohttp

    now_utc = datetime.now(timezone.utc)
    ttl = take_queue.ttl_minutes()
    queue = take_queue.load_queue()
    entries = [e for e in queue["entries"].values()
               if e.get("status") in take_queue.ACTIVE_STATUSES]
    if not entries:
        logger.info("take approver: queue empty")
        return

    mutations: dict[str, dict] = {}
    kalshi = None
    async with aiohttp.ClientSession() as session:
        for entry in sorted(entries, key=lambda e: e.get("ts", "")):
            eid = entry["id"]

            if entry["status"] == "pending":
                if take_queue.is_expired(entry, now_utc, ttl):
                    mutations[eid] = {"status": "expired",
                                      "resolved_ts": now_utc.isoformat(timespec="seconds")}
                    continue
                if dry_run:
                    print(f"would post: {format_prompt(entry, ttl)}")
                    continue
                mid = await post_prompt(session, cfg, entry, ttl)
                if mid:
                    mutations[eid] = {"status": "posted", "message_id": mid,
                                      "posted_ts": now_utc.isoformat(timespec="seconds")}
                    logger.info(f"{entry['ticker']}: TAKE? posted ({eid})")
                continue

            # status == "posted"
            reactors = None if dry_run else await get_reactors(
                session, cfg, entry["message_id"])
            live_px = None
            if reactors and (reactors & cfg["approvers"]):
                if kalshi is None:
                    from kalshi_client import KalshiClient

                    kalshi = KalshiClient(
                        api_key_id=os.getenv("KALSHI_API_KEY_ID", ""),
                        private_key_path=os.getenv("KALSHI_PRIVATE_KEY_PATH", ""),
                        demo_mode=False)
                    await kalshi.start()
                live_px = await fetch_live_px(kalshi, entry)
            verdict, reason = decide(entry, now_utc, reactors or set(),
                                     cfg["approvers"], live_px, ttl)

            if verdict == "wait":
                continue
            if verdict == "expire":
                mutations[eid] = {"status": "expired",
                                  "resolved_ts": now_utc.isoformat(timespec="seconds")}
                if not dry_run and entry.get("message_id"):
                    await edit_message(session, cfg, entry["message_id"],
                                       f"⌛ EXPIRED — {reason}\n~~{format_prompt(entry, ttl)}~~")
                continue
            if verdict == "reprice":
                mutations[eid] = {"status": "repriced", "result": reason,
                                  "resolved_ts": now_utc.isoformat(timespec="seconds")}
                if not dry_run:
                    await edit_message(session, cfg, entry["message_id"],
                                       f"↕ NOT EXECUTED — {reason}\n"
                                       f"~~{format_prompt(entry, ttl)}~~")
                logger.info(f"{entry['ticker']}: repriced — {reason}")
                continue

            # verdict == "execute" — persist the marker BEFORE the order so a
            # crash mid-subprocess can never double-fire (at-most-once).
            if dry_run:
                print(f"would execute: {' '.join(build_take_argv(entry))}")
                continue
            take_queue.update_entries(
                {eid: {"status": "executing",
                       "resolved_ts": now_utc.isoformat(timespec="seconds")}})
            ok, output = run_take(entry)
            mutations[eid] = {"status": "executed" if ok else "failed",
                              "result": output,
                              "resolved_ts": now_utc.isoformat(timespec="seconds")}
            logger.info(f"{entry['ticker']}: take.py {'ok' if ok else 'FAILED'}")
            await edit_message(
                session, cfg, entry["message_id"],
                f"{'✅ EXECUTED' if ok else '❌ FAILED'} `{entry['ticker']}` "
                f"{entry['action']} {entry['count']}× @ {entry['price_c']}¢\n"
                f"```\n{output[-800:]}\n```")

    if kalshi is not None:
        await kalshi.stop()
    if mutations and not dry_run:
        take_queue.update_entries(mutations, now_utc)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--once", action="store_true", help="single pass (cron mode)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print decisions, no Discord/orders/state")
    args = ap.parse_args()
    if not args.once:
        ap.error("use --once (cron)")
    # Single-instance run lock (same overlap class as the snipers): a slow
    # Discord/Kalshi call must not overlap the next */1 tick — two instances
    # could both read "posted" before either persists "executing".
    import fcntl
    with (PROJECT_ROOT / ".take_approver.lock").open("w") as lock_fd:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info("take approver: previous run still active — skipping")
            return
        asyncio.run(run(args.dry_run))
        write_heartbeat("take_approver")


if __name__ == "__main__":
    main()
