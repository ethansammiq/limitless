#!/usr/bin/env python3
"""TRADE AUTOPSY — reconstruct how & why one real trade won (or lost).

`postmortem_2026-01-20.md` dissected a LOSS by hand. `sniper_scorecard.py`
scores the sniper's ALERTS in aggregate. Neither answers the question you
actually ask after a fill settles: *for this one position I really held,
how and why did it work out?* This does.

It joins the three server-side records that already exist and never talked
to each other:

  logs/live_fills.jsonl   the fills — when you got in, at what price, taker
                          or maker (journaled by live_watch)
  logs/live_account.json  Kalshi's authoritative realized P&L per ticker
                          (the dashboard snapshot live_watch overwrites)
  logs/cli_sniper/*.jsonl the THESIS — the CLI-sniper finding that flagged
                          the bracket: what value the climate report printed,
                          FINAL vs floor, the ask when the alert fired

From those it builds the causal story: the CLI printed X°, that made your
bracket a (certain / leading) winner, the sniper saw it at N¢ before the
market repriced, you bought at avg C¢, it settled YES at 100¢, you captured
+Z¢/contract = $P. When no sniper finding matches (a discretionary trade),
it degrades to fills + settlement and says the thesis source was manual.

Settlement ('yes'/'no') comes from Kalshi's per-ticker market `result`;
without credentials it falls back to the sign of realized P&L in the
account snapshot, so the autopsy still runs offline.

READ-ONLY. Never trades, never writes journals.

Usage:
    python3 trade_autopsy.py                    # auto-pick your best recent win
    python3 trade_autopsy.py --ticker KXHIGHCHI-26JUL04-B85.5
    python3 trade_autopsy.py --loss             # auto-pick worst recent loss
    python3 trade_autopsy.py --report discord
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from dutch_book import kalshi_taker_fee_cents  # noqa: E402
from market_timeseries import extract_target_date_from_ticker  # noqa: E402

LOGS = PROJECT_ROOT / "logs"
FILLS_LOG = LOGS / "live_fills.jsonl"
ACCOUNT_FILE = LOGS / "live_account.json"
SNIPER_JOURNAL_DIR = LOGS / "cli_sniper"


# ── fills ────────────────────────────────────────────────────────────────
def load_fills(path: Path = FILLS_LOG) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def fill_price_cents(fill: dict) -> int | None:
    """YES price of a fill in whole cents, derived from whichever leg the
    journal carries (Kalshi records yes_price_dollars; no_price_dollars on
    the complement)."""
    y = fill.get("yes_price_dollars")
    if y is not None:
        try:
            return round(float(y) * 100)
        except (TypeError, ValueError):
            return None
    n = fill.get("no_price_dollars")
    if n is not None:
        try:
            return round((1 - float(n)) * 100)
        except (TypeError, ValueError):
            return None
    return None


def fill_count(fill: dict) -> float:
    try:
        return float(fill.get("count_fp") or 0)
    except (TypeError, ValueError):
        return 0.0


def leg_summary(fills: list[dict], action: str) -> dict:
    """Aggregate one side (buy/sell) of a ticker's fills: quantity, size-
    weighted avg YES price, taker share, and the fill-time span."""
    rows = [f for f in fills if f.get("action") == action]
    qty = sum(fill_count(f) for f in rows)
    notional = 0.0            # sum(price_c * count)
    taker_qty = 0.0
    stamps = []
    for f in rows:
        px, ct = fill_price_cents(f), fill_count(f)
        if px is not None:
            notional += px * ct
        if f.get("is_taker"):
            taker_qty += ct
        ts = f.get("created_time") or f.get("logged_at")
        if ts:
            stamps.append(ts)
    return {
        "n": len(rows),
        "qty": qty,
        "avg_cents": round(notional / qty, 2) if qty else None,
        "notional_cents": round(notional, 2),
        "taker_qty": taker_qty,
        "taker_share": round(taker_qty / qty, 3) if qty else 0.0,
        "first_ts": min(stamps) if stamps else None,
        "last_ts": max(stamps) if stamps else None,
    }


def reconstruct_trade(all_fills: list[dict], ticker: str) -> dict:
    """Fill-level story for one ticker: entries (buys) vs exits (sells),
    net contracts still held, cost basis and proceeds in dollars."""
    fills = [f for f in all_fills if f.get("ticker") == ticker]
    buys, sells = leg_summary(fills, "buy"), leg_summary(fills, "sell")
    net_qty = buys["qty"] - sells["qty"]
    return {
        "ticker": ticker,
        "n_fills": len(fills),
        "buys": buys,
        "sells": sells,
        "net_qty": net_qty,
        "cost_basis_dollars": round(buys["notional_cents"] / 100, 2),
        "proceeds_dollars": round(sells["notional_cents"] / 100, 2),
    }


# ── account snapshot ───────────────────────────────────────────────────────
def load_account(path: Path = ACCOUNT_FILE) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def account_positions(account: dict | None) -> list[dict]:
    if not account:
        return []
    return (account.get("open_positions") or []) + (account.get("closed_positions") or [])


def realized_for_ticker(account: dict | None, ticker: str) -> float | None:
    """Kalshi's realized P&L (dollars) for a ticker from the snapshot, or None
    if the ticker isn't in it."""
    for p in account_positions(account):
        if p.get("ticker") == ticker:
            try:
                return float(p.get("realized"))
            except (TypeError, ValueError):
                return None
    return None


def pick_ticker(account: dict | None, want_loss: bool = False) -> str | None:
    """The ticker with the largest realized win (or loss with --loss) in the
    snapshot. Ties broken by |P&L|; None when nothing has settled."""
    best_ticker, best_val = None, None
    for p in account_positions(account):
        try:
            val = float(p.get("realized"))
        except (TypeError, ValueError):
            continue
        if val == 0:
            continue
        if (want_loss and val < 0) or (not want_loss and val > 0):
            if best_val is None or abs(val) > abs(best_val):
                best_ticker, best_val = p.get("ticker"), val
    return best_ticker


# ── sniper thesis ──────────────────────────────────────────────────────────
def load_sniper_findings(journal_dir: Path = SNIPER_JOURNAL_DIR,
                         since: datetime | None = None) -> list[dict]:
    """Flatten the sniper journal to one dict per finding, carrying the parent
    product's timestamp and finality (the alert that flagged each ticker)."""
    out: list[dict] = []
    if not journal_dir.exists():
        return out
    for path in sorted(journal_dir.glob("*.jsonl")):
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = row.get("ts", "")
            if since is not None:
                try:
                    if datetime.fromisoformat(ts) < since:
                        continue
                except ValueError:
                    pass
            for f in row.get("findings") or []:
                out.append({"ts": ts, "awips": row.get("awips"),
                            "summary_date": row.get("summary_date"),
                            "is_final": bool(row.get("is_final")), **f})
    return out


def match_finding(findings: list[dict], ticker: str) -> dict | None:
    """The sniper alert that put us into this ticker: the earliest buy_winner
    for it (falling back to the earliest finding of any kind), so the story
    is anchored to when the edge was first visible, not the last confirmation."""
    hits = [f for f in findings if f.get("ticker") == ticker]
    if not hits:
        return None
    buys = [f for f in hits if f.get("kind") == "buy_winner"]
    pool = buys or hits
    return min(pool, key=lambda f: f.get("ts") or "")


# ── outcome + narrative ────────────────────────────────────────────────────
def classify_outcome(result: str | None, net_qty: float,
                     realized: float | None) -> str:
    """'win' | 'loss' | 'open' | 'flat' from settlement, else realized P&L."""
    if result == "yes":
        return "win"
    if result == "no":
        return "loss"
    if realized is not None and realized > 0:
        return "win"
    if realized is not None and realized < 0:
        return "loss"
    return "open" if net_qty > 0 else "flat"


def _ts_gap_minutes(a: str | None, b: str | None) -> float | None:
    if not a or not b:
        return None
    try:
        return round((datetime.fromisoformat(b) - datetime.fromisoformat(a))
                     .total_seconds() / 60, 1)
    except ValueError:
        return None


def build_autopsy(ticker: str, all_fills: list[dict], account: dict | None,
                  finding: dict | None, result: str | None) -> dict:
    """Everything the report needs, computed once. Pure over its inputs."""
    trade = reconstruct_trade(all_fills, ticker)
    realized = realized_for_ticker(account, ticker)
    outcome = classify_outcome(result, trade["net_qty"], realized)
    avg_entry = trade["buys"]["avg_cents"]

    # Edge economics on the entry: a YES buy that settles yes returns
    # 100¢ minus what you paid minus the taker fee you paid to get filled.
    edge = None
    if outcome == "win" and avg_entry is not None:
        fee = kalshi_taker_fee_cents(round(avg_entry)) if trade["buys"]["taker_share"] else 0
        edge = round(100 - avg_entry - fee, 2)

    # An estimated settlement P&L from the fills, to corroborate (or stand in
    # for) the account's realized number.
    est_pnl = None
    if result in ("yes", "no") and avg_entry is not None:
        payout = trade["net_qty"] * (100 if result == "yes" else 0)
        est_pnl = round((trade["proceeds_dollars"] * 100 + payout
                         - trade["buys"]["notional_cents"]) / 100, 2)

    thesis = None
    if finding:
        thesis = {
            "source": "cli_sniper",
            "kind": finding.get("kind"),
            "printed": finding.get("printed"),
            "final": finding.get("final"),
            "subtitle": finding.get("subtitle"),
            "ladder_kind": finding.get("ladder_kind"),
            "alert_ts": finding.get("ts"),
            "alert_ask": finding.get("ask"),
            "alert_ask_depth": finding.get("ask_depth"),
            "station": finding.get("awips"),
            "reaction_minutes": _ts_gap_minutes(
                finding.get("ts"), trade["buys"]["first_ts"]),
        }

    return {
        "ticker": ticker,
        "target_date": extract_target_date_from_ticker(ticker),
        "outcome": outcome,
        "settled": result,
        "realized_dollars": realized,
        "est_pnl_dollars": est_pnl,
        "edge_captured_cents": edge,
        "trade": trade,
        "thesis": thesis,
    }


def _fmt_money(v: float | None) -> str:
    return "n/a" if v is None else f"${v:+,.2f}"


def format_report(a: dict) -> str:
    t = a["trade"]
    buys, sells = t["buys"], t["sells"]
    verdict = {"win": "✅ WIN", "loss": "❌ LOSS",
               "open": "⏳ OPEN", "flat": "➖ FLAT"}.get(a["outcome"], a["outcome"])
    lines = [
        f"**Trade autopsy — {a['ticker']}**  {verdict}",
        f"settle date {a['target_date'] or '?'} · "
        f"result **{a['settled'] or 'unsettled'}** · "
        f"realized **{_fmt_money(a['realized_dollars'])}**"
        + (f" (est {_fmt_money(a['est_pnl_dollars'])})"
           if a["est_pnl_dollars"] is not None else ""),
    ]

    # WHY — the thesis that generated the trade.
    th = a["thesis"]
    lines.append("")
    lines.append("__Why (thesis)__")
    if th:
        drift = "warming" if th["ladder_kind"] == "high" else "cooling"
        cert = "FINAL — one bracket can win" if th["final"] \
            else f"floor — residual post-4PM {drift} risk"
        lines.append(
            f"  CLI at {th['station']} printed **{th['printed']}°** → "
            f"“{th['subtitle']}” was a **{th['kind']}** [{cert}].")
        if th["alert_ask"] is not None:
            lines.append(
                f"  Sniper flagged it at {th['alert_ask']}¢ "
                f"(×{th['alert_ask_depth']:.0f}) — {th['alert_ts']}.")
        if th["reaction_minutes"] is not None:
            lines.append(
                f"  You were filled ~{th['reaction_minutes']:.0f} min after the alert.")
    else:
        lines.append("  No CLI-sniper finding matched this ticker — "
                     "discretionary/manual entry; thesis not journaled.")

    # HOW — the execution.
    lines.append("")
    lines.append("__How (execution)__")
    if buys["qty"]:
        lines.append(
            f"  Entry: **{buys['qty']:.0f}** contracts @ avg **{buys['avg_cents']:.1f}¢** "
            f"(${t['cost_basis_dollars']:.2f} basis, "
            f"{buys['taker_share']:.0%} taker), first fill {buys['first_ts']}.")
    if sells["qty"]:
        lines.append(
            f"  Exit: sold **{sells['qty']:.0f}** @ avg **{sells['avg_cents']:.1f}¢** "
            f"(${t['proceeds_dollars']:.2f} proceeds).")
    if t["net_qty"] > 0:
        lines.append(f"  Held **{t['net_qty']:.0f}** to settlement.")
    if a["edge_captured_cents"] is not None:
        lines.append(
            f"  Edge captured: **{a['edge_captured_cents']:+.1f}¢/contract** "
            f"(100¢ settle − {buys['avg_cents']:.1f}¢ entry − fees).")

    # THE LESSON — one line tying why→how to the outcome.
    lines.append("")
    lines.append("__Takeaway__")
    lines.append("  " + _takeaway(a))
    return "\n".join(lines)


def _takeaway(a: dict) -> str:
    th, t = a["thesis"], a["trade"]
    if a["outcome"] == "win" and th and th["kind"] == "buy_winner":
        speed = (f" ~{th['reaction_minutes']:.0f} min ahead of the reprice"
                 if th.get("reaction_minutes") is not None else "")
        base = ("The settlement report — not a forecast — decided the day. The "
                "CLI printed a value that put your bracket in the money, the "
                f"sniper saw it{speed}, and you bought certainty below $1.")
        if a["edge_captured_cents"] is not None:
            base += f" That gap is the {a['edge_captured_cents']:+.1f}¢ edge you banked."
        return base
    if a["outcome"] == "win":
        return ("Won, but with no journaled thesis — capture the setup next "
                "time so the edge is repeatable, not luck.")
    if a["outcome"] == "loss":
        return ("Settled against the position — cross-check the entry price "
                "and boundary margin against postmortem_2026-01-20.md.")
    return "Still open — re-run after settlement for the full picture."


# ── settlement lookup (best-effort, needs creds) ───────────────────────────
async def fetch_result(ticker: str) -> str | None:
    """Kalshi market `result` ('yes'/'no'/None). Returns None on any failure
    so the autopsy still runs from realized P&L alone."""
    import os
    try:
        from kalshi_client import KalshiClient
    except Exception:  # noqa: BLE001
        return None
    key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
    if not key_path or not Path(key_path).is_file():
        return None
    client = KalshiClient(api_key_id=os.getenv("KALSHI_API_KEY_ID", ""),
                          private_key_path=key_path, demo_mode=False)
    try:
        await client.start()
        r = await client._req_safe("GET", f"/markets/{ticker}")
        mk = (r or {}).get("market") or {}
        res = mk.get("result")
        return res if res in ("yes", "no") else None
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            await client.stop()
        except Exception:  # noqa: BLE001
            pass


async def main_async(ticker: str | None, want_loss: bool, report: str,
                     days: int | None) -> int:
    account = load_account()
    if ticker is None:
        ticker = pick_ticker(account, want_loss=want_loss)
    if ticker is None:
        print("No settled position found in logs/live_account.json — nothing to "
              "autopsy yet. (Is live_watch journaling on the server?)",
              file=sys.stderr)
        return 1

    all_fills = load_fills()
    since = datetime.now(timezone.utc) - timedelta(days=days) if days else None
    finding = match_finding(load_sniper_findings(since=since), ticker)
    result = await fetch_result(ticker)

    autopsy = build_autopsy(ticker, all_fills, account, finding, result)
    text = format_report(autopsy)
    print(text)

    if report == "discord":
        try:
            from notifications import send_discord_alert
            await send_discord_alert(title=f"🔬 Trade autopsy — {ticker}",
                                     description=text[:4096], color=0x9B59B6,
                                     context="trade_autopsy")
        except Exception as exc:  # noqa: BLE001
            print(f"discord send failed: {exc}", file=sys.stderr)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ticker", help="autopsy this exact ticker "
                    "(default: auto-pick the biggest recent win)")
    ap.add_argument("--loss", action="store_true",
                    help="auto-pick the biggest recent LOSS instead of a win")
    ap.add_argument("--days", type=int, default=None,
                    help="only match sniper findings newer than N days")
    ap.add_argument("--report", choices=("stdout", "discord"), default="stdout")
    args = ap.parse_args()
    import asyncio
    raise SystemExit(asyncio.run(
        main_async(args.ticker, args.loss, args.report, args.days)))


if __name__ == "__main__":
    main()
