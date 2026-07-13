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

from core.io import atomic_write_json
from log_setup import get_logger
from scripts.take import order_cost_dollars

logger = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
QUEUE_FILE = PROJECT_ROOT / "take_queue.json"
QUEUE_LOCK = PROJECT_ROOT / ".take_queue.lock"

DEFAULT_TTL_MIN = 15
DEFAULT_MAX_NOTIONAL = 50.0      # mirrors scripts/take.py's cap
PRUNE_TERMINAL_AFTER_H = 48

# Entry lifecycle. "executing" is the crash-safe marker persisted BEFORE the
# order subprocess runs — an entry found in that state is never retried
# (at-most-once), only surfaced for a manual fills check.
ACTIVE_STATUSES = ("pending", "posted")
TERMINAL_STATUSES = ("executed", "expired", "repriced", "failed", "executing")
ENQUEUEABLE_KINDS = ("buy_winner", "sell_dead")


def ttl_minutes() -> int:
    try:
        return int(os.getenv("TAKE_APPROVE_TTL_MIN", DEFAULT_TTL_MIN))
    except ValueError:
        return DEFAULT_TTL_MIN


def max_notional() -> float:
    try:
        return float(os.getenv("TAKE_MAX_NOTIONAL", DEFAULT_MAX_NOTIONAL))
    except ValueError:
        return DEFAULT_MAX_NOTIONAL


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


def clamp_count(action: str, side: str, count: int, price_c: int,
                cap_dollars: float) -> int:
    """Largest count ≤ `count` whose worst-case collateral fits the cap.

    Same money math as take.py's validate() — the alert sizes to full book
    depth (60k×1¢ observed 2026-07-12), the staged order sizes to the cap.
    take.py re-validates as the final backstop.
    """
    per_contract = order_cost_dollars(action, side, 1, price_c)
    if per_contract <= 0:
        return 0
    return min(count, int(cap_dollars / per_contract))


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
    """
    if (finding.get("kind") not in ENQUEUEABLE_KINDS or finding.get("suppressed")
            or finding.get("obs_kill") or finding.get("obs_warn")
            or finding.get("wall_ask")):
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
    return {"id": f"{source}:{parsed['ticker']}:{ts}",
            "ts": ts, "source": source, "kind": finding["kind"],
            **parsed, "count": count,
            "summary": " · ".join(b for b in summary_bits if b),
            "status": "pending", "message_id": None,
            "posted_ts": None, "resolved_ts": None, "result": None}


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
    with _locked():
        queue = _load_unlocked()
        entries = queue["entries"]
        active_tickers = {e["ticker"] for e in entries.values()
                          if e.get("status") in ACTIVE_STATUSES}
        for entry in staged:
            if entry["ticker"] in active_tickers:
                continue
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


def is_expired(entry: dict, now_utc: datetime, ttl_min: int | None = None) -> bool:
    ttl = ttl_min if ttl_min is not None else ttl_minutes()
    try:
        age = now_utc - datetime.fromisoformat(entry["ts"])
    except (KeyError, ValueError):
        return True  # unparseable birth time: never executable
    return age > timedelta(minutes=ttl)
