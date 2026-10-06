"""The AI team's track record: every vote it cast on a graduation coin, scored by what that coin did next on the
bot's own exits (the exit lab's "as now" rule, the same yardstick for a buy and a pass).

Three kinds of vote count:
  * live      the team decides the graduation buys (desk.graduation_vote, the default);
  * shadow    the rules decide, and the team votes on the side (desk.graduation_vote: false);
  * practice  the graduation play is off, so nothing is bought, and the team still votes (desk.practice).

Graduation calls and early-sniper calls are kept apart ("strategy": late | sniper): what makes a good coin differs.

An LLM can't change its weights, so this is how the team learns: each persona reads its own record and its latest
scored calls at every vote, and the meetings read the whole scorecard.
"""
from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

FOLLOWED = ("late", "desk-pass", "practice-buy", "practice-pass",   # exit-lab kinds that score a vote: graduation ...
            "sniper", "sniper-pass", "sniper-skip")                  # ... and the early sniper


def stat(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0}
    return {"n": len(xs), "avg_pct": round(statistics.fmean(xs), 1), "median_pct": round(statistics.median(xs), 1),
            "won_pct": round(100 * sum(1 for x in xs if x > 0) / len(xs))}


def call_votes(votes) -> list[list]:
    """[persona, vote, conviction, first reason] for each vote that didn't fail (Vote objects or their dicts)."""
    out = []
    for v in votes:
        d = v if isinstance(v, dict) else vars(v)
        if d.get("error"):
            continue
        out.append([d["persona"], d["vote"], int(d.get("conviction") or 0), str((d.get("reasons") or [""])[0])[:90]])
    return out


def _of(calls: list[dict], strategy: str | None) -> list[dict]:
    return calls if strategy is None else [c for c in calls if c.get("strategy", "late") == strategy]


def scorecard(calls: list[dict], personas, strategy: str | None = None) -> dict:
    """The team's buy calls against its passes, each persona's too, and how many calls of each kind (strategy: late |
    sniper | None for all; records from before strategies were kept are graduation calls)."""
    calls = _of(calls, strategy)
    out = {"calls": len(calls), "buy": stat([c["pnl_pct"] for c in calls if c["approve"]]),
           "pass": stat([c["pnl_pct"] for c in calls if not c["approve"]]),
           "by_mode": dict(Counter(c.get("mode", "live") for c in calls)), "personas": {}}
    for p in personas:
        out["personas"][p] = {"buy": stat([c["pnl_pct"] for c in calls for v in c["votes"] if v[0] == p and v[1] == "buy"]),
                              "pass": stat([c["pnl_pct"] for c in calls for v in c["votes"] if v[0] == p and v[1] == "pass"])}
    return out


def persona_record(calls: list[dict], persona: str, last: int = 6, strategy: str | None = None) -> dict | None:
    """What one persona reads at a vote: how its buys and passes turned out on this strategy, and its latest calls."""
    mine = [(c, v) for c in _of(calls, strategy) for v in c["votes"] if v[0] == persona]
    if len(mine) < 8:
        return None
    return {"what_this_is": "how your past votes on this desk turned out: what each coin did next on the bot's own exits",
            "when_you_said_buy": stat([c["pnl_pct"] for c, v in mine if v[1] == "buy"]),
            "when_you_said_pass": stat([c["pnl_pct"] for c, v in mine if v[1] == "pass"]),
            "latest": [{"coin": c["symbol"], "you": f"{v[1]} {v[2]}", "your_reason": v[3], "then": f"{c['pnl_pct']:+.0f}%"}
                       for c, v in mine[-last:]]}


def load(path: Path, keep: int = 2000) -> list[dict]:
    try:
        return [json.loads(line) for line in path.read_text().splitlines()[-keep:] if line.strip()]
    except (OSError, ValueError):
        return []


def backfill(data: Path) -> list[dict]:
    """The record from before it was kept: the journal's votes, each matched to the exit lab's "as now" row for the
    same coin (the bot's own trade when the team approved, the desk-pass follow when it passed)."""
    lab: dict[str, list[dict]] = defaultdict(list)
    try:
        for line in (data / "exit_lab.jsonl").read_text().splitlines():
            x = json.loads(line)
            if x.get("variant") == "as now" and x.get("kind") in ("late", "desk-pass"):
                lab[x["mint"]].append(x)
    except (OSError, ValueError):
        return []
    calls = []
    for f in sorted(data.glob("journal-*.jsonl")):
        for line in f.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("event") != "desk" or not r.get("votes"):
                continue
            approve = "APPROVED" in r.get("text", "")
            kind = "late" if approve else "desk-pass"
            row = next((x for x in lab.get(r.get("mint", ""), []) if x["kind"] == kind
                        and r["ts"] - 5 <= x["opened"] <= r["ts"] + 120), None)
            votes = call_votes(r["votes"])
            if row is None or not votes:
                continue
            calls.append({"ts": r["ts"], "mint": r["mint"], "symbol": r["text"].split(":")[0][:20], "mode": "live", "strategy": "late",
                          "approve": approve, "votes": votes, "pnl_pct": row["pnl_pct"], "held_s": row.get("held_s"),
                          "exit": row.get("why", "")})
    return sorted(calls, key=lambda c: c["ts"])
