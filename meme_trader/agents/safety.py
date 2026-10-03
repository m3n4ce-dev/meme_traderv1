"""Safety agent: hard rug/scam gates. Any failure rejects the token."""
from __future__ import annotations

from ..models import Candidate, SafetyReport


def evaluate(c: Candidate, rc: dict, s) -> SafetyReport:
    """c: candidate, rc: RugCheck report, s: params.safety"""
    fails: list[str] = []

    if rc.get("rugged"):
        fails.append("rugcheck: marked rugged")
    if s.require_mint_authority_revoked and rc.get("mintAuthority"):
        fails.append("mint authority not revoked")
    if s.require_freeze_authority_revoked and rc.get("freezeAuthority"):
        fails.append("freeze authority not revoked")

    score = rc.get("score_normalised", rc.get("score"))
    if score is None:
        fails.append("rugcheck: no score")
    elif score > s.max_rugcheck_score:
        fails.append(f"rugcheck score {score} > {s.max_rugcheck_score}")
    if s.reject_danger_risks:
        dangers = [r.get("name", "?") for r in rc.get("risks") or [] if r.get("level") == "danger"]
        if dangers:
            fails.append("danger risks: " + ", ".join(dangers))

    # Holder concentration, excluding the AMM pool accounts themselves.
    pools = {c.pair_address} | {m.get("pubkey") for m in rc.get("markets") or []}
    holders = [h for h in rc.get("topHolders") or [] if h.get("owner") not in pools and h.get("address") not in pools]
    top10 = sum(float(h.get("pct") or 0) for h in holders[:10])
    top1 = max((float(h.get("pct") or 0) for h in holders), default=0.0)
    if top10 > s.max_top10_holder_pct:
        fails.append(f"top10 holders {top10:.1f}% > {s.max_top10_holder_pct}%")
    if top1 > s.max_single_holder_pct:
        fails.append(f"largest holder {top1:.1f}% > {s.max_single_holder_pct}%")
    total_holders = int(rc.get("totalHolders") or 0)
    if total_holders < s.min_holders:
        fails.append(f"holders {total_holders} < {s.min_holders}")

    lp_locked = max((float((m.get("lp") or {}).get("lpLockedPct") or 0) for m in rc.get("markets") or []), default=0.0)
    if lp_locked < s.min_lp_locked_pct:
        fails.append(f"LP locked {lp_locked:.0f}% < {s.min_lp_locked_pct}%")

    if c.liquidity_usd < s.min_liquidity_usd:
        fails.append(f"liquidity ${c.liquidity_usd:,.0f} < ${s.min_liquidity_usd:,.0f}")
    if c.age_minutes < s.min_token_age_minutes:
        fails.append(f"too new ({c.age_minutes:.0f}m)")
    if c.age_minutes > s.max_token_age_hours * 60:
        fails.append(f"too old ({c.age_minutes / 60:.1f}h)")

    return SafetyReport(c.mint, not fails, fails, raw={"score": score, "top10_pct": top10, "holders": total_holders,
                                                      "lp_locked_pct": lp_locked})
