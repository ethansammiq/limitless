#!/usr/bin/env python3
"""FILLS WATCH — read-only readout of paper order/fill activity.

Watch the moment a cron-placed order crosses the real book and FILLS. Until the
2026-06-17 orderbook_fp fix, every order rested at 1c into a phantom-empty book
(0/100) and the EXECUTED count was stuck at 0; this surfaces the first real fill
the instant it lands.

Reads only — never places, cancels, or mutates state.

  .venv/bin/python3 fills_watch.py            # one snapshot
  .venv/bin/python3 fills_watch.py --watch     # refresh every 20s, bell on new fill
  .venv/bin/python3 fills_watch.py --watch 5   # refresh every 5s
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()  # paper paths are static, but keep parity with the rest of the bot

from config import (
    PAPER_BALANCE_FILE,
    PAPER_ORDERS_FILE,
    PAPER_POSITIONS_FILE,
    PAPER_INITIAL_BALANCE,
    PAPER_FILL_MODE,
    PAPER_TRADING_MODE,
)

ET = ZoneInfo("America/New_York")
AUTO_TRADER_HOURS = (6, 8, 10, 15, 16, 23)  # crontab auto_trader.py windows (ET)

# ── ANSI (suppressed when not a TTY, e.g. piped to a log) ──
_TTY = sys.stdout.isatty()
BOLD = "\033[1m" if _TTY else ""
DIM = "\033[2m" if _TTY else ""
RESET = "\033[0m" if _TTY else ""
GREEN = "\033[32m" if _TTY else ""
YELLOW = "\033[33m" if _TTY else ""
RED = "\033[31m" if _TTY else ""
CYAN = "\033[36m" if _TTY else ""


def _load(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return default


def _is_real(o: dict) -> bool:
    """Real market order (KX ticker), not a synthetic test/freeroll artifact."""
    return str(o.get("ticker", "")).startswith("KX")


def _fmt_ts(raw: str) -> str:
    if not raw:
        return "—"
    try:
        return datetime.fromisoformat(raw).astimezone(ET).strftime("%m-%d %H:%M:%S")
    except ValueError:
        return str(raw)[:19]


def _next_auto_trader(now: datetime) -> str:
    for h in AUTO_TRADER_HOURS:
        if h > now.hour or (h == now.hour and now.minute == 0):
            return f"{h:02d}:00 ET (in {_delta(now, h)})"
    return f"{AUTO_TRADER_HOURS[0]:02d}:00 ET tomorrow"


def _delta(now: datetime, hour: int) -> str:
    mins = (hour - now.hour) * 60 - now.minute
    return f"{mins // 60}h{mins % 60:02d}m" if mins >= 60 else f"{mins}m"


def strategy_pnl(positions: list[dict]) -> dict[str, dict]:
    """Aggregate realized P&L by the strategy that opened each position.

    Legacy records predate the strategy field and group under "untagged".
    Cancelled/rejected entries never held contracts, so they're excluded.
    """
    NEVER_HELD = ("cancelled", "canceled", "rejected")
    out: dict[str, dict] = {}
    for p in positions:
        if not _is_real(p) or p.get("status") in NEVER_HELD:
            continue
        strat = p.get("strategy") or "untagged"
        row = out.setdefault(strat, {"positions": 0, "open": 0, "pnl": 0.0})
        row["positions"] += 1
        if p.get("status") in ("open", "resting", "pending_sell"):
            row["open"] += 1
        row["pnl"] += float(p.get("pnl_realized") or 0.0)
    return out


def snapshot() -> tuple[str, int]:
    """Render the readout. Returns (text, executed_count) for new-fill detection."""
    now = datetime.now(ET)
    bal = _load(PAPER_BALANCE_FILE, {})
    orders = _load(PAPER_ORDERS_FILE, [])
    positions = _load(PAPER_POSITIONS_FILE, [])

    balance = float(bal.get("balance", PAPER_INITIAL_BALANCE))
    initial = float(bal.get("initial_balance", PAPER_INITIAL_BALANCE))
    delta = balance - initial

    real = [o for o in orders if _is_real(o)]
    counts: dict[str, int] = {}
    for o in real:
        counts[o.get("status", "?")] = counts.get(o.get("status", "?"), 0) + 1

    # Positions are the source of truth for fills: a real market fill shows up as an
    # open/closed position at a real price, and the position store can LEAD the order
    # ledger. (Instant-crossing fills written before the 2026-07-01 broker fix never
    # reached paper_orders.json — the DEN-T88 case.) Unify EXECUTED orders with filled
    # positions, deduped by order_id, so a fill is never missed just because the order
    # ledger lagged.
    fill_events: dict[str, dict] = {}
    for o in (x for x in real if x.get("status") == "EXECUTED"):
        key = o.get("order_id") or f"ord:{o.get('ticker')}:{o.get('filled_at')}"
        fill_events[key] = {
            "ts": o.get("filled_at") or o.get("created_at"),
            "side": o.get("side", ""), "qty": o.get("filled_count", o.get("count", 0)),
            "ticker": o.get("ticker", ""), "price": o.get("filled_price", o.get("price", 0)),
        }
    # A filled position can be open/closed/pending_sell/freerolled/... — anything
    # except the never-filled states. Denylist is more robust than an allowlist.
    NOT_FILLED = ("resting", "cancelled", "canceled", "rejected")
    for p in positions:
        if not (_is_real(p) and p.get("status") not in NOT_FILLED
                and (p.get("avg_price") or 0) > 1):   # >1c excludes 1c placeholders + synthetic
            continue
        key = p.get("order_id") or f"pos:{p.get('ticker')}"
        fill_events.setdefault(key, {
            "ts": p.get("entry_time"), "side": p.get("side", ""),
            "qty": p.get("contracts", 0), "ticker": p.get("ticker", ""),
            "price": p.get("avg_price", 0),
        })
    fills = sorted(fill_events.values(), key=lambda f: f.get("ts") or "", reverse=True)

    L = []
    L.append(f"{BOLD}{CYAN}═══ WEATHER EDGE · FILLS WATCH ═══{RESET}  {DIM}{now:%Y-%m-%d %H:%M:%S ET}{RESET}")
    mode = f"{GREEN}PAPER{RESET}" if PAPER_TRADING_MODE else f"{RED}LIVE{RESET}"
    L.append(f"  mode={mode}  fill_mode={PAPER_FILL_MODE}  next auto_trader: {YELLOW}{_next_auto_trader(now)}{RESET}")
    dcol = GREEN if delta > 0 else (RED if delta < 0 else DIM)
    L.append(f"  balance ${balance:,.2f}   Δ {dcol}{delta:+.2f}{RESET}   initial ${initial:,.0f}")
    L.append("")

    # Order status line — the headline that has been stuck at 0 EXECUTED
    parts = []
    for st in ("EXECUTED", "RESTING", "CANCELED", "REJECTED"):
        n = counts.get(st, 0)
        col = GREEN if (st == "EXECUTED" and n) else (DIM if not n else "")
        parts.append(f"{col}{st.title()}:{n}{RESET}")
    L.append(f"  {BOLD}Real orders ({len(real)}){RESET}  " + "  ".join(parts))

    if fills:
        L.append(f"  {BOLD}{GREEN}★ {len(fills)} FILL(S) — real positions on the live book "
                 f"(tracked via positions; order ledger may show Executed:0 if it lagged).{RESET}")
    else:
        L.append(f"  {DIM}No fills yet. First fill will come from the next auto_trader window above.{RESET}")
    L.append("")

    # Recent fills (unified from EXECUTED orders + filled positions)
    L.append(f"{BOLD}FILLS{RESET} {DIM}(most recent first){RESET}")
    if fills:
        for f in fills[:8]:
            px, qty = f.get("price", 0), f.get("qty", 0)
            cost = qty * px / 100
            L.append(f"  {GREEN}●{RESET} {_fmt_ts(f.get('ts'))}  "
                     f"{f.get('side', '').upper():3} {qty:>3}x {f.get('ticker', ''):26} @ {px:>2}c  ${cost:.2f}")
    else:
        L.append(f"  {DIM}—{RESET}")
    L.append("")

    # Recent order activity — placement book is now the REAL book, not 0/100
    L.append(f"{BOLD}RECENT ORDERS{RESET} {DIM}(book@placement should now show real bid/ask, not 0/100){RESET}")
    recent = sorted(real, key=lambda x: x.get("created_at") or "", reverse=True)[:8]
    if recent:
        for o in recent:
            st = o.get("status", "?")
            col = GREEN if st == "EXECUTED" else (YELLOW if st == "RESTING" else DIM)
            bb, ba = o.get("book_bid_at_placement"), o.get("book_ask_at_placement")
            book = f"{bb}/{ba}"
            flag = f" {RED}⚠ empty book{RESET}" if (bb == 0 and ba == 100) else ""
            L.append(f"  {col}{st:9}{RESET} {_fmt_ts(o.get('created_at'))}  {o.get('side','').upper():3} "
                     f"{o.get('count',0):>3}x {o.get('ticker',''):26} @ {o.get('price',0):>2}c  book {book}{flag}")
    else:
        L.append(f"  {DIM}—{RESET}")
    L.append("")

    # Live positions (held or in-flight — everything except terminal cancelled/rejected)
    live = [p for p in positions
            if p.get("status") not in ("cancelled", "canceled", "rejected") and _is_real(p)]
    L.append(f"{BOLD}POSITIONS{RESET} {DIM}(held / in-flight){RESET}")
    if live:
        for p in sorted(live, key=lambda x: x.get("entry_time") or "", reverse=True)[:10]:
            st = p.get("status", "?")
            col = GREEN if st == "open" else (RED if st == "pending_sell" else YELLOW)
            L.append(f"  {col}{st:8}{RESET} {p.get('side','').upper():3} {p.get('contracts',0):>3}x "
                     f"{p.get('ticker',''):26} @ {p.get('avg_price',0):>2}c  pnl ${p.get('pnl_realized',0):.2f}")
    else:
        L.append(f"  {DIM}—{RESET}")
    L.append("")

    # Realized P&L attributed to the subsystem that opened each position
    L.append(f"{BOLD}P&L BY STRATEGY{RESET} {DIM}(Σ realized; pre-tagging records = untagged){RESET}")
    by_strat = strategy_pnl(positions)
    if by_strat:
        for strat, row in sorted(by_strat.items(), key=lambda kv: kv[1]["pnl"], reverse=True):
            pcol = GREEN if row["pnl"] > 0 else (RED if row["pnl"] < 0 else DIM)
            L.append(f"  {strat:12} {row['positions']:>3} pos ({row['open']} open)  "
                     f"{pcol}${row['pnl']:+.2f}{RESET}")
    else:
        L.append(f"  {DIM}—{RESET}")

    return "\n".join(L), len(fills)


def main():
    args = sys.argv[1:]
    if not args or args[0] not in ("--watch", "-w"):
        text, _ = snapshot()
        print(text)
        return

    interval = 20
    if len(args) > 1:
        try:
            interval = max(2, int(args[1]))
        except ValueError:
            pass

    import time
    last_fills = -1
    try:
        while True:
            text, n_fills = snapshot()
            print("\033[2J\033[H", end="")  # clear + home
            print(text)
            if last_fills >= 0 and n_fills > last_fills:
                print(f"\n{BOLD}{GREEN}🔔 NEW FILL(S): {n_fills - last_fills} just landed!{RESET}\a")
            last_fills = n_fills
            print(f"\n{DIM}refreshing every {interval}s · Ctrl-C to stop{RESET}")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
