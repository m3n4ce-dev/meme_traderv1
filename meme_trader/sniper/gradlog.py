"""Graduated coins, after the bonding curve: each one's 1-minute candles, fetched once, some hours after it graduates.

The feed records trades on the bonding curve only, so the bot has no history of what coins do in their pool after
graduating, a slower market with lower fees (where a 2.5 s fill matters less). This builds that history: when a coin
graduates it is queued; `after_h` later one DexScreener call (up to 30 coins) finds its pool and one GeckoTerminal call
fetches up to 1000 one-minute candles through the shared Chains pacing (a 429 waits for everyone). Rows go to
data/graduated-YYYY-MM-DD.jsonl. Measurement only: nothing is traded on it.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

CANDLES = 1000                  # GeckoTerminal's most per call: 16.7 h of minutes


class GradLog:
    def __init__(self, data_dir: Path | None, after_h: float = 6.0, per_day: int = 2000, every_s: float = 40.0):
        self.dir = Path(data_dir) if data_dir else None
        self.after_s, self.per_day, self.every_s = after_h * 3600, per_day, every_s
        self.queue: dict[str, dict] = {}
        self.day, self.done_today, self.failed_today = "", 0, 0
        self._last = 0.0
        self._busy = False
        if self.dir:
            try:
                self.queue = json.loads((self.dir / "graduated_queue.json").read_text())
            except (OSError, ValueError):
                self.queue = {}

    def _save_queue(self) -> None:
        if self.dir:
            tmp = self.dir / "graduated_queue.tmp"
            tmp.write_text(json.dumps(self.queue))
            tmp.replace(self.dir / "graduated_queue.json")

    def add(self, mint: str, symbol: str, ts: float, mcap_usd: float) -> None:
        if mint in self.queue or len(self.queue) > 20_000:
            return
        self.queue[mint] = {"mint": mint, "symbol": symbol, "graduated_ts": ts, "mcap_usd": round(mcap_usd or 0), "tries": 0}
        self._save_queue()

    def view(self) -> dict:
        return {"queued": len(self.queue), "logged_today": self.done_today, "failed_today": self.failed_today,
                "next_due_s": round(min((q["graduated_ts"] + self.after_s for q in self.queue.values()), default=0) - time.time())
                if self.queue else None}

    def tick(self, now: float, chains) -> None:
        """Called from the engine's loop: at most one fetch every `every_s`, off the loop, never two at once."""
        if self._busy or chains is None or now - self._last < self.every_s:
            return
        due = [q for q in self.queue.values() if now - q["graduated_ts"] >= self.after_s]
        if not due:
            return
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        if day != self.day:
            self.day, self.done_today, self.failed_today = day, 0, 0
        if self.done_today >= self.per_day:
            return
        self._last, self._busy = now, True
        asyncio.create_task(self._run(sorted(due, key=lambda q: q["graduated_ts"]), chains))

    async def _run(self, due: list[dict], chains) -> None:
        try:
            need = [q["mint"] for q in due if not q.get("pool")][:30]
            if need:
                from ..clients.dexscreener import best_pair_by_mint
                try:
                    pairs = await asyncio.get_running_loop().run_in_executor(None, best_pair_by_mint, need)
                except Exception:
                    pairs = {}
                for m in need:
                    q = self.queue.get(m)
                    if q is None:
                        continue
                    p = pairs.get(m)
                    if p and p.get("pairAddress"):
                        q["pool"], q["dex"] = p["pairAddress"], p.get("dexId", "")
                    else:
                        q["tries"] += 1
                        if q["tries"] >= 3:                     # no SOL pool found three times: let it go
                            self.queue.pop(m, None)
                            self.failed_today += 1
            q = next((x for x in due if x.get("pool") and x["mint"] in self.queue), None)
            if q is not None:
                r = await chains.history("solana", q["pool"], CANDLES)
                if r.get("error") and not r.get("candles"):
                    q["tries"] += 1
                    if q["tries"] >= 4:
                        self.queue.pop(q["mint"], None)
                        self.failed_today += 1
                else:
                    self.queue.pop(q["mint"], None)
                    self.done_today += 1
                    rows = [c for c in r["candles"] if c[0] >= q["graduated_ts"] - 120]
                    if self.dir:
                        row = {**{k: q[k] for k in ("mint", "symbol", "graduated_ts", "mcap_usd", "pool", "dex")},
                               "fetched_ts": time.time(), "candles": rows}
                        with (self.dir / f"graduated-{time.strftime('%Y-%m-%d', time.gmtime(q['graduated_ts']))}.jsonl").open("a") as f:
                            f.write(json.dumps(row, separators=(",", ":")) + "\n")
            self._save_queue()
        finally:
            self._busy = False
