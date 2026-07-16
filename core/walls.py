"""Certainty-wall detection from shadow-book L2 levels.

The 2026-07-06 MIA episode exposed the adversary's signature: ~2,500-contract
bids stacked every 3¢ down the YES side of the market's favorite — informed
size defending a settlement thesis (competitor dossier, 2026-07-07). A wall's
presence, side, and first-seen time are tradeable context: dead brackets
guarded by a wall are not prey, and a wall that arrives BEFORE the public data
that justifies it marks a station where faster/insider flow operates.

Pure functions over shadow_logger rows ({yes,no}_levels = [[cents, qty], ...]).
"""
from __future__ import annotations

# A single resting level this big is a wall on its own (retail lots are 1-3
# digit; the MIA ladder ran 2,500/level).
WALL_SINGLE_LEVEL_MIN = 1000.0
# ...or a laddered wall: this many levels of at least this size each.
WALL_LADDER_LEVELS = 3
WALL_LADDER_LEVEL_MIN = 500.0

# A deep top-of-book ask on the ENTRY side of a "winner" — the snipers'
# buy-path trap flag (5-0 vs floor signals through 2026-07-12: MIA, MSP×2,
# DAL, AUS). Distinct from the resting-BID structure detect_wall() measures
# on shadow-book L2 (adversary intel): one asks "who is selling certainty
# into my entry", the other "who is defending a settlement thesis". Same
# adversary, different signature — do not merge the thresholds.
WALL_ASK_DEPTH = 10_000


def detect_wall(levels: list | None) -> dict | None:
    """Wall metrics for one side's resting bids; None when no wall.

    `levels` is [[price_cents, qty], ...] (top-of-book first or not — order
    is not assumed). Returns {"total", "max_level", "ladder_levels", "band"}.
    """
    lvls = [(float(c), float(q)) for c, q in (levels or []) if q]
    if not lvls:
        return None
    total = sum(q for _, q in lvls)
    max_level = max(q for _, q in lvls)
    ladder_levels = sum(1 for _, q in lvls if q >= WALL_LADDER_LEVEL_MIN)
    is_wall = (max_level >= WALL_SINGLE_LEVEL_MIN
               or ladder_levels >= WALL_LADDER_LEVELS)
    if not is_wall:
        return None
    prices = [c for c, _ in lvls]
    band = [min(prices), max(prices)]
    # Penny farms (huge size parked ≤10¢ fishing for lottery fills) are noise;
    # a defended settlement thesis rests at conviction prices (≥50¢). The MIA
    # 7/6 book had both: 2,500/3¢ YES rungs at 85-92¢ (defense) vs 173k of
    # 1-2¢ NO bids (farm).
    if band[1] <= 10:
        kind = "penny_farm"
    elif band[0] >= 50:
        kind = "defense"
    else:
        kind = "mid"
    return {
        "total": round(total, 1),
        "max_level": round(max_level, 1),
        "ladder_levels": ladder_levels,
        "band": band,
        "kind": kind,
    }


def scan_rows(rows: list[dict]) -> dict[str, dict]:
    """ticker -> latest wall state + first-seen times, from shadow rows.

    Rows must be a single venue's live rows in ascending ts order (the
    shadow journal's natural order). first_seen_* is the earliest snapshot
    where that side's wall was present.
    """
    out: dict[str, dict] = {}
    for row in rows:
        ticker = row.get("ticker")
        if not ticker:
            continue
        yes_wall = detect_wall(row.get("yes_levels"))
        no_wall = detect_wall(row.get("no_levels"))
        rec = out.setdefault(ticker, {
            "series": row.get("series"), "target_date": row.get("target_date"),
            "first_seen_yes": None, "first_seen_no": None,
        })
        if yes_wall and rec["first_seen_yes"] is None:
            rec["first_seen_yes"] = row.get("ts")
        if no_wall and rec["first_seen_no"] is None:
            rec["first_seen_no"] = row.get("ts")
        rec.update(
            ts=row.get("ts"), yes_wall=yes_wall, no_wall=no_wall,
            yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"),
            vol24=row.get("vol24"),
        )
    # Only tickers whose LATEST snapshot still has a wall are interesting.
    return {t: r for t, r in out.items() if r.get("yes_wall") or r.get("no_wall")}
