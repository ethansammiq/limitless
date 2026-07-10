"""Floor→final drift model for CLI afternoon prints.

The afternoon CLI prints the observed max "AS OF 4 PM" — a floor. The final
(next morning's product) can only match or exceed it. Measured live
2026-07-09 across 84 station-day pairs from our own journal: final == floor
85.7%, +1°F 11.9%, +2°F 2.4%. Every one of that night's manual losses came
from trading a bracket the drift distribution had already priced; the module
exists so the sniper quantifies that instead of a human guessing.

The distribution is recomputed from the journal on every load (n grows
daily); the 2026-07-09 numbers above are the frozen reference, not a
hardcoded prior.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from core.fees import kalshi_taker_fee_cents

# Below this many settled pairs the distribution is noise — refuse to price.
MIN_PAIRS = 30


@dataclass(frozen=True)
class DriftDist:
    """Counts of final-minus-floor outcomes for high-ladder station-days."""
    same: int      # final <= floor (floor held; <0 only via correction)
    up1: int       # final = floor + 1
    up2: int       # final >= floor + 2 (both observed cases were exactly +2)

    @property
    def n(self) -> int:
        return self.same + self.up1 + self.up2

    def p(self, bucket: str) -> float:
        return getattr(self, bucket) / self.n if self.n else 0.0


def load_pairs(journal_dir: Path) -> list[tuple[str, str, int, int]]:
    """(awips, summary_date, floor, final) for station-days with both an
    afternoon floor print and a final, skipping intraday-skipped entries."""
    by_key: dict[tuple[str, str], dict] = {}
    for path in sorted(journal_dir.glob("*.jsonl")):
        for line in path.read_text().splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("max_f") is None or e.get("skipped"):
                continue
            k = (e.get("awips"), e.get("summary_date"))
            if None in k:
                continue
            slot = by_key.setdefault(k, {})
            if e.get("is_final"):
                slot["final"] = e["max_f"]     # last final wins (corrections)
            else:
                # FIRST pre-final print = the floor as of the alert. Using the
                # highest re-issued floor instead measured 94% "same" on the
                # same journal that gives 86% first-print — optimistic by
                # exactly the drift a first-print trader eats. The alert fires
                # on the first print, so the model prices that moment.
                slot.setdefault("floor", e["max_f"])
    return [(aw, sd, v["floor"], v["final"])
            for (aw, sd), v in sorted(by_key.items())
            if "floor" in v and "final" in v]


def distribution(pairs: list[tuple[str, str, int, int]]) -> DriftDist:
    same = up1 = up2 = 0
    for _, _, floor, final in pairs:
        diff = final - floor
        if diff <= 0:
            same += 1
        elif diff == 1:
            up1 += 1
        else:
            up2 += 1
    return DriftDist(same=same, up1=up1, up2=up2)


def bracket_win_prob(floor: int, lo: float | None, hi: float | None,
                     dist: DriftDist) -> float | None:
    """P(final lands in [lo, hi]) for a bracket, given the printed floor.

    None when the sample is too small to mean anything (MIN_PAIRS) or the
    bracket doesn't contain the floor (drift only moves up — a bracket below
    the floor is dead, above it is a forecast bet; neither is this model).
    """
    if dist.n < MIN_PAIRS:
        return None
    lo_v = float("-inf") if lo is None else lo
    hi_v = float("inf") if hi is None else hi
    if not (lo_v <= floor <= hi_v):
        return None
    prob = 0.0
    for bucket, offset in (("same", 0), ("up1", 1), ("up2", 2)):
        if lo_v <= floor + offset <= hi_v:
            prob += dist.p(bucket)
    return prob


def ev_cents(prob: float, ask_c: int) -> float:
    """Expected value per contract, after the taker fee, buying YES at ask."""
    return prob * 100 - ask_c - kalshi_taker_fee_cents(ask_c)
