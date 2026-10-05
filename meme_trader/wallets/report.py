"""The wallet study at a glance, for the dashboard's Analytics tab: how far period A is, the moves found, the
wallets qualifying so far, and which of them look like one trader (they keep buying the same coins within
seconds of each other). Read-only: it runs the study's own selection, never freezes or changes anything.

Run as a separate process (python -m meme_trader.wallets view) so its memory goes away when it's done."""
from __future__ import annotations

import collections
import json
import time
from pathlib import Path

from . import study
from .recorder import DATA

TOGETHER_S = 5          # first buys this close together count as "in the same moment"
LINK_MIN_TOGETHER = 3   # this many coins bought in the same moment links two wallets: one operator, or bots
                        # copying the same leader. Either way not independent (they also share coins hours apart)


def clusters(d: study.Data, wallets: list[str], min_sol: float) -> tuple[list[list[str]], list[dict]]:
    """Groups of listed wallets that look like one operator, and the pair statistics behind them."""
    first: dict[str, dict[str, float]] = {w: {} for w in wallets}
    for pool, rs in d.by_pool.items():
        for t, w, buy, sol, _px in rs:
            if buy and sol >= min_sol and w in first and pool not in first[w]:
                first[w][pool] = t
    parent = {w: w for w in wallets}

    def root(w):
        while parent[w] != w:
            parent[w] = parent[parent[w]]
            w = parent[w]
        return w
    pairs = []
    for i, a in enumerate(wallets):
        for b in wallets[i + 1:]:
            shared = first[a].keys() & first[b].keys()
            if not shared:
                continue
            together = sum(1 for p in shared if abs(first[a][p] - first[b][p]) <= TOGETHER_S)
            pairs.append({"a": a, "b": b, "shared": len(shared), "together": together})
            if together >= LINK_MIN_TOGETHER:
                parent[root(a)] = root(b)
    groups: dict[str, list[str]] = collections.defaultdict(list)
    for w in wallets:
        groups[root(w)].append(w)
    return sorted((g for g in groups.values() if len(g) > 1), key=len, reverse=True), \
        sorted(pairs, key=lambda p: -p["together"])


def build(policy: str = "wallets-v1", data_dir: Path = DATA) -> dict:
    pol = study.load_policy(policy)
    lock = study.checked_lock(pol)
    sel = pol["selection"]
    end = study._ts(lock["frozen_at"]) if lock.get("frozen_at") else None
    d = study.load_data(study.trade_files(data_dir), end=end, min_sol=sel["min_price_sol"], coin_cap=sel["max_coins"])
    res = study.select_wallets(d, sel)
    listed = [r["wallet"] for r in res["wallets"]]
    groups, pairs = clusters(d, listed, sel["min_price_sol"])     # every priced buy, not just the 0.2 SOL "early" size
    gid = {w: i + 1 for i, g in enumerate(groups) for w in g}
    for r in res["wallets"]:
        r["cluster"] = gid.get(r["wallet"])
    reg = study._ts(lock["registered_at"])
    status = {}
    sp = Path(data_dir) / "status.json"
    if sp.exists():
        try:
            status = json.loads(sp.read_text())
        except ValueError:
            pass
    return {
        "generated": time.time(), "policy": policy, "rules_signature": lock.get("rules_signature"),
        "frozen": bool(lock.get("frozen_at")), "registered": reg, "days": round(res["days"], 2),
        "period_days": sel.get("period_days", 14), "freeze_after": reg + 14 * study.DAY,
        "verdict_after": reg + 28 * study.DAY, "need": sel.get("min_wallets", 10),
        "moves": res["moves"], "qualified": res["qualified"], "near_misses": res["near_misses"],
        "bots_excluded": res["bots_excluded"], "control": len(res["control"]),
        "wallets": res["wallets"], "clusters": groups, "pairs": pairs[:20],
        "recent_moves": [{"sym": m["sym"], "mint": m["mint"], "x": round(m["x"], 1), "t_low": m["t_low"], "t_hit": m["t_hit"]}
                         for m in res["move_list"][-12:][::-1]],
        "recorder": {k: status.get(k) for k in ("active", "pause_reason", "mode", "coverage", "running_h", "trades_run")
                     if k in status},
        "together_s": TOGETHER_S,
    }
