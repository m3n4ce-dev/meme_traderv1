"""The call ledger: every call, timestamped, hash-chained, and scored honestly afterwards.

A call is a claim made before the outcome is known: "SYMBOL at $X market cap". Yours come from the 📣 Call button;
the bot's own entries are recorded too. Each record carries the SHA-256 of the record before it, so the file can't
be backdated, reordered or trimmed without breaking every hash after the change. Publishing the newest hash (on X,
Telegram, or in a public repo) pins everything before it: anyone holding the file can re-check it against what was
published, with `python -m meme_trader.sniper calls verify`.

Outcomes are measured from market data, not entered: the market cap 5 minutes, 1 hour, 6 hours and 24 hours after
the call, and the highest and lowest seen in 24 hours. Groups usually quote the peak ("8x from call"); nobody sells
the exact top, so the record leads with what holding for a fixed time would have returned, after typical costs.

Files: data/calls.jsonl (the chain, append-only) and data/calls_outcomes.json (measurements, recomputable).
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import secrets
import statistics
import time
from pathlib import Path

GENESIS = "0" * 64
HORIZONS = (("5m", 300), ("1h", 3600), ("6h", 21600), ("24h", 86400))
TRACK_S = 86400
COST_PCT = 5.0                 # typical pump.fun round trip: fees + slippage, in and out
HIT_X = 2.0                    # a "hit": the price at least doubled at some point within 24 h


def _canon(rec: dict) -> str:
    return json.dumps({k: v for k, v in rec.items() if k != "hash"}, sort_keys=True, separators=(",", ":"))


def chain_hash(prev: str, rec: dict) -> str:
    return hashlib.sha256((prev + _canon(rec)).encode()).hexdigest()


class LedgerError(ValueError):
    pass


class CallLedger:
    def __init__(self, path: Path | None, outcomes_path: Path | None = None):
        self.path = Path(path) if path else None
        self.outcomes_path = Path(outcomes_path) if outcomes_path else (
            self.path.with_name("calls_outcomes.json") if self.path else None)
        self.records: list[dict] = []
        self.outcomes: dict[str, dict] = {}
        self._size = 0
        self._reload()
        if self.outcomes_path and self.outcomes_path.exists():
            try:
                self.outcomes = json.loads(self.outcomes_path.read_text())
            except ValueError:
                self.outcomes = {}
        self._dirty = False

    def _reload(self) -> None:
        if self.path and self.path.exists():
            raw = self.path.read_bytes()
            self.records = [json.loads(line) for line in raw.decode().splitlines() if line.strip()]
            self._size = len(raw)

    # ---------------------------------------------------------------- the chain
    @property
    def head(self) -> str:
        return self.records[-1]["hash"] if self.records else GENESIS

    def _append(self, rec: dict) -> dict:
        if not self.path:
            rec = {**rec, "n": len(self.records) + 1, "prev": self.head}
            rec["hash"] = chain_hash(rec["prev"], rec)
            self.records.append(rec)
            return rec
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)                # one writer at a time (the bot and the command line)
            try:
                if self.path.stat().st_size != self._size:
                    self._reload()                       # someone else appended: chain onto their record
                rec = {**rec, "n": len(self.records) + 1, "prev": self.head}
                rec["hash"] = chain_hash(rec["prev"], rec)
                line = json.dumps(rec, sort_keys=True) + "\n"
                f.write(line)
                f.flush()
                self.records.append(rec)
                self._size += len(line.encode())
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        return rec

    def call(self, mint: str, symbol: str, caller: str, mcap_usd: float, price_sol: float, sol_usd: float,
             thesis: str = "", source: str = "", mode: str = "", stage: str = "curve", ts: float | None = None) -> dict:
        if not 32 <= len(mint or "") <= 44:
            raise LedgerError("that isn't a contract address")
        if not mcap_usd or mcap_usd <= 0:
            raise LedgerError("no market cap for it yet: wait for its first trade")
        rec = self._append({"type": "call", "id": secrets.token_hex(5), "ts": round(ts or time.time(), 3),
                            "mint": mint, "symbol": (symbol or mint[:6])[:24], "caller": caller[:40],
                            "source": source[:40], "mode": mode, "stage": stage,
                            "mcap_usd": round(mcap_usd, 2), "price_sol": price_sol, "sol_usd": round(sol_usd, 4),
                            "thesis": " ".join((thesis or "").split())[:280]})
        self.outcomes[rec["id"]] = {"peak": mcap_usd, "low": mcap_usd, "last": mcap_usd, "last_ts": rec["ts"]}
        self._dirty = True
        return rec

    def anchor(self, where: str, url: str = "") -> dict:
        """Record that the head was published somewhere (the next hash covers it)."""
        return self._append({"type": "anchor", "ts": round(time.time(), 3), "where": where[:40], "url": url[:300],
                             "anchored": self.head, "calls": len(self.calls())})

    def verify(self) -> dict:
        prev = GENESIS
        for i, rec in enumerate(self.records):
            if rec.get("prev") != prev or rec.get("n") != i + 1 or chain_hash(prev, rec) != rec.get("hash"):
                return {"ok": False, "records": len(self.records), "broken_at": i + 1,
                        "text": f"broken at record {i + 1}: it was changed, removed or reordered"}
            prev = rec["hash"]
        return {"ok": True, "records": len(self.records), "calls": len(self.calls()), "head": prev,
                "text": f"chain intact: {len(self.records)} records, {len(self.calls())} calls, head {prev[:12]}…"}

    def calls(self) -> list[dict]:
        return [r for r in self.records if r.get("type") == "call"]

    # ---------------------------------------------------------------- outcomes
    def open_calls(self, now: float) -> list[dict]:
        out = []
        for r in reversed(self.records):                  # newest first; stop at the first one past tracking
            if now - r["ts"] >= TRACK_S + 600:
                break
            if r.get("type") == "call":
                out.append(r)
        return out

    def observe(self, call_id: str, now: float, mcap_usd: float | None, graduated: bool = False) -> None:
        c = next((r for r in self.records if r.get("id") == call_id), None)
        if c is None or not mcap_usd or mcap_usd <= 0:
            return
        o = self.outcomes.setdefault(call_id, {"peak": c["mcap_usd"], "low": c["mcap_usd"]})
        age = now - c["ts"]
        if age <= TRACK_S:
            o["peak"] = max(o.get("peak", 0), mcap_usd)
            o["low"] = min(o.get("low", mcap_usd), mcap_usd)
        for label, secs in HORIZONS:
            if label not in o and age >= secs:
                o[label] = mcap_usd          # the first measurement at or after the horizon
        o["last"], o["last_ts"] = mcap_usd, now
        if graduated:
            o["graduated"] = True
        self._dirty = True

    def save(self) -> None:
        if not (self._dirty and self.outcomes_path):
            return
        self.outcomes_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.outcomes_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.outcomes))
        tmp.replace(self.outcomes_path)
        self._dirty = False

    def rows(self, caller: str = "", limit: int = 200) -> list[dict]:
        out = []
        for c in reversed(self.calls()):
            if caller and not c["caller"].startswith(caller):
                continue
            o = self.outcomes.get(c["id"], {})
            m0 = c["mcap_usd"]
            row = {**c, "peak_x": o.get("peak", m0) / m0, "low_pct": (o.get("low", m0) / m0 - 1) * 100,
                   "now_x": o.get("last", m0) / m0, "graduated": bool(o.get("graduated"))}
            for label, _ in HORIZONS:
                row[label] = (o[label] / m0 - 1) * 100 if label in o else None
            out.append(row)
            if len(out) >= limit:
                break
        return out

    def stats(self, caller: str = "", now: float | None = None) -> dict:
        """The record, honestly: returns at fixed horizons after costs first, the peak multiple second."""
        now = now or time.time()
        rows = self.rows(caller, limit=100_000)
        out = {"calls": len(rows), "caller": caller or "everyone", "cost_pct": COST_PCT, "hit_x": HIT_X}
        for label, secs in HORIZONS:
            xs = [r[label] - COST_PCT for r in rows if r[label] is not None]
            out[label] = None if not xs else {
                "n": len(xs), "mean_pct": statistics.fmean(xs), "median_pct": statistics.median(xs),
                "win_rate": sum(1 for x in xs if x > 0) / len(xs)}
        done = [r for r in rows if now - r["ts"] >= TRACK_S]
        peaks = [r["peak_x"] for r in done] or [r["peak_x"] for r in rows]
        out["peak"] = None if not peaks else {
            "n": len(peaks), "settled": len(done), "median_x": statistics.median(peaks),
            "hit_rate": sum(1 for x in peaks if x >= HIT_X) / len(peaks), "best_x": max(peaks)}
        out["callers"] = sorted({r["caller"] for r in rows})
        return out


def proof_text(ledger: CallLedger) -> str:
    return (f"Call ledger #{len(ledger.records)}: {len(ledger.calls())} calls, every one timestamped and "
            f"hash-chained.\nHead: {ledger.head}\n(published so the record can't be rewritten later)")
