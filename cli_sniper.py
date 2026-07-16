#!/usr/bin/env python3
"""CLI SNIPER — race the settlement report, not the weather.

2026-07-04, live: the LOT afternoon climate report printed a Midway max of
85°F at 4:36 PM CDT and KXHIGHCHI-26JUL04-B85.5 repriced 16¢ → 99¢ within
~10 minutes. The report is public, per-station, and published at a roughly
known local time. This job reads it seconds-to-minutes after publication and
alerts on the brackets it has just decided — before or alongside the wall.

Feed: forecast.weather.gov/product.php?site={WFO}&issuedby={AWIPS}&product=CLI
— the EXACT source Kalshi's settlement_sources cite, deterministic per
station (api.weather.gov's CLI listings collide same-minute station products
and are unreliable). One ~28 KB page per station.

Semantics (mirrors core.obs certainty logic):
  afternoon product ("VALID TODAY AS OF 0400 PM"): printed max M is a FLOOR
      (final ≥ M) — brackets with hi < M are certainly dead; the bracket
      containing M leads but late warming can still shift it up.
  morning product (no VALID TODAY line): FINAL for yesterday — exactly one
      bracket wins; everything else is dead.
  lows mirror: printed min m ⇒ final ≤ m.

Per run (cron */2): stations whose local time is inside an issuance window
(afternoon 15:30–18:30, morning 05:30–08:30) get one page fetch; UNSEEN
issuances (state-deduped on the WMO day-hour-minute stamp) get parsed and
their ladders' books checked; opportunities → Discord with a ready-to-run
command. Outside all windows: heartbeat and exit, zero fetches.

ALERT ONLY — never trades. Journal: logs/cli_sniper/YYYY-MM-DD.jsonl
(every parse, uncensored). Heartbeat "cli_sniper" on every clean exit.

Usage:
    python3 cli_sniper.py --once                 # cron entry point
    python3 cli_sniper.py --once --dry-run       # print, no Discord/state
    python3 cli_sniper.py --replay MDW           # latest MDW product, full
                                                 # pipeline, no Discord/state
    python3 cli_sniper.py --replay MDW:2         # 2 issuances back

Suggested crontab (NOT auto-installed):
    */2 * * * * $VENV $PROJ/cli_sniper.py --once >> /tmp/cli_sniper.log 2>&1
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import re
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from core import drift, dsm  # noqa: E402
from core.brackets import contains, is_dead, parse_subtitle  # noqa: E402
from core.io import atomic_write_json  # noqa: E402
from core.walls import WALL_ASK_DEPTH  # noqa: E402
from core.obs import (  # noqa: E402
    annotate_floor_buys, corroborated_extreme, fetch_day_obs_timed, trend_class)
from dead_bracket_sweeper import bid_proceeds_cents  # noqa: E402
from heartbeat import write_heartbeat  # noqa: E402
from ladders import Ladder, by_awips  # noqa: E402
from log_setup import get_logger  # noqa: E402
from market_timeseries import extract_target_date_from_ticker  # noqa: E402

logger = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
STATE_FILE = PROJECT_ROOT / "cli_sniper_state.json"
JOURNAL_DIR = PROJECT_ROOT / "logs" / "cli_sniper"

PRODUCT_URL = ("https://forecast.weather.gov/product.php?site={wfo}"
               "&issuedby={awips}&product=CLI&format=txt&version={version}"
               "&glossary=0")
USER_AGENT = "WeatherEdgeCLISniper/1.0"

AFTERNOON_WINDOW = (15.5, 18.5)   # local fractional hours
# Morning finals actually issue 01:13-04:51 local (measured across 16 offices
# by backtest/cli_timing.py on the journal, 2026-07-05); the old (5.5, 8.5)
# window missed every one by ~4h, ceding the reprice to settlement bots.
MORNING_WINDOW = (1.0, 5.5)
SEEN_MAX_AGE_H = 72
ALERTED_MAX_AGE_H = 48
BUY_MAX_ASK_FINAL_C = 85          # certain winner: buy up to this ask
BUY_MAX_ASK_FLOOR_C = 70          # floor leader: residual warming risk
MIN_SELL_NET_C = 100              # dead-bid alert floor, cents ($1)
# WALL_ASK_DEPTH (core/walls.py): a deep opposing ask on a "winner" is the
# certainty-wall signature — flag, never fade.
# A LOW ladder's afternoon print locks nothing — the min can still fall until
# midnight LST, so "bracket contains printed min" is an open forecast bet.
# Scorecard 2026-07-08: this class realized -30.8¢/contract over 6 settles
# (the high/floor class was +2.3¢). Journaled for measurement, never alerted.
SUPPRESS_LOW_FLOOR_BUYS = True
# Corrections can issue any time (2026-07-08: post-final MM scrub at ~09:26Z).
# Outside issuance windows, stations with journaled findings in the last 24h
# get a v1 re-fetch every ~20 min so a corrected re-issue is seen and alerted.
CORRECTION_SWEEP_EVERY_MIN = 20
# Reissue guard (2026-07-16 BOS): BOX printed a bogus MINIMUM of 51 (stamp
# 162129), then silently re-issued 162139 with the real 69 — no CORRECTED
# tag, correction field null — and the sniper staged a falsified sell_dead
# on the live 83¢ favorite. forecast.weather.gov kept serving the stale
# product for 14 more minutes, so re-fetching the page can't catch this
# class; the IEM AFOS archive (NOAAPort ingest) had the re-issue within a
# minute. Before staging, the newest same-date archive product is compared
# against the print: a moved premise stamps `reissue_conflict` — blocks the
# one-tap button and downgrades the alert to info-only (same gate family as
# obs_kill/obs_warn/wall_ask). Fail open on IEM refusal: the guard only
# removes suggestions (§8 principle 5). take_approver runs the same check
# again at fire time, and a later run that sees the reissue supersedes any
# still-active button (three layers; none adds latency to the money path).
ARCHIVE_PRODUCT_LIMIT = 5

MONTHS = {m: i + 1 for i, m in enumerate(
    ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY",
     "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"])}

# The stamp line may carry a correction suffix (CCA/CCB/COR) — e.g.
# "CDUS42 KMFL 081455 CCA". 2026-07-08 live: a post-final correction scrubbed
# the MIA minimum to MM and the old $-anchored regex silently rejected the
# whole product, leaving positions premised on the original value blind.
_WMO_LINE = re.compile(r"^\w{6}\s+K\w{3}\s+(\d{6})(?:\s+([A-Z]{2,3}))?\s*$", re.M)
_AWIPS_LINE = re.compile(r"^CLI(\w{3})\s*$", re.M)
_SUMMARY = re.compile(r"CLIMATE SUMMARY FOR\s+(\w+)\s+(\d{1,2})\s+(\d{4})")
_VALID_TODAY = re.compile(r"VALID TODAY AS OF")
_MAX_LINE = re.compile(r"^\s*MAXIMUM\s+(-?\d+)", re.M)
_MIN_LINE = re.compile(r"^\s*MINIMUM\s+(-?\d+)", re.M)
_PRE_BLOCK = re.compile(r"<pre[^>]*>(.*?)</pre>", re.S | re.I)


@dataclass
class ParsedCLI:
    awips: str
    stamp: str            # WMO ddhhmm — dedup key component
    summary_date: str     # ISO date the report covers
    is_final: bool        # morning product (final) vs afternoon floor
    max_f: int | None
    min_f: int | None
    correction: str | None = None   # WMO suffix (CCA/COR/...) when corrected


def parse_product(text: str) -> ParsedCLI | None:
    """Structured read of one CLI product; None when essentials missing."""
    awips = _AWIPS_LINE.search(text or "")
    stamp = _WMO_LINE.search(text or "")
    summary = _SUMMARY.search(text or "")
    if not (awips and stamp and summary):
        return None
    month = MONTHS.get(summary.group(1).upper())
    if not month:
        return None
    mx = _MAX_LINE.search(text)
    mn = _MIN_LINE.search(text)
    return ParsedCLI(
        awips=awips.group(1).upper(),
        stamp=stamp.group(1),
        summary_date=f"{summary.group(3)}-{month:02d}-{int(summary.group(2)):02d}",
        is_final=not _VALID_TODAY.search(text),
        max_f=int(mx.group(1)) if mx else None,
        min_f=int(mn.group(1)) if mn else None,
        correction=stamp.group(2).upper() if stamp.group(2) else None,
    )


def _seen_key(parsed: ParsedCLI) -> str:
    """Dedup key — a corrected re-issue is a distinct, never-seen product."""
    base = f"{parsed.awips}:{parsed.stamp}"
    return f"{base}:{parsed.correction}" if parsed.correction else base


# Same-day products issued before mid-afternoon (e.g. a 07:31 local "so far"
# report) carry no daily-extreme information — the real floor is the ~16:30
# issue. 2026-07-05: three such products alerted false 1¢ "certain winners"
# (AUS/SAT/DEN) because the AS-OF regex missed them; finality now comes from
# the calendar, not the regex.
INTRADAY_CLASSIFY_MIN_LOCAL_H = 15.0


def effective_finality(parsed: ParsedCLI, tz: str, now_utc: datetime) -> str:
    """'final' | 'floor' | 'skip' — trust the calendar over the AS-OF regex.

    A CLI product can only FINALIZE the day BEFORE its station-local
    issuance date. A same-day product is an intraday snapshot: meaningful
    as a floor only from mid-afternoon on; earlier issues must not classify.
    """
    from backtest.cli_timing import stamp_to_utc

    issued = stamp_to_utc(parsed.stamp, now_utc)
    if issued is None:
        return "skip"
    local = issued.astimezone(ZoneInfo(tz))
    issue_date = local.date().isoformat()
    if parsed.summary_date < issue_date:
        return "final"
    if (parsed.summary_date == issue_date
            and local.hour + local.minute / 60 >= INTRADAY_CLASSIFY_MIN_LOCAL_H):
        return "floor"
    return "skip"


def window_kind(local_hour_frac: float) -> str | None:
    if AFTERNOON_WINDOW[0] <= local_hour_frac < AFTERNOON_WINDOW[1]:
        return "afternoon"
    if MORNING_WINDOW[0] <= local_hour_frac < MORNING_WINDOW[1]:
        return "morning"
    return None


def stations_in_window(now_utc: datetime, ladder_groups: dict[str, list[Ladder]]) -> list[str]:
    """AWIPS codes whose station-local time is inside an issuance window."""
    out = []
    for awips, group in ladder_groups.items():
        local = now_utc.astimezone(ZoneInfo(group[0].tz))
        if window_kind(local.hour + local.minute / 60) is not None:
            out.append(awips)
    return sorted(out)


def classify(parsed: ParsedCLI, ladder: Ladder, markets: list[dict]) -> list[dict]:
    """Decisions this product forces on one ladder's brackets.

    Returns finding dicts: kind 'sell_dead' (bracket can no longer win) or
    'buy_winner' (bracket contains the printed value; certain when final).
    """
    printed = parsed.max_f if ladder.kind == "high" else parsed.min_f
    if printed is None:
        return []
    findings = []
    for mkt in markets or []:
        ticker = mkt.get("ticker", "")
        if extract_target_date_from_ticker(ticker) != parsed.summary_date:
            continue
        bounds = parse_subtitle(mkt.get("subtitle") or mkt.get("yes_sub_title"))
        if bounds is None:
            continue
        lo, hi = bounds
        base = {"ticker": ticker, "subtitle": mkt.get("subtitle") or mkt.get("yes_sub_title"),
                "series": ladder.series, "ladder_kind": ladder.kind,
                "printed": printed, "final": parsed.is_final,
                # Product provenance: the take queue stages these as the
                # entry's premise so the approver can re-verify the print
                # still stands at fire time (reissue guard).
                "awips": parsed.awips, "stamp": parsed.stamp,
                "summary_date": parsed.summary_date}
        if parsed.correction:
            base["corrected"] = parsed.correction
        if is_dead(ladder.kind, lo, hi, printed):
            findings.append({**base, "kind": "sell_dead"})
        elif contains(lo, hi, printed):
            findings.append({**base, "kind": "buy_winner"})
    return findings


def apply_dsm_veto(findings: list[dict], reports: list[dsm.DSMReport],
                   summary_date: str) -> tuple[list[dict], list[dict]]:
    """Split floor findings into (kept, vetoed) against the station's DSM.

    The DSM is authoritative over a prelim CLI print (final CLI == DSM max
    85/85 days in the MIA archive study, 2026-07-07): a buy_winner premised
    on a printed floor the DSM already exceeds is a losing trade — the final
    report will follow the DSM into the next bracket. But only when the DSM
    extreme actually leaves the bracket: 2026-07-09 MSP printed max 83, DSM
    said 84, both inside the "83° to 84°" bracket — the veto killed a winning
    trade. A DSM extreme still inside the finding's bracket confirms the buy
    rather than contradicting it (the same day's DEN low — printed 59, DSM 57,
    bracket 58-59 — stays vetoed). sell_dead findings are never vetoed (a
    bigger DSM extreme only strengthens deadness). With no usable DSM the
    finding passes through marked dsm="unchecked" (fail open: alerts are
    human-verified, and a veto only removes suggestions).
    """
    kept, vetoed = [], []
    day_reports = dsm.reports_for_date(reports, summary_date)
    for f in findings:
        if f["kind"] != "buy_winner":
            kept.append(f)
            continue
        extreme = dsm.dsm_extreme(day_reports, f["ladder_kind"])
        if extreme is None:
            kept.append({**f, "dsm": "unchecked"})
        elif dsm.contradicts(f["ladder_kind"], f["printed"], extreme[0]):
            bounds = parse_subtitle(f["subtitle"])
            if bounds and contains(*bounds, extreme[0]):
                kept.append({**f, "dsm": extreme[0]})
            else:
                vetoed.append({**f, "kind": "dsm_veto",
                               "dsm_extreme": extreme[0],
                               "dsm_time_lst": extreme[1]})
        else:
            kept.append({**f, "dsm": extreme[0]})
    return kept, vetoed


def _conflicts(kind: str, ladder_kind: str, is_final: bool,
               v_old: int, v_new: int | None) -> bool:
    """Does a re-issued extreme refute a finding premised on the old one?

    Deadness is monotone on floors: a floor max can only legitimately RISE
    (running max) and a floor min only FALL, and legit movement leaves
    sell_dead brackets just as dead. Everything else refutes — any move
    under a buy_winner (its bracket premise), any move on a FINAL (nothing
    should move), a value scrubbed to MM, and an impossible-direction move
    (max down / min up — the BOS 51→69 bogus-print signature).
    """
    if v_new == v_old:
        return False
    if kind == "sell_dead" and not is_final and v_new is not None:
        return not (v_new > v_old if ladder_kind == "high" else v_new < v_old)
    return True


# AFOS blobs concatenate products; split on the control char or the
# 3-digit sequence header before each WMO line (backtest/metar_leak_study
# uses the same shape against this feed).
_AFOS_SPLIT = re.compile(r"\x01|\n(?=\d{3}\s*\n\w{6}\s+K\w{3}\s+\d{6})")


def fetch_archive_products(awips: str) -> list[ParsedCLI] | None:
    """Recent CLI products for one station from the IEM AFOS archive — the
    reissue guard's second opinion. None = feed unavailable (fail open),
    a list (possibly empty) = the archive answered."""
    try:
        blob = dsm.afos_text(f"CLI{awips.upper()}", limit=ARCHIVE_PRODUCT_LIMIT)
    except Exception as exc:  # noqa: BLE001 — the guard only removes suggestions
        logger.warning(f"{awips}: CLI archive fetch failed: {exc}")
        return None
    return [p for chunk in _AFOS_SPLIT.split(blob or "")
            if (p := parse_product(chunk)) is not None]


def newer_archive_product(parsed: ParsedCLI, tz: str, now_utc: datetime,
                          products: list[ParsedCLI] | None,
                          ) -> tuple[str, ParsedCLI | None]:
    """('newer'|'clear'|'unchecked', product): the newest archive product
    STRICTLY newer than `parsed` for the same station/date/finality class.

    Only a newer product can refute a print — an older archive copy that
    differs is just the floor legitimately moving between issuances (and
    IEM ingest lag must never suppress the freshest print's staging).
    """
    from backtest.cli_timing import stamp_to_utc

    if products is None:
        return "unchecked", None
    ours = stamp_to_utc(parsed.stamp, now_utc)
    if ours is None:
        return "unchecked", None
    newest, newest_t = None, None
    for p in products:
        if p.awips != parsed.awips or p.summary_date != parsed.summary_date:
            continue
        finality = effective_finality(p, tz, now_utc)
        if finality == "skip" or (finality == "final") != parsed.is_final:
            continue
        t = stamp_to_utc(p.stamp, now_utc)
        if t is None or t <= ours:
            continue
        if newest_t is None or t > newest_t:
            newest, newest_t = p, t
    return ("newer", newest) if newest is not None else ("clear", None)


def apply_reissue_conflicts(findings: list[dict], newer: ParsedCLI) -> int:
    """Stamp findings the newer product refutes; returns how many."""
    stamped = 0
    for f in findings:
        if f.get("kind") not in ("sell_dead", "buy_winner"):
            continue
        v_new = newer.max_f if f["ladder_kind"] == "high" else newer.min_f
        if _conflicts(f["kind"], f["ladder_kind"], f["final"],
                      f["printed"], v_new):
            f["reissue_conflict"] = (
                f"reissued {newer.stamp}: "
                f"{'max' if f['ladder_kind'] == 'high' else 'min'} "
                f"{f['printed']}→{v_new if v_new is not None else 'MM'}")
            stamped += 1
    return stamped


def check_premise(entry: dict, now_utc: datetime,
                  products: list[ParsedCLI] | None = None) -> tuple[str, str]:
    """Fire-time re-check of a staged entry's CLI premise vs the archive:
    ('moved'|'clear'|'unchecked', reason). take_approver calls this just
    before take.py — the last line of defense when a bogus print staged
    BEFORE its silent re-issue existed anywhere (the stage-time guard
    can't beat causality; this one runs minutes later, at money time)."""
    premise = entry.get("premise") or {}
    try:
        pseudo = ParsedCLI(awips=premise["awips"], stamp=premise["stamp"],
                           summary_date=premise["summary_date"],
                           is_final=bool(premise["final"]),
                           max_f=None, min_f=None)
        printed, lk = premise["printed"], premise["ladder_kind"]
    except (KeyError, TypeError):
        return "unchecked", "no CLI premise on entry"
    if printed is None or lk not in ("high", "low"):
        return "unchecked", "no CLI premise on entry"
    group = by_awips().get(pseudo.awips)
    if group is None:
        return "unchecked", f"unknown station {pseudo.awips}"
    if products is None:
        products = fetch_archive_products(pseudo.awips)
    status, newer = newer_archive_product(pseudo, group[0].tz, now_utc, products)
    if status == "unchecked":
        return "unchecked", "CLI archive unavailable — passing (fail open)"
    if newer is None:
        return "clear", "no newer product in the archive"
    v_new = newer.max_f if lk == "high" else newer.min_f
    if _conflicts(entry.get("kind", ""), lk, pseudo.is_final, printed, v_new):
        return "moved", (f"CLI reissued ({premise['stamp']}→{newer.stamp}): "
                         f"{'max' if lk == 'high' else 'min'} "
                         f"{printed}→{v_new if v_new is not None else 'MM'} "
                         f"— premise dead")
    return "clear", f"reissue {newer.stamp} keeps the premise"


def _prior_journaled_product(awips: str, summary_date: str, want_final: bool,
                             now_utc: datetime) -> dict | None:
    """Most recent journaled product row (with findings, not skipped) for
    this station-date-finality — the premise prior alerts/stages ran on."""
    rows = [e for e in _recent_journal_entries(now_utc)
            if e.get("awips") == awips and e.get("summary_date") == summary_date
            and e.get("findings") and not e.get("skipped")
            and bool(e.get("is_final")) == want_final]
    return max(rows, key=lambda e: e.get("ts", "")) if rows else None


def reissue_moves(prior: dict, parsed: ParsedCLI,
                  now_utc: datetime) -> dict[str, tuple]:
    """{'high': (old, new), 'low': (old, new)} for moved extremes that a
    prior journaled finding was actually premised on (a move on a ladder
    kind nothing classified against is not an exit signal). Only a
    STRICTLY NEWER product moves a premise — replaying or re-serving an
    older stamp must never read as a reissue."""
    from backtest.cli_timing import stamp_to_utc

    ours = stamp_to_utc(parsed.stamp, now_utc)
    theirs = stamp_to_utc(prior.get("stamp") or "", now_utc)
    if ours is None or theirs is None or ours <= theirs:
        return {}
    premised = {f.get("ladder_kind") for f in prior.get("findings") or []
                if not f.get("suppressed")}
    moves = {}
    if (prior.get("max_f") is not None and prior.get("max_f") != parsed.max_f
            and "high" in premised):
        moves["high"] = (prior["max_f"], parsed.max_f)
    if (prior.get("min_f") is not None and prior.get("min_f") != parsed.min_f
            and "low" in premised):
        moves["low"] = (prior["min_f"], parsed.min_f)
    return moves


def superseded_entry_ids(queue_entries: dict, parsed: ParsedCLI,
                         now_utc: datetime) -> list[str]:
    """Active cli_sniper queue entries whose staged premise this (newer)
    product refutes — the buttons to retract when a reissue lands."""
    from backtest.cli_timing import stamp_to_utc

    from core import take_queue

    ours = stamp_to_utc(parsed.stamp, now_utc)
    out = []
    for eid, e in queue_entries.items():
        if (e.get("source") != "cli_sniper"
                or e.get("status") not in take_queue.ACTIVE_STATUSES):
            continue
        pr = e.get("premise") or {}
        if (pr.get("awips") != parsed.awips
                or pr.get("summary_date") != parsed.summary_date
                or bool(pr.get("final")) != parsed.is_final
                or pr.get("printed") is None
                or pr.get("ladder_kind") not in ("high", "low")):
            continue
        theirs = stamp_to_utc(pr.get("stamp") or "", now_utc)
        if ours is None or theirs is None or ours <= theirs:
            continue
        v_new = parsed.max_f if pr["ladder_kind"] == "high" else parsed.min_f
        if _conflicts(e.get("kind", ""), pr["ladder_kind"],
                      bool(pr.get("final")), pr["printed"], v_new):
            out.append(eid)
    return out


async def _retract_superseded(parsed: ParsedCLI, now_utc: datetime) -> int:
    """Kill active buttons premised on a value this product moved; returns
    how many were retracted. Never raises — retraction must not break the
    alert path (same contract as staging)."""
    try:
        from core import take_queue

        snapshot = take_queue.load_queue()["entries"]
        ids = superseded_entry_ids(snapshot, parsed, now_utc)
        if not ids:
            return 0
        reason = (f"CLI reissued {parsed.stamp} "
                  f"(max {parsed.max_f} min {parsed.min_f}) — premise dead")
        dead = take_queue.supersede_entries(ids, reason, now_utc)
        for e in dead:
            logger.warning(f"{e['ticker']}: staged take RETRACTED — {reason}")
        if dead:
            from take_approver import retract_buttons

            await retract_buttons(dead, reason)
        return len(dead)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"{parsed.awips}: supersede/retract failed: {exc}")
        return 0


def _fetch_product(wfo: str, awips: str, version: int = 1) -> str | None:
    url = PRODUCT_URL.format(wfo=wfo, awips=awips, version=version)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8", "replace")
    m = _PRE_BLOCK.search(raw)
    return html.unescape(m.group(1)) if m else None


def _load_state() -> dict:
    state = {"seen": {}, "alerted": {}}
    if STATE_FILE.exists():
        try:
            state.update(json.loads(STATE_FILE.read_text()))
        except (json.JSONDecodeError, OSError):
            pass
    cutoff_seen = (datetime.now(timezone.utc) - timedelta(hours=SEEN_MAX_AGE_H)).isoformat()
    cutoff_alerted = (datetime.now(timezone.utc) - timedelta(hours=ALERTED_MAX_AGE_H)).isoformat()
    state["seen"] = {k: v for k, v in state["seen"].items() if v >= cutoff_seen}
    state["alerted"] = {k: v for k, v in state["alerted"].items()
                        if v.get("ts", "") >= cutoff_alerted}
    return state


def _save_state(state: dict) -> None:
    atomic_write_json(STATE_FILE, state, indent=1)


def _journal(entry: dict, now_utc: datetime) -> None:
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    path = JOURNAL_DIR / f"{now_utc.strftime('%Y-%m-%d')}.jsonl"
    with path.open("a") as fh:
        fh.write(json.dumps(entry, separators=(",", ":")) + "\n")


def _recent_journal_entries(now_utc: datetime) -> list[dict]:
    """Journal entries from today's and yesterday's files (≈ last 24-48h)."""
    out = []
    for day in (now_utc, now_utc - timedelta(days=1)):
        path = JOURNAL_DIR / f"{day.strftime('%Y-%m-%d')}.jsonl"
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _recently_active_stations(now_utc: datetime) -> set[str]:
    """Stations whose journaled products produced findings in the last ~24h."""
    return {e["awips"] for e in _recent_journal_entries(now_utc)
            if e.get("awips") and e.get("findings")}


def _journal_has_findings(awips: str, summary_date: str, now_utc: datetime) -> bool:
    """Did we journal findings for this station+date? (correction relevance)"""
    return any(e.get("awips") == awips and e.get("summary_date") == summary_date
               and e.get("findings") for e in _recent_journal_entries(now_utc))


def _take_cmd(action: str, ticker: str, qty: int, price_c: int) -> str:
    return (f".venv/bin/python scripts/take.py {ticker} {action} yes "
            f"{qty} {price_c}")


async def _price_findings(client, findings: list[dict]) -> list[dict]:
    """Attach live book economics; keep only actionable findings."""
    priced = []
    for f in findings:
        try:
            book = await client.get_orderbook(f["ticker"])
        except Exception as exc:  # noqa: BLE001 — one book must not kill the run
            logger.warning(f"{f['ticker']}: book fetch failed: {exc}")
            continue
        yes_bids = sorted((book or {}).get("yes") or [], key=lambda lv: -lv[0])
        no_bids = sorted((book or {}).get("no") or [], key=lambda lv: -lv[0])
        if f["kind"] == "sell_dead":
            net, contracts, levels = bid_proceeds_cents(yes_bids)
            if net >= MIN_SELL_NET_C:
                price = levels[-1][0] if levels else 0
                priced.append({**f, "net_cents": net, "contracts": contracts,
                               "levels": levels,
                               "cmd": _take_cmd("sell", f["ticker"], contracts, price)})
        else:  # buy_winner
            if not no_bids:
                continue
            ask = 100 - no_bids[0][0]
            depth = no_bids[0][1]
            limit = BUY_MAX_ASK_FINAL_C if f["final"] else BUY_MAX_ASK_FLOOR_C
            if 1 <= ask <= limit and depth >= 1:
                qty = max(1, int(depth))
                entry = {**f, "ask": ask, "ask_depth": depth,
                         "cmd": _take_cmd("buy", f["ticker"], qty, ask)}
                if depth >= WALL_ASK_DEPTH:
                    entry["wall_ask"] = True
                _attach_drift_economics(entry)
                if (SUPPRESS_LOW_FLOOR_BUYS and f["ladder_kind"] == "low"
                        and not f["final"]):
                    # Journal-only: measured -EV forecast bet, never alerted.
                    entry.pop("cmd")
                    entry["suppressed"] = "low_floor_forecast"
                priced.append(entry)
    return priced


# The obs-vs-floor annotation moved to core.obs 2026-07-13 so the METAR
# sniper shares it (its 6-hr-group floor buys carry the same warming risk).
_annotate_obs_context = annotate_floor_buys


_DRIFT_DIST: drift.DriftDist | None = None


def _drift_dist() -> drift.DriftDist:
    """Floor→final distribution from the journal, computed once per run."""
    global _DRIFT_DIST
    if _DRIFT_DIST is None:
        _DRIFT_DIST = drift.distribution(drift.load_pairs(JOURNAL_DIR))
    return _DRIFT_DIST


def _attach_drift_economics(entry: dict) -> None:
    """Quantify a floor buy_winner with the measured drift distribution.

    2026-07-09: three floor-containing brackets graded 86-98% by this table
    went unbought at 51-66¢ because the alert carried no probability. The
    number belongs in the alert, not in a human's head. High ladders only —
    a low floor locks nothing (the min can still fall until midnight LST).
    """
    if entry["final"] or entry["ladder_kind"] != "high":
        return
    bounds = parse_subtitle(entry.get("subtitle"))
    if bounds is None:
        return
    dist = _drift_dist()
    prob = drift.bracket_win_prob(entry["printed"], bounds[0], bounds[1], dist)
    if prob is None:
        return
    entry["drift_prob"] = round(prob, 3)
    entry["drift_n"] = dist.n
    entry["drift_ev_c"] = round(drift.ev_cents(prob, entry["ask"]), 1)


def format_alert(opps: list[dict]) -> tuple[str, str]:
    n_buy = sum(1 for o in opps if o["kind"] == "buy_winner")
    n_veto = sum(1 for o in opps if o["kind"] == "dsm_veto")
    n_sell = sum(1 for o in opps if o["kind"] == "sell_dead")
    title = f"🎯 CLI SNIPER — {n_buy} winner buy(s), {n_sell} dead-bid sell(s)"
    if n_veto:
        title += f", {n_veto} DSM veto(es)"
    n_corr = sum(1 for o in opps if o["kind"] == "correction_notice")
    if n_corr:
        title += f", {n_corr} CORRECTION(s)"
    n_re = sum(1 for o in opps if o["kind"] == "reissue_notice")
    if n_re:
        title += f", {n_re} REISSUE(s)"
    lines = []
    for o in opps:
        if o["kind"] == "reissue_notice":
            desc = ", ".join(
                f"{'max' if k == 'high' else 'min'} "
                f"{v[0]}→{v[1] if v[1] is not None else 'MM (removed)'}"
                for k, v in (o.get("moves") or {}).items())
            lines.append(
                f"🛑 **REISSUE (no CORRECTED tag)** — {o['awips']} "
                f"{o['summary_date']}: {desc} "
                f"(stamp {o['prior_stamp']}→{o['stamp']}). Prior findings "
                f"were premised on the old value"
                + (f"; {o['retracted']} staged button(s) retracted"
                   if o.get("retracted") else "")
                + " — re-verify any open trade NOW.")
            continue
        if o["kind"] == "correction_notice":
            lines.append(
                f"🛑 **CORRECTION {o['corrected']}** — {o['awips']} "
                f"{o['summary_date']}: max now **{o['max_f'] if o['max_f'] is not None else 'MM (removed)'}**, "
                f"min now **{o['min_f'] if o['min_f'] is not None else 'MM (removed)'}**. "
                f"Prior findings on this ladder may be premised on a value "
                f"that no longer exists — re-verify any open trade NOW.")
            continue
        drift = "warming" if o.get("ladder_kind") == "high" else "cooling"
        cert = "FINAL" if o["final"] else f"floor (post-4PM {drift} risk)"
        if o["kind"] == "dsm_veto":
            lines.append(
                f"⛔ **{o['ticker']}** ({o['subtitle']}) — CLI printed "
                f"**{o['printed']}°** but DSM already has "
                f"**{o['dsm_extreme']}° @ {o['dsm_time_lst']} LST** → "
                f"printed-bracket buy VETOED; final CLI follows the DSM "
                f"(85/85 archive) — revision side likely wins")
        elif o["kind"] == "buy_winner":
            econ = ""
            # A wall invalidates the drift base rate (walls 5-0 vs floor
            # signals) — printing "EV +87¢" next to "never fade" is a
            # temptation dispenser (MIN T94, 2026-07-14).
            if "drift_prob" in o and not o.get("wall_ask"):
                econ = (f" | drift {o['drift_prob']:.0%} win "
                        f"(n={o['drift_n']}), EV {o['drift_ev_c']:+.0f}¢")
            if o.get("obs_kill"):
                econ += f"\n  🚫 **{o['obs_kill']}** — do not buy"
            elif o.get("obs_warn"):
                econ += f"\n  ⚠️ **{o['obs_warn']}**"
            elif "obs_max_f" in o:
                econ += f" | obs so far {o['obs_max_f']}°"
            if o.get("obs_trend") == "still_hot":
                econ += (f"\n  🌡 **still hot at print** — drift risk "
                         f"{o['trend_drift_p']:.0%} vs 3% post-peak "
                         f"(archive n={o['trend_drift_n']})")
            elif o.get("obs_trend") == "post_peak":
                econ += (f" | post-peak, drift {o['trend_drift_p']:.0%} "
                         f"(archive n={o['trend_drift_n']})")
            if o.get("wall_ask"):
                econ += (f"\n  🧱 {o['ask_depth']:.0f}-deep ask wall — "
                         f"walls are 5-0 vs floor signals, never fade")
            if o.get("reissue_conflict"):
                econ += (f"\n  🛑 **{o['reissue_conflict']}** — "
                         f"do not trade this print")
            lines.append(
                f"**{o['ticker']}** ({o['subtitle']}) — CLI printed **{o['printed']}°** "
                f"[{cert}] → ask {o['ask']}¢ × {o['ask_depth']:.0f}{econ}\n  `{o['cmd']}`")
        else:
            levels = ", ".join(f"{p}¢×{q}" for p, q in o["levels"])
            warn = (f"\n  🛑 **{o['reissue_conflict']}** — do not trade this print"
                    if o.get("reissue_conflict") else "")
            lines.append(
                f"**{o['ticker']}** ({o['subtitle']}) — dead vs CLI {o['printed']}° "
                f"[{cert}] → bids {levels}, net ~${o['net_cents'] / 100:.2f}{warn}\n  `{o['cmd']}`")
    lines.append("_Alert only — the CLI text is quoted in the journal; verify before trading._")
    return title, "\n".join(lines)


async def run(dry_run: bool, replay: str | None) -> None:
    import os

    from kalshi_client import KalshiClient

    now_utc = datetime.now(timezone.utc)
    groups = by_awips()
    state = _load_state()

    if replay:
        awips, _, ver = replay.partition(":")
        targets = {awips.upper(): int(ver or 1)}
        if awips.upper() not in groups:
            raise SystemExit(f"unknown station {awips!r} — not in ladders.json")
    else:
        targets = {a: 1 for a in stations_in_window(now_utc, groups)}
        # Correction sweep: recently-active stations get a v1 re-fetch every
        # ~20 min even out of window — a corrected re-issue (new seen-key)
        # flows through the normal pipeline; anything unchanged dedups out.
        if now_utc.minute % CORRECTION_SWEEP_EVERY_MIN < 2:
            for awips in _recently_active_stations(now_utc):
                if awips in groups:
                    targets.setdefault(awips, 1)
        if not targets:
            logger.info("cli sniper: no station in an issuance window")
            return

    new_parses: list[tuple[ParsedCLI, list[Ladder]]] = []
    for awips, version in targets.items():
        group = groups[awips]
        try:
            text = _fetch_product(group[0].wfo, awips, version)
        except Exception as exc:  # noqa: BLE001 — one station must not kill the run
            logger.warning(f"{awips}: product fetch failed: {exc}")
            continue
        parsed = parse_product(text or "")
        if parsed is None:
            logger.info(f"{awips}: no parseable CLI product")
            continue
        key = _seen_key(parsed)
        if not replay and key in state["seen"]:
            continue
        # NOTE: 'seen' is marked AFTER a clean market read (below), not here —
        # a transient API failure during a live product must not permanently
        # discard it (2026-07-06 review: the sniper's top money path).
        new_parses.append((parsed, group))
        logger.info(f"{awips}: new CLI {parsed.summary_date} "
                    f"{'FINAL' if parsed.is_final else 'floor'} "
                    f"max={parsed.max_f} min={parsed.min_f}")

    if not new_parses:
        if not dry_run and not replay:
            _save_state(state)
        return

    client = KalshiClient(
        api_key_id=os.getenv("KALSHI_API_KEY_ID", ""),
        private_key_path=os.getenv("KALSHI_PRIVATE_KEY_PATH", ""),
        demo_mode=False,
    )
    opportunities: list[dict] = []
    await client.start()
    try:
        for parsed, group in new_parses:
            key = _seen_key(parsed)
            finality = effective_finality(parsed, group[0].tz, now_utc)
            if finality == "skip":
                logger.info(f"{parsed.awips}: same-day pre-afternoon product "
                            f"({parsed.summary_date}) — not classifiable")
                if not replay:
                    state["seen"][key] = now_utc.isoformat(timespec="seconds")
                if not dry_run:
                    # Journal the EFFECTIVE finality: DEN's pre-dawn same-day
                    # dailies (12:3xZ) carry no VALID TODAY line, so the raw
                    # regex marks them FINAL — and a false final for (awips,
                    # date) poisoned metar_sniper's cli_floor_crosscheck
                    # finals set, muting busts/confirms all day (2026-07-16).
                    _journal({"ts": now_utc.isoformat(timespec="seconds"),
                              **asdict(parsed), "is_final": False,
                              "skipped": "intraday", "findings": []}, now_utc)
                continue
            parsed.is_final = finality == "final"
            journal_entry = {"ts": now_utc.isoformat(timespec="seconds"),
                             **asdict(parsed), "findings": []}
            read_ok = True
            dsm_reports: list[dsm.DSMReport] | None = None  # lazy, once per product
            obs_pair: tuple[float | None, float | None] = (None, None)
            obs_trend: dict | None = None
            obs_checked = False                # lazy, once per station
            product_priced: list[dict] = []    # all priced findings, guard runs once
            for ladder in group:
                markets, ok = await client.get_markets_checked(series_ticker=ladder.series)
                if not ok:
                    read_ok = False
                    logger.warning(f"{ladder.series}: market read degraded — "
                                   f"{parsed.awips} left unseen for retry")
                    continue
                findings = classify(parsed, ladder, markets)
                # DSM veto — floor products only (finals already equal the DSM).
                if (not parsed.is_final
                        and any(f["kind"] == "buy_winner" for f in findings)):
                    if dsm_reports is None:
                        dsm_reports = await asyncio.to_thread(
                            dsm.fetch_dsm_reports, parsed.awips)
                        if not dsm_reports:
                            logger.warning(f"{parsed.awips}: DSM unavailable — "
                                           f"buy findings pass unchecked")
                    findings, vetoed = apply_dsm_veto(
                        findings, dsm_reports, parsed.summary_date)
                    for v in vetoed:
                        logger.warning(
                            f"{v['ticker']}: DSM VETO — CLI printed "
                            f"{v['printed']}° but DSM has {v['dsm_extreme']}° "
                            f"@ {v['dsm_time_lst']} LST")
                    journal_entry["findings"] += vetoed
                    opportunities += vetoed
                priced = await _price_findings(client, findings)
                if (not obs_checked
                        and any(p.get("kind") == "buy_winner" and not p["final"]
                                and p.get("ladder_kind") == "high"
                                and not p.get("suppressed") for p in priced)):
                    obs_checked = True
                    try:
                        timed = await asyncio.to_thread(
                            fetch_day_obs_timed, ladder.station_icao,
                            ZoneInfo(ladder.tz))
                        temps = [f for _, f in timed]
                        obs_pair = (corroborated_extreme(temps, "high"),
                                    max(temps) if temps else None)
                        obs_trend = trend_class(timed, now_utc)
                    except Exception as exc:  # noqa: BLE001 — annotation is fail-open
                        logger.warning(f"{parsed.awips}: obs fetch failed: {exc}")
                _annotate_obs_context(priced, *obs_pair, trend=obs_trend)
                product_priced += priced
            # Stage-time reissue guard: one archive fetch per product that
            # would offer a button (or print a runnable command) — a
            # strictly-newer same-class product whose extreme moved stamps
            # `reissue_conflict`: no button, info-only alert.
            if any(p.get("cmd") and not (p.get("obs_kill") or p.get("obs_warn")
                                         or p.get("wall_ask"))
                   for p in product_priced):
                archive = await asyncio.to_thread(
                    fetch_archive_products, parsed.awips)
                status, newer = newer_archive_product(
                    parsed, group[0].tz, now_utc, archive)
                if newer is not None:
                    n = apply_reissue_conflicts(product_priced, newer)
                    journal_entry["reissue_check"] = (
                        f"newer:{newer.stamp}:conflicts={n}")
                    if n:
                        logger.warning(
                            f"{parsed.awips}: REISSUE GUARD — newer product "
                            f"{newer.stamp} (max={newer.max_f} "
                            f"min={newer.min_f}) refutes {n} finding(s); "
                            f"staging suppressed")
                else:
                    journal_entry["reissue_check"] = status
            journal_entry["findings"] += [
                {k: v for k, v in f.items() if k != "cmd"}
                for f in product_priced]
            opportunities += [p for p in product_priced
                              if not p.get("suppressed")]
            # Untagged-reissue watch: NWS re-issues without a CORRECTED tag
            # (2026-07-16 BOS: min 51→69, correction field null). When a new
            # stamp moves an extreme prior findings were premised on, the
            # human may hold a position and a staged button may still be
            # live — supersede the buttons and mirror the correction path
            # with an exit-signal notice.
            prior = _prior_journaled_product(
                parsed.awips, parsed.summary_date, parsed.is_final, now_utc)
            moves = reissue_moves(prior, parsed, now_utc) if prior else {}
            if moves:
                retracted = 0
                if not dry_run and not replay:
                    retracted = await _retract_superseded(parsed, now_utc)
                if not parsed.correction:
                    notice = {"kind": "reissue_notice",
                              "ticker": f"REISSUE:{parsed.awips}"
                                        f":{parsed.summary_date}:{parsed.stamp}",
                              "awips": parsed.awips,
                              "summary_date": parsed.summary_date,
                              "prior_stamp": prior.get("stamp"),
                              "stamp": parsed.stamp,
                              "moves": {k: list(v) for k, v in moves.items()},
                              "retracted": retracted,
                              "final": parsed.is_final}
                    journal_entry["findings"].append(notice)
                    opportunities.append(notice)
                    logger.warning(
                        f"{parsed.awips}: untagged REISSUE for "
                        f"{parsed.summary_date} — "
                        + ", ".join(f"{k} {v[0]}→{v[1]}"
                                    for k, v in moves.items())
                        + (f"; {retracted} button(s) retracted"
                           if retracted else ""))
            # A corrected re-issue of a product we previously found money on
            # is alert-worthy even when nothing re-classifies (e.g. a value
            # scrubbed to MM removes every finding) — the human may hold a
            # position premised on the ORIGINAL print.
            if (parsed.correction
                    and _journal_has_findings(parsed.awips, parsed.summary_date,
                                              now_utc)):
                notice = {"kind": "correction_notice",
                          "ticker": f"CORR:{parsed.awips}:{parsed.summary_date}"
                                    f":{parsed.stamp}:{parsed.correction}",
                          "awips": parsed.awips,
                          "summary_date": parsed.summary_date,
                          "corrected": parsed.correction,
                          "final": parsed.is_final,
                          "max_f": parsed.max_f, "min_f": parsed.min_f}
                journal_entry["findings"].append(notice)
                opportunities.append(notice)
                logger.warning(f"{parsed.awips}: CORRECTION {parsed.correction} "
                               f"for {parsed.summary_date} — max={parsed.max_f} "
                               f"min={parsed.min_f}")
            # Mark seen only on a clean sweep — a degraded read leaves the
            # product for the next */2 cron to retry.
            if read_ok and not replay:
                state["seen"][key] = now_utc.isoformat(timespec="seconds")
            if not dry_run:
                _journal(journal_entry, now_utc)
    finally:
        await client.stop()

    # A corrected product bypasses the 48h ticker dedup: the prior alert was
    # about a value that may no longer exist.
    fresh = [o for o in opportunities
             if replay or o.get("corrected")
             or o["ticker"] not in state["alerted"]]
    if dry_run or replay:
        if fresh:
            title, body = format_alert(fresh)
            print(title)
            print(body)
        else:
            print("no actionable opportunities")
        return

    if fresh:
        title, body = format_alert(fresh)
        # Phone-push (@mention) only when something is actually takeable —
        # trap-stamped or suppressed findings stay silent embeds.
        actionable = any(
            o["kind"] in ("correction_notice", "reissue_notice")
            or (o["kind"] in ("buy_winner", "sell_dead")
                and not (o.get("suppressed") or o.get("obs_kill")
                         or o.get("obs_warn") or o.get("wall_ask")
                         or o.get("reissue_conflict")))
            for o in fresh)
        try:
            from notifications import send_discord_alert

            await send_discord_alert(title=title, description=body[:4096],
                                     color=0x2ECC71, context="cli_sniper",
                                     mention=actionable)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"discord alert failed: {exc}")
        try:
            from core import take_queue

            staged = take_queue.enqueue_findings(fresh, source="cli_sniper",
                                                 now_utc=now_utc)
            if staged:
                logger.info(f"take queue: staged {staged} command(s) "
                            f"for one-tap approve")
                # Post the button NOW — the floor race reprices inside the
                # 0-60s wait for the approver's next tick (2026-07-14 ×3).
                from take_approver import post_new_entries

                await post_new_entries()
        except Exception as exc:  # noqa: BLE001 — staging must not break alerting
            logger.warning(f"take queue enqueue failed: {exc}")
        for o in fresh:
            # .get: notice kinds (correction/reissue) carry no printed value
            # — o["printed"] here crashed the run after the alert but before
            # the state save, re-alerting every product each */2 tick.
            state["alerted"][o["ticker"]] = {
                "ts": now_utc.isoformat(timespec="seconds"),
                "printed": o.get("printed")}
        logger.info(f"cli sniper: alerted {len(fresh)} opportunity(ies)")
    _save_state(state)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--once", action="store_true", help="single pass (cron mode)")
    ap.add_argument("--dry-run", action="store_true", help="print, no Discord/state")
    ap.add_argument("--replay", metavar="AWIPS[:VER]",
                    help="run the pipeline on a station's latest (or VER-back) product")
    args = ap.parse_args()
    if not args.once and not args.replay:
        ap.error("use --once (cron) or --replay AWIPS")
    # Single-instance run lock: a slow run (NWS timeouts across overlapping
    # windows) must not overlap the next */2 tick — overlapping instances
    # double-fire Discord and clobber each other's seen/alerted state (the
    # save is a whole-dict write). Locked-out runs exit WITHOUT heartbeating
    # so a genuinely hung instance still trips the watchdog.
    import fcntl
    with (PROJECT_ROOT / ".cli_sniper.lock").open("w") as lock_fd:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info("cli sniper: previous run still active — skipping")
            return
        asyncio.run(run(args.dry_run, args.replay))
        write_heartbeat("cli_sniper")


if __name__ == "__main__":
    main()
