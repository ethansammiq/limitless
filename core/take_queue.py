"""TAKE QUEUE — staged one-tap approvals bridging sniper alerts to take.py.

Snipers enqueue every alerted take.py command here (alongside, not instead
of, the Discord alert). take_approver.py (cron */1) posts each entry to
Discord with a ✅ reaction; a tap from an allow-listed user executes the
EXACT staged command through scripts/take.py — which stays the only
order-placing entry point. No tap, no order: entries expire after
TAKE_APPROVE_TTL_MIN (default 15, sized from the measured ~10-15 min
fillable window on 2026-07-12) and the queue is inert without the
approver's bot token.

Two writers share the queue file (snipers enqueue, the approver
transitions), so every read-modify-write happens under an fcntl lock and
the approver merges per-entry mutations instead of saving its whole stale
snapshot — a sniper's enqueue between the approver's load and save must
never be clobbered.
"""
from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import risk
from core.io import atomic_write_json
from core.risk import clamp_count, order_cost_dollars  # noqa: F401 — shared money math
from log_setup import get_logger

logger = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
QUEUE_FILE = PROJECT_ROOT / "take_queue.json"
QUEUE_LOCK = PROJECT_ROOT / ".take_queue.lock"

DEFAULT_TTL_MIN = 15
MAX_STAGE_ASK_C = risk.MAX_ENTRY_ASK_C
PRUNE_TERMINAL_AFTER_H = 48

# Entry lifecycle. "executing" is the crash-safe marker persisted BEFORE the
# order subprocess runs — an entry found in that state is never retried
# (at-most-once), only surfaced for a manual fills check. "capped" is a
# fire refused by the portfolio-day cap — terminal, but it spent nothing
# (kept out of NIGHT_LEDGER_STATUSES below). "superseded" is a premise
# killed by a CLI reissue (2026-07-16 BOS: bogus min 51 re-issued as 69
# with no CORRECTED tag) — terminal, spent nothing, button retracted.
ACTIVE_STATUSES = ("pending", "posted")
TERMINAL_STATUSES = ("executed", "expired", "repriced", "failed", "executing",
                     "capped", "superseded")
ENQUEUEABLE_KINDS = ("buy_winner", "sell_dead")

# The only class pre-cleared for auto-execution (shadow-graded first): a
# METAR high-ladder buy_winner from the 00Z synoptic group — the one anchor
# at which all four of the day's 6-hr groups exist (day-max == final CLI
# 98.4%, 815/828). Earlier anchors carry post-window warming risk: the
# 2026-07-13 18Z batch would have gone 1-for-5 against the finals.
AUTO_ANCHOR_UTC = 0


def is_auto_eligible(finding: dict, source: str) -> bool:
    """Does this finding fall in the pre-cleared auto-take class?

    Trap flags (obs_kill/obs_warn/wall_ask) never reach here — they block
    staging entirely in entry_from_finding.
    """
    return (source == "metar_sniper"
            and finding.get("kind") == "buy_winner"
            and finding.get("ladder_kind") == "high"
            and finding.get("synoptic_anchor_utc") == AUTO_ANCHOR_UTC)


def stageable_class(finding: dict, source: str) -> bool:
    """Only classes with ≥95% measured base rates get a button; everything
    else stays alert-only. The raw feed grades 52% (scorecard 2026-07-14)
    — SELECTION is the edge, and it must be mechanical, not discipline:
      - sell_dead: obs-certain by construction
      - CLI floor buys: floor-at-bottom drift class (≥.95), or within the
        20¢ standing entry cap (cheap asymmetric floor-at-top stays legal)
      - METAR buys: the 00Z anchor only (day-max == final 98.4%) — the
        11:53/17:53 groups are forecasts, not settlements (the 18Z batch
        graded 1-for-5 on 2026-07-13; a full day of 7/14 morning buttons
        graded as warming traps)
    """
    if finding.get("kind") == "sell_dead":
        return True
    if source == "metar_sniper":
        return is_auto_eligible(finding, source)
    if finding.get("drift_prob", 0.0) >= 0.95:
        return True
    ask = finding.get("ask")
    return ask is not None and ask <= MAX_STAGE_ASK_C


def ttl_minutes() -> int:
    try:
        return int(os.getenv("TAKE_APPROVE_TTL_MIN", DEFAULT_TTL_MIN))
    except ValueError:
        return DEFAULT_TTL_MIN


def max_notional() -> float:
    """Bankroll-derived (never above the fixed $50) unless TAKE_MAX_NOTIONAL
    overrides — see core/risk.py."""
    return risk.max_notional_dollars()


def night_cap_dollars() -> float:
    """Bankroll-derived (never above the fixed $25) unless
    TAKE_NIGHT_CAP_DOLLARS overrides — see core/risk.py."""
    return risk.night_cap_dollars()


def event_key(ticker: str) -> str:
    """One cap bucket per STATION-night: a city's high AND low ladders
    share it, matching the scorecard's cluster unit (awips, summary_date).
    The v1 series-date key counted them separately ("documented not
    accidental" — but sell_dead stages on low ladders at complement
    collateral and final CLI low buys ≤20¢ stage too, so one station-night
    could absorb 2× the cap; retired 2026-07-16). Unknown series falls
    back to the v1 key."""
    return risk.station_night_key(ticker)


# Statuses that hold or have spent money against the night budget. Expired,
# repriced and failed entries release theirs.
NIGHT_LEDGER_STATUSES = ("pending", "posted", "executing", "executed")


def night_spent_dollars(entries: dict, ticker: str) -> float:
    """Worst-case dollars already committed to this ticker's station-night."""
    key = event_key(ticker)
    return sum(order_cost_dollars(e["action"], e["side"], e["count"], e["price_c"])
               for e in entries.values()
               if e.get("ticker") and event_key(e["ticker"]) == key
               and e.get("status") in NIGHT_LEDGER_STATUSES)


def day_spent_dollars(entries: dict, now_utc: datetime,
                      exclude_id: str | None = None) -> float:
    """Worst-case dollars committed across ALL stations this UTC day.

    Staged (pending/posted) counts as committed — a button offered is money
    promised; executing counts as spent (a crash mid-order is money out
    until fills reconcile); expired/repriced/failed/capped release theirs.
    `exclude_id` lets the approver's fire-time check leave the candidate
    itself out of the sum (it is already staged in the queue)."""
    today = now_utc.date().isoformat()
    return sum(order_cost_dollars(e["action"], e["side"], e["count"], e["price_c"])
               for eid, e in entries.items()
               if eid != exclude_id
               and e.get("status") in NIGHT_LEDGER_STATUSES
               and (e.get("ts") or "").startswith(today))


def parse_take_cmd(cmd: str) -> dict | None:
    """Structured fields from the snipers' own take.py command string.

    The format is ours (`_take_cmd` in both snipers): anything that doesn't
    parse exactly is rejected — the queue must never stage a command shape
    the snipers didn't emit.
    """
    tokens = (cmd or "").split()
    try:
        idx = next(i for i, t in enumerate(tokens) if t.endswith("take.py"))
        ticker, action, side, count, price_c = tokens[idx + 1:idx + 6]
        if tokens[idx + 6:]:
            return None  # trailing flags are not part of the emitted shape
        if action not in ("buy", "sell") or side not in ("yes", "no"):
            return None
        count_i, price_i = int(count), int(price_c)
        if count_i < 1 or not (1 <= price_i <= 99):
            return None
        return {"ticker": ticker, "action": action, "side": side,
                "count": count_i, "price_c": price_i}
    except (StopIteration, IndexError, ValueError):
        return None


def _load_unlocked() -> dict:
    if QUEUE_FILE.exists():
        try:
            data = json.loads(QUEUE_FILE.read_text())
            if isinstance(data.get("entries"), dict):
                return data
        except (json.JSONDecodeError, OSError):
            logger.warning("take queue unreadable — starting empty")
    return {"entries": {}}


@contextmanager
def _locked():
    with QUEUE_LOCK.open("w") as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)


def load_queue() -> dict:
    """Point-in-time snapshot (read under the lock, then released)."""
    with _locked():
        return _load_unlocked()


def _prune(entries: dict, now_utc: datetime) -> None:
    cutoff = (now_utc - timedelta(hours=PRUNE_TERMINAL_AFTER_H)).isoformat()
    for eid in [eid for eid, e in entries.items()
                if e.get("status") in TERMINAL_STATUSES
                and e.get("ts", "") < cutoff]:
        del entries[eid]


def entry_from_finding(finding: dict, source: str, now_utc: datetime) -> dict | None:
    """A queue entry from one alerted sniper finding, or None if not stageable.

    An obs_kill/obs_warn/wall_ask finding stays in the alert (the human may
    want the other side, or to verify) but a one-tap buy button on an
    observed-dead bracket — or INTO a certainty wall (5-0, never fade; the
    2026-07-13 CHI T87 5000×1¢ button was a fade dispenser) — is never staged.
    reissue_conflict joins the same family (2026-07-16 BOS: a silently
    re-issued CLI moved min 51→69 under a staged sell_dead on the live
    favorite): the print the finding is premised on no longer stands.
    """
    if (finding.get("kind") not in ENQUEUEABLE_KINDS or finding.get("suppressed")
            or finding.get("obs_kill") or finding.get("obs_warn")
            or finding.get("wall_ask") or finding.get("reissue_conflict")):
        return None
    if not stageable_class(finding, source):
        return None
    parsed = parse_take_cmd(finding.get("cmd", ""))
    if parsed is None:
        return None
    count = clamp_count(parsed["action"], parsed["side"], parsed["count"],
                        parsed["price_c"], max_notional())
    if count < 1:
        return None
    ts = now_utc.isoformat(timespec="seconds")
    summary_bits = [finding.get("subtitle") or "",
                    f"printed {finding['printed']}°" if finding.get("printed") is not None else ""]
    if "drift_prob" in finding:
        summary_bits.append(f"drift {finding['drift_prob']:.0%} "
                            f"EV {finding.get('drift_ev_c', 0):+.0f}¢")
    entry = {"id": f"{source}:{parsed['ticker']}:{ts}",
             "ts": ts, "source": source, "kind": finding["kind"],
             **parsed, "count": count,
             "summary": " · ".join(b for b in summary_bits if b),
             "auto_eligible": is_auto_eligible(finding, source),
             "status": "pending", "message_id": None,
             "posted_ts": None, "resolved_ts": None, "result": None}
    if finding.get("awips") and finding.get("stamp"):
        # The CLI print this button is premised on — the approver re-checks
        # it against the archive at fire time, and a later sniper run that
        # sees a reissue supersedes the entry through it (reissue guard).
        entry["premise"] = {
            "awips": finding["awips"], "stamp": finding["stamp"],
            "summary_date": finding.get("summary_date"),
            "printed": finding.get("printed"),
            "ladder_kind": finding.get("ladder_kind"),
            "final": bool(finding.get("final"))}
    return entry


def enqueue_findings(findings: list[dict], source: str,
                     now_utc: datetime | None = None) -> int:
    """Stage alerted findings for one-tap approval. Returns entries added.

    A ticker with an entry still active (pending/posted) is skipped — the
    snipers' own 48h alert dedup makes same-run duplicates impossible, and
    a re-alert while the first tap window is open must not double-stage.
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    staged = [e for f in findings
              if (e := entry_from_finding(f, source, now_utc)) is not None]
    if not staged:
        return 0
    added = 0
    night_cap, night_why = risk.night_cap_detail(now_utc)
    daily_cap, daily_why = risk.daily_cap_detail(now_utc)
    with _locked():
        queue = _load_unlocked()
        entries = queue["entries"]
        active_tickers = {e["ticker"] for e in entries.values()
                          if e.get("status") in ACTIVE_STATUSES}
        for entry in staged:
            if entry["ticker"] in active_tickers:
                continue
            # Two exposure caps, tighter one binds. Station-night: same-night
            # brackets are one correlated bet — three max-size losses on one
            # bankroll is the ruin path, not the 52%-vs-98% winrate spread
            # (2026-07-14: a single button offered 34% of the bankroll).
            # Portfolio-day: many station-nights can still stack; a button
            # past the day budget is never offered.
            night_left = night_cap - night_spent_dollars(
                entries, entry["ticker"])
            daily_left = daily_cap - day_spent_dollars(entries, now_utc)
            if night_left <= daily_left:
                bound, why = "station-night", night_why
            else:
                bound, why = "portfolio-day", daily_why
            capped = clamp_count(entry["action"], entry["side"],
                                 entry["count"], entry["price_c"],
                                 min(night_left, daily_left))
            if capped < 1:
                logger.info(f"{entry['ticker']}: {bound} cap reached "
                            f"({why}) — not staged")
                continue
            if capped < entry["count"]:
                logger.info(f"{entry['ticker']}: {bound} cap trims "
                            f"{entry['count']}→{capped} ({why})")
                entry["count"] = capped
            entries[entry["id"]] = entry
            active_tickers.add(entry["ticker"])
            added += 1
        _prune(entries, now_utc)
        atomic_write_json(QUEUE_FILE, queue, indent=1)
    return added


def update_entries(mutations: dict[str, dict],
                   now_utc: datetime | None = None) -> None:
    """Merge per-entry field updates into the live queue (approver's writer).

    Loads fresh under the lock and applies only the given fields, so entries
    enqueued after the caller's snapshot survive untouched.
    """
    if not mutations:
        return
    now_utc = now_utc or datetime.now(timezone.utc)
    with _locked():
        queue = _load_unlocked()
        for eid, fields in mutations.items():
            if eid in queue["entries"]:
                queue["entries"][eid].update(fields)
        _prune(queue["entries"], now_utc)
        atomic_write_json(QUEUE_FILE, queue, indent=1)


def supersede_entries(entry_ids: list[str], reason: str,
                      now_utc: datetime | None = None) -> list[dict]:
    """Terminal-mark still-active entries whose CLI premise died (reissue
    guard). Returns the superseded entries (with message_id) so the caller
    can retract their Discord buttons. Their night/day budget is released
    (superseded is not in NIGHT_LEDGER_STATUSES)."""
    now_utc = now_utc or datetime.now(timezone.utc)
    out = []
    with _locked():
        queue = _load_unlocked()
        for eid in entry_ids:
            e = queue["entries"].get(eid)
            if not e or e.get("status") not in ACTIVE_STATUSES:
                continue
            e.update(status="superseded", result=reason,
                     resolved_ts=now_utc.isoformat(timespec="seconds"))
            out.append(dict(e))
        if out:
            atomic_write_json(QUEUE_FILE, queue, indent=1)
    return out


def claim_for_execution(entry_id: str, auto_fired: bool = False,
                        now_utc: datetime | None = None) -> bool:
    """Atomically move a POSTED entry to the crash-safe "executing" marker.

    Status check and write happen under one lock, so a supersede (or any
    other resolution) landing between the approver's snapshot and its fire
    can never be overwritten — False means someone else resolved the entry
    first and the order must NOT be placed."""
    now_utc = now_utc or datetime.now(timezone.utc)
    with _locked():
        queue = _load_unlocked()
        e = queue["entries"].get(entry_id)
        if not e or e.get("status") != "posted":
            return False
        e.update(status="executing", auto_fired=auto_fired,
                 resolved_ts=now_utc.isoformat(timespec="seconds"))
        atomic_write_json(QUEUE_FILE, queue, indent=1)
    return True


def is_expired(entry: dict, now_utc: datetime, ttl_min: int | None = None) -> bool:
    ttl = ttl_min if ttl_min is not None else ttl_minutes()
    try:
        age = now_utc - datetime.fromisoformat(entry["ts"])
    except (KeyError, ValueError):
        return True  # unparseable birth time: never executable
    return age > timedelta(minutes=ttl)
