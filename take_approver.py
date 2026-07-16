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
While entries are active, the one-shot re-polls every POLL_INTERVAL_S
inside its own minute (2026-07-14: three floor races were lost with taps
landing inside 60s — the cron-boundary wait, not the human, was the
latency). Still a cron one-shot: it exits the moment the queue is idle,
and the run lock keeps ticks from overlapping. Snipers additionally call
post_new_entries() right after staging, so the button (and its phone
push) goes out at detection time instead of the next tick.
Guardrails: allow-list only (DISCORD_TAKE_APPROVER_IDS), at-most-once (the
"executing" status persists BEFORE the subprocess; a crash mid-order is
surfaced, never retried), IOC only (automation never leaves resting
orders), notional capped at enqueue AND re-validated by take.py itself,
and a portfolio-day cap on every fire — manual and auto (2026-07-16:
staging keeps buttons inside it; the fire-time check catches env/bankroll
moves mid-flight and resolves the entry "capped", order never placed).

AUTO-TAKE, 00Z class only (2026-07-14, ships in SHADOW mode): entries the
queue marked `auto_eligible` (METAR high-ladder buy_winner from the 00Z
synoptic group — day-max == final CLI 98.4%, all four groups in) can skip
the tap. Default is shadow: the would-fire decision (live-book re-check +
caps) is journaled once per entry to logs/take_approver/YYYY-MM-DD.jsonl
and the button still works normally. AUTO_TAKE_00Z=on flips to live
auto-fire behind extra caps (fires/day + daily auto notional, on top of
the per-order clamp) — pre-registered flip-on gate in claude.md §1; do not
set it before the shadow week grades.

Unconfigured (no DISCORD_BOT_TOKEN) the job exits cleanly and the queue
drains by TTL — the feature is strictly additive to the existing alerts.

.env keys:
    DISCORD_BOT_TOKEN=...            # bot with View/Send/Add Reactions/History
    DISCORD_TAKE_CHANNEL_ID=...      # channel the TAKE? prompts post to
    DISCORD_TAKE_APPROVER_IDS=1,2    # Discord user ids allowed to fire
    TAKE_APPROVE_TTL_MIN=15          # optional
    TAKE_NIGHT_CAP_DOLLARS=25        # optional OVERRIDE: $ per station-night —
                                     # default derives from the live bankroll
                                     # (15%, never above $25; core/risk.py)
    TAKE_DAILY_CAP_DOLLARS=60        # optional OVERRIDE: $ portfolio-wide per
                                     # UTC day, all fires (35%, never above $60)
    AUTO_TAKE_00Z=on                 # optional: live auto-fire (default shadow)
    AUTO_TAKE_MAX_PER_DAY=3          # optional: auto-fires allowed per UTC day
    AUTO_TAKE_DAILY_CAP=30           # optional: $ auto notional per UTC day
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

from core import risk, take_queue  # noqa: E402
from core.risk import order_cost_dollars  # noqa: E402
from heartbeat import write_heartbeat  # noqa: E402
from log_setup import get_logger  # noqa: E402

logger = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
TAKE_SCRIPT = PROJECT_ROOT / "scripts" / "take.py"
SHADOW_JOURNAL_DIR = PROJECT_ROOT / "logs" / "take_approver"
DISCORD_API = "https://discord.com/api/v10"
CHECK = "✅"  # ✅
SUBPROCESS_TIMEOUT_S = 90
# Tap→fire budget: keep polling reactions this long within one cron minute,
# then exit before the next tick wants the lock.
POLL_WINDOW_S = 45
POLL_INTERVAL_S = 5

AUTO_MAX_FIRES_PER_DAY = 3
AUTO_DAILY_CAP_DOLLARS = 30.0


def auto_mode() -> str:
    """"on" fires the 00Z class without a tap; anything else is shadow."""
    return "on" if os.getenv("AUTO_TAKE_00Z", "").strip().lower() == "on" \
        else "shadow"


def auto_max_fires() -> int:
    try:
        return int(os.getenv("AUTO_TAKE_MAX_PER_DAY", AUTO_MAX_FIRES_PER_DAY))
    except ValueError:
        return AUTO_MAX_FIRES_PER_DAY


def auto_daily_cap() -> float:
    try:
        return float(os.getenv("AUTO_TAKE_DAILY_CAP", AUTO_DAILY_CAP_DOLLARS))
    except ValueError:
        return AUTO_DAILY_CAP_DOLLARS


def _config() -> dict | None:
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    channel = os.getenv("DISCORD_TAKE_CHANNEL_ID", "").strip()
    approvers = {u.strip() for u in
                 os.getenv("DISCORD_TAKE_APPROVER_IDS", "").split(",") if u.strip()}
    if not (token and channel and approvers):
        return None
    return {"token": token, "channel": channel, "approvers": approvers,
            "auto_mode": auto_mode()}


def daily_allowance(entry: dict, all_entries: dict, now_utc: datetime,
                    ) -> tuple[bool, str]:
    """(ok, reason): would firing this entry keep the whole portfolio inside
    the daily cap? Applies to EVERY fire — manual tap and auto alike (auto
    keeps its own tighter fires/notional caps ON TOP).

    Ledger = the queue itself (same source auto_allowance uses; the 48h
    retention covers a UTC day). Staged entries count as committed money —
    staging already enforced this cap, so a fire-time breach means the env
    changed mid-flight, entries predate a deploy, or the bankroll dropped:
    refuse, which is always the tighter answer. The candidate itself is
    excluded from the spent sum (it is already staged in the queue)."""
    cap, cap_why = risk.daily_cap_detail(now_utc)
    cost = order_cost_dollars(entry["action"], entry["side"],
                              entry["count"], entry["price_c"])
    spent = take_queue.day_spent_dollars(all_entries, now_utc,
                                         exclude_id=entry["id"])
    if spent + cost > cap:
        return False, (f"${spent + cost:.2f} would exceed the portfolio-day "
                       f"cap ({cap_why})")
    return True, f"${spent:.2f} committed today (cap {cap_why})"


def auto_allowance(entry: dict, all_entries: dict, now_utc: datetime,
                   ) -> tuple[bool, str]:
    """(ok, reason): would auto-firing this entry stay inside the auto caps?

    Auto caps sit ON TOP of the per-order notional clamp: fires per UTC day
    and total auto notional per UTC day, both counted from `auto_fired`
    entries in the queue itself (48h retention covers a day). An entry
    stuck in "executing" counts as spent — a crash mid-order is treated as
    money out the door until a human reconciles fills.
    """
    today = now_utc.date().isoformat()
    fired = [e for e in all_entries.values()
             if e.get("auto_fired") and (e.get("resolved_ts") or "").startswith(today)]
    max_fires = auto_max_fires()
    if len(fired) >= max_fires:
        return False, f"{len(fired)} auto-fires already today (max {max_fires})"
    cost = order_cost_dollars(entry["action"], entry["side"],
                              entry["count"], entry["price_c"])
    spent = sum(order_cost_dollars(e["action"], e["side"],
                                   e["count"], e["price_c"]) for e in fired)
    cap = auto_daily_cap()
    if spent + cost > cap:
        return False, f"${spent + cost:.2f} would exceed ${cap:.2f} daily auto cap"
    return True, f"{len(fired)} fire(s) / ${spent:.2f} auto-spent today"


def shadow_record(entry: dict, live_px: int | None, verdict: str, reason: str,
                  caps_ok: bool, caps_reason: str, now_utc: datetime) -> dict:
    """One graded auto-shadow row: what live auto mode WOULD have done."""
    return {"ts": now_utc.isoformat(timespec="seconds"), "kind": "auto_shadow",
            "id": entry["id"], "ticker": entry["ticker"],
            "action": entry["action"], "side": entry["side"],
            "count": entry["count"], "price_c": entry["price_c"],
            "live_px": live_px,
            "cost_dollars": round(order_cost_dollars(
                entry["action"], entry["side"],
                entry["count"], entry["price_c"]), 2),
            "would": verdict, "reason": reason,
            "caps_ok": caps_ok, "caps_reason": caps_reason}


def _journal_shadow(record: dict) -> None:
    SHADOW_JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    path = SHADOW_JOURNAL_DIR / f"{record['ts'][:10]}.jsonl"
    with path.open("a") as fh:
        fh.write(json.dumps(record, separators=(",", ":")) + "\n")


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
    if entry.get("auto_eligible"):
        lines.append("🤖 auto-eligible (00Z day-max class) — "
                     + ("fires on live-book check, no tap needed"
                        if auto_mode() == "on"
                        else "shadow mode: tap still required"))
    lines.append(f"React {CHECK} to fire (IOC via take.py) — expires <t:{expires}:R>")
    return "\n".join(lines)


def decide(entry: dict, now_utc: datetime, reactor_ids: set[str],
           approver_ids: set[str], live_px: int | None,
           ttl_min: int, *, auto_approved: bool = False) -> tuple[str, str]:
    """(verdict, reason) for one posted entry. Pure — the whole guardrail
    stack in one testable place.

    verdicts: "expire" | "wait" | "reprice" | "execute"

    auto_approved substitutes ONLY for the human tap (caller has already
    verified auto-eligibility, live auto mode, and the auto caps); expiry,
    the fail-closed book check, and the reprice guard apply unchanged.
    """
    if take_queue.is_expired(entry, now_utc, ttl_min):
        return "expire", f"no approval within {ttl_min} min"
    if not auto_approved and not (reactor_ids & approver_ids):
        return "wait", "no allow-listed reaction yet"
    if live_px is None:
        # Book unreadable — fail closed on the order, retry inside the TTL.
        return "wait", "book unreadable — retrying"
    if entry["action"] == "buy" and live_px > entry["price_c"]:
        return "reprice", f"ask now {live_px}¢ > staged {entry['price_c']}¢"
    if entry["action"] == "sell" and live_px < entry["price_c"]:
        return "reprice", f"bid now {live_px}¢ < staged {entry['price_c']}¢"
    return "execute", "auto-approved (00Z class)" if auto_approved else "approved"


def premise_verdict(entry: dict, now_utc: datetime) -> tuple[str, str]:
    """('moved'|'clear'|'unchecked', reason): does the CLI print this entry
    was staged on still stand in the IEM archive?

    The fire-time layer of the reissue guard (2026-07-16 BOS: bogus min 51
    silently re-issued as 69 — a staged sell_dead on the live favorite must
    die here even when it staged before the re-issue existed anywhere).
    Lazy import keeps the approver importable without the sniper's module
    graph; ANY failure passes open — the guard only removes, and the human
    tap (or the shadow-graded auto class) already authorized the order."""
    try:
        from cli_sniper import check_premise

        return check_premise(entry, now_utc)
    except Exception as exc:  # noqa: BLE001 — fail open at money time
        logger.warning(f"{entry.get('ticker')}: premise check failed: {exc}")
        return "unchecked", f"premise check failed: {exc}"


async def retract_buttons(entries: list[dict], reason: str) -> None:
    """Edit already-posted TAKE? prompts for superseded entries so a stale
    button can't invite a tap. The queue status already blocks execution
    (claim_for_execution refuses non-posted entries) — this is the
    human-facing half. Called by the snipers after supersede_entries."""
    cfg = _config()
    posted = [e for e in entries if e.get("message_id")]
    if cfg is None or not posted:
        return
    import aiohttp

    ttl = take_queue.ttl_minutes()
    async with aiohttp.ClientSession() as session:
        for e in posted:
            await edit_message(session, cfg, e["message_id"],
                               f"🛑 RETRACTED — {reason}\n"
                               f"~~{format_prompt(e, ttl)}~~")


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
    # @mention the approvers: Discord mobile only pushes on mentions by
    # default, and a button nobody's phone buzzes for expires unseen
    # (2026-07-14: two full windows of buttons died untapped that way).
    approvers = sorted(cfg["approvers"])
    mentions = " ".join(f"<@{u}>" for u in approvers)
    msg = await _discord(session, "POST", f"/channels/{cfg['channel']}/messages",
                         cfg["token"],
                         {"content": f"{mentions} {format_prompt(entry, ttl_min)}",
                          "allowed_mentions": {"parse": [], "users": approvers}})
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


def try_run_lock():
    """The single-instance lock, non-blocking: an open fd (hold it until
    done) or None if another instance is mid-run."""
    import fcntl

    fd = (PROJECT_ROOT / ".take_approver.lock").open("w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        fd.close()
        return None


async def post_new_entries() -> int:
    """Post pending queue entries to Discord NOW — called by the snipers
    right after staging so the button + phone push go out at detection
    time, not up to 60s later at the next cron tick (2026-07-14: the DC
    floor race repriced inside that gap). Returns buttons posted.

    Takes the approver run lock non-blocking: if the cron instance holds
    it, skip — it (or the next tick) posts the entry; never double-post."""
    cfg = _config()
    if cfg is None:
        return 0
    lock_fd = try_run_lock()
    if lock_fd is None:
        return 0
    try:
        import aiohttp

        now_utc = datetime.now(timezone.utc)
        ttl = take_queue.ttl_minutes()
        queue = take_queue.load_queue()
        pending = [e for e in queue["entries"].values()
                   if e.get("status") == "pending"
                   and not take_queue.is_expired(e, now_utc, ttl)]
        if not pending:
            return 0
        mutations: dict[str, dict] = {}
        async with aiohttp.ClientSession() as session:
            for entry in sorted(pending, key=lambda e: e.get("ts", "")):
                mid = await post_prompt(session, cfg, entry, ttl)
                if mid:
                    mutations[entry["id"]] = {
                        "status": "posted", "message_id": mid,
                        "posted_ts": now_utc.isoformat(timespec="seconds")}
                    logger.info(f"{entry['ticker']}: TAKE? posted at stage time")
        if mutations:
            take_queue.update_entries(mutations, now_utc)
        return len(mutations)
    finally:
        lock_fd.close()


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


async def run(dry_run: bool, quiet_idle: bool = False) -> bool:
    """One decision pass. Returns True while entries are still active —
    the caller uses it to keep fast-polling inside the cron minute."""
    cfg = _config()
    if cfg is None:
        logger.info("take approver: not configured (DISCORD_BOT_TOKEN / "
                    "DISCORD_TAKE_CHANNEL_ID / DISCORD_TAKE_APPROVER_IDS) — idle")
        return False

    import aiohttp

    now_utc = datetime.now(timezone.utc)
    ttl = take_queue.ttl_minutes()
    queue = take_queue.load_queue()
    entries = [e for e in queue["entries"].values()
               if e.get("status") in take_queue.ACTIVE_STATUSES]
    if not entries:
        if not quiet_idle:
            logger.info("take approver: queue empty")
        return False

    mutations: dict[str, dict] = {}
    kalshi = None

    def _mut(eid: str, **fields) -> None:
        # Merge, never replace — an auto_shadow stamp and a status change
        # can land on the same entry in one run.
        mutations.setdefault(eid, {}).update(fields)

    async def ensure_kalshi():
        nonlocal kalshi
        if kalshi is None:
            from kalshi_client import KalshiClient

            kalshi = KalshiClient(
                api_key_id=os.getenv("KALSHI_API_KEY_ID", ""),
                private_key_path=os.getenv("KALSHI_PRIVATE_KEY_PATH", ""),
                demo_mode=False)
            await kalshi.start()
        return kalshi

    async with aiohttp.ClientSession() as session:
        for entry in sorted(entries, key=lambda e: e.get("ts", "")):
            eid = entry["id"]

            if entry["status"] == "pending":
                if take_queue.is_expired(entry, now_utc, ttl):
                    _mut(eid, status="expired",
                         resolved_ts=now_utc.isoformat(timespec="seconds"))
                    continue
                if dry_run:
                    print(f"would post: {format_prompt(entry, ttl)}")
                    continue
                mid = await post_prompt(session, cfg, entry, ttl)
                if mid:
                    _mut(eid, status="posted", message_id=mid,
                         posted_ts=now_utc.isoformat(timespec="seconds"))
                    logger.info(f"{entry['ticker']}: TAKE? posted ({eid})")
                continue

            # status == "posted"
            reactors = None if dry_run else await get_reactors(
                session, cfg, entry["message_id"])

            auto_entry = bool(entry.get("auto_eligible"))
            caps_ok, caps_reason = (auto_allowance(entry, queue["entries"], now_utc)
                                    if auto_entry else (False, "not auto-eligible"))
            auto_fire = auto_entry and cfg["auto_mode"] == "on" and caps_ok
            shadow_due = (auto_entry and cfg["auto_mode"] != "on"
                          and not entry.get("auto_shadow") and not dry_run)
            if auto_entry and cfg["auto_mode"] == "on" and not caps_ok:
                logger.info(f"{entry['ticker']}: auto-fire blocked — {caps_reason}"
                            " (button still live)")

            live_px = None
            tapped = bool(reactors and (reactors & cfg["approvers"]))
            if not dry_run and (tapped or auto_fire or shadow_due):
                live_px = await fetch_live_px(await ensure_kalshi(), entry)

            if shadow_due:
                if live_px is None:
                    # Book unreadable — leave the shadow unstamped so the
                    # next tick retries inside the TTL.
                    logger.info(f"{entry['ticker']}: auto-shadow deferred — "
                                f"book unreadable")
                else:
                    w_verdict, w_reason = decide(
                        entry, now_utc, set(), cfg["approvers"], live_px, ttl,
                        auto_approved=True)
                    _journal_shadow(shadow_record(
                        entry, live_px, w_verdict, w_reason,
                        caps_ok, caps_reason, now_utc))
                    _mut(eid, auto_shadow=now_utc.isoformat(timespec="seconds"))
                    logger.info(f"{entry['ticker']}: auto-shadow would_{w_verdict}"
                                f" ({w_reason}; caps: {caps_reason})")

            verdict, reason = decide(entry, now_utc, reactors or set(),
                                     cfg["approvers"], live_px, ttl,
                                     auto_approved=auto_fire)

            if verdict == "wait":
                continue
            if verdict == "expire":
                _mut(eid, status="expired",
                     resolved_ts=now_utc.isoformat(timespec="seconds"))
                if not dry_run and entry.get("message_id"):
                    await edit_message(session, cfg, entry["message_id"],
                                       f"⌛ EXPIRED — {reason}\n~~{format_prompt(entry, ttl)}~~")
                continue
            if verdict == "reprice":
                _mut(eid, status="repriced", result=reason,
                     resolved_ts=now_utc.isoformat(timespec="seconds"))
                if not dry_run:
                    await edit_message(session, cfg, entry["message_id"],
                                       f"↕ NOT EXECUTED — {reason}\n"
                                       f"~~{format_prompt(entry, ttl)}~~")
                logger.info(f"{entry['ticker']}: repriced — {reason}")
                continue

            # verdict == "execute" — the portfolio-day cap gates EVERY fire
            # (manual tap and auto alike; staging already enforced it, so a
            # breach here means env/bankroll moved mid-flight — refuse).
            day_ok, day_reason = daily_allowance(entry, queue["entries"],
                                                 now_utc)
            if not day_ok:
                if dry_run:
                    print(f"would cap: {entry['ticker']} — {day_reason}")
                    continue
                _mut(eid, status="capped", result=day_reason,
                     resolved_ts=now_utc.isoformat(timespec="seconds"))
                await edit_message(session, cfg, entry["message_id"],
                                   f"🧢 NOT EXECUTED — {day_reason}\n"
                                   f"~~{format_prompt(entry, ttl)}~~")
                logger.info(f"{entry['ticker']}: daily cap — {day_reason}")
                continue

            if dry_run:
                print(f"would execute: {' '.join(build_take_argv(entry))}")
                continue
            # Fire-time reissue guard: entries staged from a CLI print carry
            # its premise — re-check the archive so a silently re-issued
            # value can never be traded (fail open on archive refusal).
            if entry.get("premise"):
                p_verdict, p_reason = await asyncio.to_thread(
                    premise_verdict, entry, now_utc)
                if p_verdict == "moved":
                    _mut(eid, status="superseded", result=p_reason,
                         resolved_ts=now_utc.isoformat(timespec="seconds"))
                    await edit_message(session, cfg, entry["message_id"],
                                       f"🛑 NOT EXECUTED — {p_reason}\n"
                                       f"~~{format_prompt(entry, ttl)}~~")
                    logger.warning(f"{entry['ticker']}: premise moved — "
                                   f"{p_reason}")
                    continue
            # Persist the marker BEFORE the order so a crash mid-subprocess
            # can never double-fire (at-most-once). The claim is atomic on
            # status=="posted": a supersede that landed after our snapshot
            # wins, and the order is never placed.
            marker = {"status": "executing", "auto_fired": auto_fire,
                      "resolved_ts": now_utc.isoformat(timespec="seconds")}
            if not take_queue.claim_for_execution(eid, auto_fire, now_utc):
                logger.warning(f"{entry['ticker']}: not fired — entry was "
                               f"resolved elsewhere between snapshot and claim")
                continue
            # Same dict object as queue["entries"][eid] — keeps this run's
            # auto_allowance arithmetic honest for later entries.
            entry.update(marker)
            ok, output = run_take(entry)
            _mut(eid, status="executed" if ok else "failed", result=output,
                 auto_fired=auto_fire,
                 resolved_ts=now_utc.isoformat(timespec="seconds"))
            logger.info(f"{entry['ticker']}: take.py "
                        f"{'ok' if ok else 'FAILED'}"
                        f"{' (auto-fired)' if auto_fire else ''}")
            header = ("🤖 AUTO-FIRED" if auto_fire else "✅ EXECUTED") if ok \
                else f"❌ {'AUTO-' if auto_fire else ''}FAILED"
            await edit_message(
                session, cfg, entry["message_id"],
                f"{header} `{entry['ticker']}` "
                f"{entry['action']} {entry['count']}× @ {entry['price_c']}¢\n"
                f"```\n{output[-800:]}\n```")

    if kalshi is not None:
        await kalshi.stop()
    if mutations and not dry_run:
        take_queue.update_entries(mutations, now_utc)
    # Anything unresolved this pass is worth another look in POLL_INTERVAL_S.
    return any(mutations.get(e["id"], {}).get("status", e["status"])
               in take_queue.ACTIVE_STATUSES for e in entries)


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
    import time

    lock_fd = try_run_lock()
    if lock_fd is None:
        logger.info("take approver: previous run still active — skipping")
        return
    try:
        # Fast-poll while entries are active so a tap fires in ≤POLL_INTERVAL_S
        # instead of waiting for the next cron minute; exit as soon as the
        # queue idles (one-shot semantics preserved).
        deadline = time.monotonic() + POLL_WINDOW_S
        first = True
        while True:
            active = asyncio.run(run(args.dry_run, quiet_idle=not first))
            first = False
            if args.dry_run or not active or time.monotonic() >= deadline:
                break
            time.sleep(POLL_INTERVAL_S)
        write_heartbeat("take_approver")
    finally:
        lock_fd.close()


if __name__ == "__main__":
    main()
