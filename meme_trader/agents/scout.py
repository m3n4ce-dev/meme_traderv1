"""Scout agent: finds fresh Solana tokens and attaches market data."""
from __future__ import annotations

from ..clients import dexscreener
from ..journal import record
from ..models import Candidate

SOURCES = {
    "dexscreener_profiles": dexscreener.latest_profiles,
    "dexscreener_boosts": dexscreener.latest_boosts,
}


def scan(params, exclude: set[str]) -> list[Candidate]:
    mints: dict[str, str] = {}
    for name in params.scout.sources:
        try:
            for m in SOURCES[name]():
                mints.setdefault(m, name)
        except Exception as e:  # one dead source shouldn't stop the cycle
            record("scout", "source_error", source=name, error=str(e))
    todo = [m for m in mints if m not in exclude][: params.scout.max_candidates_per_cycle]
    if not todo:
        return []
    pairs = dexscreener.best_pair_by_mint(todo)
    out = [dexscreener.to_candidate(p, mints[m]) for m, p in pairs.items()]
    record("scout", "scan", found=len(mints), checked=len(todo), with_sol_pair=len(out))
    return out
