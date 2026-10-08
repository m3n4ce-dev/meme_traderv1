"""A research package for an independent reviewer (a fifth review's "Stage 1", 2026-10-07).

    python -m meme_trader.sniper.review_export OUT_DIR [--data DATA] [--scan-feeds]

Exports the stores as they are: every row in each declared interval, nothing picked for looking good, and what was
never recorded marked unavailable rather than reconstructed from today's code or data. It is written for the owner to
review and send; nothing here uploads anything.

Privacy: no keys, .env values, signed transactions or RPC URLs. Wallet addresses (the owner's, copy-trade leaders',
creators', traders') are replaced by stable pseudonyms keyed by data/research/pseudonym.key (local, never exported).
Public coin and pool addresses stay. The export fails if its final scan finds the owner's wallet or a secret.
"""
from __future__ import annotations

import argparse
import calendar
import csv
import gzip
import hashlib
import hmac
import json
import re
import secrets
import shutil
import sqlite3
import statistics
import subprocess
import time
from collections import Counter, defaultdict
from pathlib import Path

from ..config import ROOT
from ..redact import secret_pieces

B58 = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")
WALLET_FIELDS = {"leader", "creator", "trader", "wallet", "funder", "owner", "user", "dev", "buyer", "seller", "caller"}
SENSITIVE_KEYS = ("url", "key", "secret", "token", "password", "wallet", "pubkey", "webhook", "services")
# ("services": this machine's systemd units, e.g. sniper.hq.extra_services - local names, not research settings)
BRIEF_SOURCES = ("late", "sniper")                       # the brief's 303 bot trades: paper mode, these strategies
RESIDUAL_TOLERANCE_SOL = 0.01                            # declared in advance: an account reconciles within this


def day_of(ts) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(float(ts))) if ts else ""


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 23), b""):
            h.update(b)
    return h.hexdigest()


class Pseudo:
    """Stable wallet pseudonyms: w_ + 12 hex of HMAC-SHA256(local key, address). Known coins and pools stay."""

    def __init__(self, key: bytes, owner_wallet: str = "", public: set | None = None):
        self.key, self.owner, self.public = key, owner_wallet, set(public or ())

    @classmethod
    def for_data(cls, data: Path, owner_wallet: str = "", public: set | None = None) -> "Pseudo":
        kp = data / "research" / "pseudonym.key"
        if not kp.exists():
            kp.parent.mkdir(parents=True, exist_ok=True)
            kp.write_text(secrets.token_hex(32))
            kp.chmod(0o600)
        return cls(bytes.fromhex(kp.read_text().strip()), owner_wallet, public)

    def wallet(self, w):
        if not isinstance(w, str) or not w:
            return w
        if self.owner and w == self.owner:
            return "OWNER_WALLET"
        return "w_" + hmac.new(self.key, w.encode(), hashlib.sha256).hexdigest()[:12]

    def text(self, s):
        if not isinstance(s, str):
            return s
        if self.owner:
            s = s.replace(self.owner, "OWNER_WALLET")
        return B58.sub(lambda m: m.group(0) if self.is_public(m.group(0)) else self.wallet(m.group(0)), s)

    def is_public(self, x: str) -> bool:
        return x in self.public or x.endswith("pump")      # a pump.fun coin address

    def row(self, r: dict) -> dict:
        out = {}
        for k, v in r.items():
            if k in WALLET_FIELDS and isinstance(v, str):
                out[k] = self.wallet(v)
            elif isinstance(v, dict):
                out[k] = self.row(v)
            elif isinstance(v, str) and k not in ("mint", "pool", "symbol", "id", "url", "signature"):
                out[k] = self.text(v)
            else:
                out[k] = v
        return out


def read_jsonl(p: Path):
    """(rows, malformed line count). A truncated last line is counted, never fatal."""
    rows, bad = [], 0
    op = gzip.open if p.suffix == ".gz" else open
    with op(p, "rt") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                bad += 1
    return rows, bad


def _days(known: list[str]) -> list[str]:
    """Every calendar day from the first to the last (days with nothing included)."""
    span = []
    if known:
        t = calendar.timegm(time.strptime(known[0], "%Y-%m-%d"))
        while day_of(t) <= known[-1]:
            span.append(day_of(t))
            t += 86400
    return span


def interval_of(r: dict) -> str:
    mode = r.get("mode")
    if mode is None:
        return "untagged (logged before trades carried a mode)"
    if mode == "paper" and r.get("source") in BRIEF_SOURCES:
        return "brief cohort (paper bot trades)"
    if mode.startswith("paper-synthetic"):
        return "synthetic demo (not market data)"
    return f"{mode} other ({r.get('source')})"


# ------------------------------------------------------------------ the exports
class Export:
    def __init__(self, data: Path, out: Path, pseudo: Pseudo, now: float | None = None):
        self.data, self.out, self.ps = Path(data), Path(out), pseudo
        self.now = now if now is not None else time.time()
        self.inputs: list[Path] = []
        self.counts: dict[str, dict] = {}
        self.unavailable: list[tuple[str, str]] = []
        self.findings: list[str] = []
        self.out.mkdir(parents=True, exist_ok=True)

    def _note(self, name: str, **kw) -> None:
        self.counts[name] = kw

    def trades(self) -> list[dict]:
        files = sorted(self.data.glob("trades-*.jsonl"))
        self.inputs += files
        rows, bad = [], 0
        for f in files:
            rs, b = read_jsonl(f)
            rows += rs
            bad += b
        fixed = ["trade_id", "interval", "day_utc", "mode", "source", "session", "config", "model", "mint", "symbol",
                 "chain", "pool", "opened", "closed", "close_day_utc", "cost", "proceeds", "gross_pnl",
                 "failed_fees_sol", "pnl", "pnl_pct", "pnl_schema", "net_derived", "peak_gain_pct", "mae_pct",
                 "exit", "score", "initials", "desk", "p", "start_sol", "entry_delay_s", "exit_delay_s",
                 "entry_vs_signal_pct", "exit_vs_signal_pct", "entry_mcap_sol", "exit_mcap_sol",
                 "cost_usd", "proceeds_usd", "pnl_usd", "leader"]
        extra = set()
        out = []
        from .pnl import upgrade
        sp = self.data / "sniper_state_paper.json"
        booked = set()
        if sp.exists():
            for b in (json.loads(sp.read_text()).get("book") or {}).get("closed") or []:
                booked.add((b.get("mint"), round(float(b.get("opened") or 0), 3)))
        for r in rows:
            r = self.ps.row(upgrade(r))
            o = {k: r.get(k) for k in fixed}
            o["provenance"] = "trade log"
            if r.get("mode") is None and (r.get("mint"), round(float(r.get("opened") or 0), 3)) in booked:
                o["mode"] = "paper"                      # logged before its close path tagged a mode; the book has it
                o["provenance"] = "trade log; mode recovered from the saved paper book"
            o["trade_id"] = hashlib.sha1(f"{r.get('mint')}|{r.get('opened')}|{r.get('source')}|{r.get('session')}"
                                         .encode()).hexdigest()[:16]
            o["interval"], o["day_utc"] = interval_of({**r, "mode": o["mode"]}), day_of(r.get("opened"))
            o["close_day_utc"] = day_of(r.get("closed"))
            for k in ("fees", "real"):
                for kk, vv in (r.get(k) or {}).items():
                    o[f"{k}_{kk}"] = vv
                    extra.add(f"{k}_{kk}")
            o["feat_json"] = json.dumps(r["feat"]) if r.get("feat") else ""
            out.append(o)
        cols = fixed + ["provenance"] + sorted(extra) + ["feat_json"]
        with open(self.out / "paper_trades.csv", "w", newline="") as f:
            w = csv.DictWriter(f, cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(out)
        self._note("paper_trades.csv", rows=len(out), malformed_input_lines=bad,
                   by_interval=dict(Counter(o["interval"] for o in out)),
                   brief_cohort_by_source=dict(Counter(o["source"] for o in out if o["interval"].startswith("brief"))))
        return out

    def ledger(self, trades: list[dict]) -> None:
        """The paper account by close day (realized, chronological - not the entry-day cohorts in paper_trades.csv),
        and whether the current account's cash reconciles with its trades (a sixth review, 2026-10-07)."""
        from .pnl import upgrade_all
        sp = self.data / "sniper_state_paper.json"
        state = json.loads(sp.read_text()) if sp.exists() else {}
        if state:
            self.inputs.append(sp)
        book = state.get("book") or {}
        closed = upgrade_all([dict(r) for r in book.get("closed") or []])
        start_ts = min((r["closed"] for r in closed), default=None)   # the current account's first close
        # ONE row set for the daily ledger and the cash check (a seventh review, 2026-10-07): the current account is
        # the saved book's closes, each matched to its trade-log row; earlier periods are the paper rows before it
        key = lambda r: (r.get("mint"), round(float(r.get("opened") or 0), 3))
        logged = {key(t): t for t in trades}
        current = []
        for r in closed:
            t = logged.get(key(r))
            if t is None:                                # in the book, not in any trade log
                t = {"mint": r.get("mint"), "opened": r.get("opened"), "closed": r.get("closed"),
                     "close_day_utc": day_of(r.get("closed")), "gross_pnl": r.get("gross_pnl"), "pnl": r.get("pnl"),
                     "failed_fees_sol": r.get("failed_fees_sol"), "source": r.get("source"),
                     "provenance": "saved book only (not in the trade logs)"}
            current.append(t)
        cur_keys = {key(t) for t in current}
        earlier = [t for t in trades if t.get("mode") == "paper" and key(t) not in cur_keys and
                   (start_ts is None or (t["closed"] or 0) < start_ts - 1)]
        self.counts["account_rows"] = {"current": len(current), "earlier": len(earlier),
                                       "by_provenance": dict(Counter(t.get("provenance", "trade log") for t in current))}
        days: dict[tuple, dict] = {}
        for acct, rows in (("current", current), ("earlier", earlier)):
            for t in rows:
                g = days.setdefault((t["close_day_utc"], acct), {"positions": 0, "gross_pnl": 0.0, "failed_fees": 0.0,
                                                                   "net_pnl": 0.0, "sources": Counter()})
                g["positions"] += 1
                g["gross_pnl"] += float(t.get("gross_pnl") or 0.0)
                g["failed_fees"] += float(t.get("failed_fees_sol") or 0.0)
                g["net_pnl"] += float(t.get("pnl") or 0.0)
                g["sources"][str(t.get("source", "")).split(":")[0]] += 1
        span = _days(sorted({d for d, _ in days}))
        with open(self.out / "account_ledger.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["close_day_utc", "account", "positions", "gross_pnl_sol", "failed_fees_sol", "net_pnl_sol",
                        "cumulative_net_sol", "by_source"])
            cum = {"current": 0.0, "earlier": 0.0}
            for d in span:
                for acct in ("earlier", "current"):
                    g = days.get((d, acct))
                    if g is None:
                        if acct == "current" and not any((d, a) in days for a in cum):
                            w.writerow([d, "", 0, 0, 0, 0, "", "{}"])        # a day with no closes at all
                        continue
                    cum[acct] += g["net_pnl"]
                    w.writerow([d, acct, g["positions"], round(g["gross_pnl"], 6), round(g["failed_fees"], 6),
                                round(g["net_pnl"], 6), round(cum[acct], 6), json.dumps(dict(g["sources"]))])
        rec = {"account_id": state.get("account_id"), "account_start": start_ts,
               "account_start_basis": "the first close in the saved book: the reset that began it wasn't journaled",
               "note": "earlier periods' resets weren't all journaled, so they can't be reconciled to cash",
               "tolerance_sol": RESIDUAL_TOLERANCE_SOL}
        if book:
            net = sum(float(t.get("pnl") or 0) for t in current)        # the same rows as account_ledger.csv
            opened = list((state.get("positions") or {}).values())
            open_part = sum(float(p.get("proceeds_sol", 0)) - float(p.get("initial_cost_sol", 0)) -
                            float(p.get("rent_sol", 0)) - float(p.get("failed_fees_sol", 0)) for p in opened)
            expected = float(book["start_sol"]) + net + open_part
            in_logs = sum(1 for t in current if t.get("provenance") != "saved book only (not in the trade logs)")
            rec.update({
                "start_sol_incl_deposits": book["start_sol"], "deposits": book.get("deposits") or [],
                "closed_positions": len(closed), "closed_list_truncated": len(closed) >= 500,
                "gross_pnl_sol": round(sum(float(t.get("gross_pnl") if t.get("gross_pnl") is not None else t.get("pnl") or 0)
                                           for t in current), 6),
                "failed_fees_on_positions_sol": round(sum(float(t.get("failed_fees_sol") or 0) for t in current), 6),
                "net_pnl_sol": round(net, 6), "open_positions": len(opened), "open_positions_cash_effect_sol":
                round(open_part, 6), "expected_cash_sol": round(expected, 6), "cash_sol": round(float(book["sol"]), 6),
                "residual_sol": round(float(book["sol"]) - expected, 6),
                "within_tolerance": abs(float(book["sol"]) - expected) <= RESIDUAL_TOLERANCE_SOL,
                "residual_means": "cash spent outside any closed or open position: fees of failed buys that never "
                                  "opened one, and anything not journaled (negative = unexplained loss)",
                "closes_in_book_found_in_trade_logs": in_logs,
                "closes_in_book_not_in_trade_logs": len(current) - in_logs,
                "closes_recovered_untagged": sum(1 for t in current if str(t.get("provenance", "")).startswith("trade log;"))})
            rec["event_journal"] = self._events_check(state)
        (self.out / "account_reconciliation.json").write_text(json.dumps(rec, indent=1, default=str))
        self._note("account_ledger.csv", rows=sum(1 for _ in open(self.out / "account_ledger.csv")) - 1)
        self._note("account_reconciliation.json", residual_sol=rec.get("residual_sol"))

    def _events_check(self, state: dict, mode: str = "paper") -> dict:
        """The account rebuilt from its typed events (data/account-<mode>.jsonl, since 2026-10-07): the balance at
        its opening event (open / adopted / reset) plus every cash event after, against the saved cash."""
        p = self.data / f"account-{mode}.jsonl"
        acct = state.get("account_id")
        if not p.exists() or not acct:
            return {"available": False, "why": "no account journal yet (it starts with the 2026-10-07 code)"}
        self.inputs.append(p)
        rows = [r for r in read_jsonl(p)[0] if r.get("account") == acct]
        opens = [i for i, r in enumerate(rows) if r.get("kind") in ("open", "adopted", "reset")]
        if not opens:
            return {"available": False, "why": "this account has no opening event in the journal"}
        i0 = opens[-1]
        base = float(rows[i0].get("cash_after") or 0.0)
        later = rows[i0 + 1:]
        by = defaultdict(float)
        for r in later:
            by[r["kind"]] += float(r.get("sol") or 0.0)
        expected = base + sum(by.values())
        cash = float((state.get("book") or {}).get("sol") or 0.0)
        return {"available": True, "opening": rows[i0].get("kind"), "opening_ts": rows[i0].get("ts"),
                "opening_cash": base, "events": len(later), "cash_by_kind": {k: round(v, 9) for k, v in by.items()},
                "unattached_failed_fees_sol": round(sum(float(r.get("sol") or 0) for r in later
                                                        if r.get("kind") == "failed_fee" and not r.get("attached")), 9),
                "expected_cash_sol": round(expected, 9), "cash_sol": round(cash, 9),
                "residual_sol": round(cash - expected, 9),
                "within_tolerance": abs(cash - expected) <= RESIDUAL_TOLERANCE_SOL}

    def journal(self) -> None:
        files = sorted(self.data.glob("journal-*.jsonl"))
        self.inputs += files
        n, bad, kinds = 0, 0, Counter()
        with open(self.out / "paper_orders.jsonl", "w") as f:
            for p in files:
                rs, b = read_jsonl(p)
                bad += b
                for r in rs:
                    kinds[r.get("event", "?")] += 1
                    o = {"ts": r.get("ts"), "day_utc": day_of(r.get("ts")), "agent": r.get("agent"),
                         "event": r.get("event"), "mint": r.get("mint") or "", "text": self.ps.text(r.get("text", "")),
                         "structured": False}
                    f.write(json.dumps(o) + "\n")
                    n += 1
        self._note("paper_orders.jsonl", rows=n, malformed_input_lines=bad, by_event=dict(kinds))
        self.unavailable.append(("paper_orders.jsonl: structured per-attempt orders (intent vs result, retries, "
                                 "skip reasons)", "paper orders were logged as journal text, not records; only live "
                                 "orders get structured outbox rows (since PR #89). Exported: every journal event, "
                                 "verbatim (pseudonymized)."))

    def lab(self) -> None:
        rows = []
        ex = self.data / "lab" / "experiments.json"
        if ex.exists():
            self.inputs.append(ex)
            for e in json.loads(ex.read_text()):
                res = None
                rp = self.data / "lab" / f"{e['id']}.result.json"
                if rp.exists():
                    self.inputs.append(rp)
                    res = json.loads(rp.read_text())
                rows.append({"run_id": f"lab-{e['id']}", "kind": "lab A/B (rules only, account stops off)",
                             "change": {e["key"]: e["value"]}, "from": e.get("now"), "why": e.get("why"),
                             "by": e.get("by"), "status": e.get("status"), "started": e.get("started"),
                             "finished": e.get("finished"), "result": res})
        t8 = self.data / "research" / "exploratory" / "outputs" / "edge2-compare.out"
        line = re.compile(r"\[(\d+)/(\d+)\]\s+(\S+)\s+feed-(\S+)\s+P&L ([+-][\d.]+)\s+n=(\d+)")
        if t8.exists():
            self.inputs.append(t8)
            commit = (self.data / "research" / "exploratory" / "provenance" / "t8-compare-wt-cr.commit")
            for m in line.finditer(t8.read_text()):
                rows.append({"run_id": "t8-compare-2026-10-06", "kind": "T8 graduation variants (rules only, no AI "
                             "vote, daily loss limit and drawdown stop off)", "variant": m.group(3),
                             "day": m.group(4), "pnl_sol": float(m.group(5)), "trades": int(m.group(6)),
                             "job": f"{m.group(1)}/{m.group(2)}",
                             "code": (commit.read_text().strip() if commit.exists() else "unknown") + " + patch "
                             "(provenance/t8-compare-wt-cr.patch)", "per_trade": "unavailable"})
        with open(self.out / "graduation_latency_runs.jsonl", "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        self._note("graduation_latency_runs.jsonl", rows=len(rows))
        self.unavailable.append(("graduation_latency_runs.jsonl: per-trade rows, starting capital per run, failed "
                                 "fills", "the compare tool printed one line per variant and day; it doesn't keep its "
                                 "trades. A current-code rerun with per-trade output is possible (hours of CPU) and "
                                 "would be labelled as a rerun."))
        self.unavailable.append(("T8 jobs 29-40 (devsell variants on 10-05; everything on 10-06)", "the run stopped "
                                 "when the recorder compressed 10-06's file mid-run (fixed in PR #89)."))

    def revival(self) -> None:
        db = self.data / "revival-v2.db"
        if not db.exists():
            self.unavailable.append(("revival_*", "no forward-test store yet"))
            return
        self.inputs.append(db)
        src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        mem = sqlite3.connect(":memory:")
        src.backup(mem)                                  # one consistent snapshot, WAL included
        src.close()
        results = [json.loads(r) for (r,) in mem.execute("SELECT row FROM results ORDER BY rowid")]
        ck = mem.execute("SELECT v FROM state WHERE k = 'checkpoint'").fetchone()
        st = json.loads(ck[0]) if ck else {}
        opened = st.get("open") or {}
        follows = [f for fs in (opened.values() if isinstance(opened, dict) else [opened]) for f in fs]
        signals: dict[str, dict] = {}
        keep = ("pool", "mint", "symbol", "rule", "delay", "control", "signal_t", "decided_at", "lag_s", "ret5",
                "surge", "cost", "cost_how")
        # one row per (follow, exit) - a sixth review, 2026-10-07: a follow stays in the checkpoint while any of its
        # exits is pending, so an exit that already has its durable result must not be written again as open
        rows_out, terminal = [], set()
        for r in results:
            terminal.add((r["id"], r["exit"]))
            rows_out.append({"follow_id": r["id"], "status": "censored" if r.get("censored") else "closed",
                             "exploratory": True, **r})
            signals.setdefault(r["id"], {k: r.get(k) for k in keep})
        for fo in follows:
            for name, x in (fo.get("exits") or {}).items():
                if (fo["id"], name) in terminal:
                    continue                             # its result row stands
                done = "pnl" in x or x.get("censored")   # terminal in the checkpoint, yet absent from the store
                rows_out.append({"follow_id": fo["id"], "status": "inconsistent" if done else "open",
                                 "exploratory": True, "exit": name, **{k: fo.get(k) for k in keep},
                                 "p0": fo.get("p0"), "fill_t": fo.get("fill_t"), "state": x})
            signals.setdefault(fo["id"], {k: fo.get(k) for k in keep})
        keys = [(r["follow_id"], r["exit"]) for r in rows_out]
        if len(keys) != len(set(keys)):
            raise ValueError(f"revival export: {len(keys) - len(set(keys))} duplicate (follow, exit) rows")
        with open(self.out / "revival_outcomes.jsonl", "w") as f:
            for r in rows_out:
                f.write(json.dumps(r) + "\n")
        with open(self.out / "revival_signals.jsonl", "w") as f:
            for sid, s in signals.items():
                f.write(json.dumps({"signal_id": sid, "eligible": True, **s}) + "\n")
        by = Counter(r["status"] for r in rows_out)
        self._note("revival_outcomes.jsonl", rows=len(rows_out), by_status=dict(by),
                   unique_follows=len({r["follow_id"] for r in rows_out}), duplicate_keys=0,
                   store_results=len(results), checkpoint_follows=len(follows), checkpoint_saved=st.get("saved"))
        self._note("revival_signals.jsonl", rows=len(signals))
        self.unavailable.append(("revival_signals.jsonl: rejected signals and as-of pool/safety inputs",
                                 "the forward test records the follows it takes (signal, control, decision clock, "
                                 "cost); signals it filtered out aren't logged."))

    def follows(self) -> None:
        for src, name, note in (("exit_lab.jsonl", "model_picks_follow.jsonl", "T2 live follow and exit lab"),
                                ("desk_calls.jsonl", "ai_team_calls.jsonl", "T10: the AI team's calls")):
            p = self.data / src
            if not p.exists():
                continue
            self.inputs.append(p)
            rs, bad = read_jsonl(p)
            with open(self.out / name, "w") as f:
                for r in rs:
                    f.write(json.dumps(self.ps.row(r)) + "\n")
            self._note(name, rows=len(rs), malformed_input_lines=bad, what=note)

    def as_run(self) -> None:
        """The exploratory research scripts exactly as they ran (and their printed outputs), for the owner to decide
        whether to share: kept out of the public repo. Local paths are blanked; each original's sha256 is listed."""
        src = self.data / "research" / "exploratory"
        listing = []
        for sub_, dest in (("as-run", "research_as_run"), ("outputs", "research_outputs")):
            d = src / sub_
            if not d.exists():
                continue
            (self.out / dest).mkdir(exist_ok=True)
            for p in sorted(d.iterdir()):
                if not p.is_file():
                    continue
                text = p.read_text(errors="replace")
                clean = re.sub(r"/tmp/claude-[^\s\"']*?/scratchpad/", "<scratch>/", text)
                clean = re.sub(r"/home/[^/\s\"']+/meme_traderv1", "<repo>", clean.replace(str(ROOT), "<repo>"))
                (self.out / dest / p.name).write_text(self.ps.text(clean) if dest == "research_outputs" else clean)
                listing.append({"file": f"{dest}/{p.name}", "original_sha256": sha256(p)})
        if listing:
            (self.out / "research_as_run.json").write_text(json.dumps(listing, indent=1))
            self._note("research_as_run/", files=len(listing))

    def quotes(self) -> None:
        """The live price watcher (data/quotes.db): its coverage by reason for both arms, quote-priced P&L, and every
        attempt (pool addresses and amounts are public chain data; no RPC URL is stored, only its host)."""
        p = self.data / "quotes.db"
        if not p.exists():
            self.unavailable.append(("quotes_summary.json", "no price-watcher database yet"))
            return
        self.inputs.append(p)
        from .quotes import QuoteBook
        qb = QuoteBook.read_only(p)
        (self.out / "quotes_summary.json").write_text(json.dumps(qb.view(max_age_s=0), indent=1))
        rows = qb.attempts()
        with open(self.out / "quote_attempts.jsonl", "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        self._note("quote_attempts.jsonl", rows=len(rows))
        jobs = qb.jobs()                                 # every job's outcome, those with no attempt included
        with open(self.out / "quote_jobs.jsonl", "w") as f:
            for r in jobs:
                f.write(json.dumps(r) + "\n")
        self._note("quote_jobs.jsonl", rows=len(jobs))
        ids = {j["job_id"] for j in jobs}                # the export's own joins, checked end to end (13th review)
        keys = [(r["job_id"], r["attempt_n"]) for r in rows]
        tried = {r["job_id"] for r in rows}
        check = {"export_version": jobs[0]["export_version"] if jobs else None, "jobs": len(jobs), "attempts": len(rows),
                 "duplicate_attempt_keys": len(keys) - len(set(keys)),
                 "attempts_without_a_job": sum(1 for r in rows if r["job_id"] not in ids),
                 "jobs_with_zero_attempts": sum(1 for j in jobs if j["job_id"] not in tried),
                 "jobs_with_unknown_scheduled_time": sum(1 for j in jobs if not j["due_known"]),
                 "attempts_by_record_version": dict(Counter(str(r["record"].get("v", 1)) for r in rows))}
        (self.out / "quote_export_check.json").write_text(json.dumps(check, indent=1))
        if check["duplicate_attempt_keys"] or check["attempts_without_a_job"]:
            self.findings.append(f"Quote export joins aren't clean: {check}")

    def fork_conflicts(self) -> None:
        """data/fork_conflicts.jsonl (from the ninth review's code on): every fork conflict by a stable id, matched
        creation to terminal record, with every unmatched one classified from the records themselves (a tenth review:
        a count of terminal records isn't a count of conflicts resolved)."""
        p = self.data / "fork_conflicts.jsonl"
        if not p.exists():
            self.unavailable.append(("fork_conflicts_summary.json", "no conflict log yet (it starts with the round-9 code)"))
            return
        self.inputs.append(p)
        rows, bad = read_jsonl(p)
        made, done = {}, {}
        for r in rows:
            cid = r.get("id") or f"{r.get('signature')}|{r.get('event_index')}"
            (made if r.get("kind") == "created" else done if r.get("kind") == "resolved" else {}).setdefault(cid, r)
        first_ts = min((r.get("ts", 0) for r in rows), default=0)
        cutoff = max((r.get("ts", 0) for r in rows), default=0)
        epochs = sorted({r.get("epoch", "") for r in rows if r.get("epoch")})
        cls, durations, out_rows = Counter(), [], []
        for cid in sorted(set(made) | set(done)):
            c, d = made.get(cid), done.get(cid)
            if c and d:
                why = "matched"
                durations.append(d["ts"] - c["ts"])
            elif c and not c.get("tracked", True):
                why = "created, no lookup: coin not tracked"
            elif c:
                later = [e for e in epochs if e > (c.get("epoch") or "")]
                why = ("created, still pending at the export cutoff" if cutoff - c["ts"] < 600 else
                       "created, no terminal record: a later process run took over (restart)" if later else
                       "created, no terminal record")
            else:
                why = ("terminal only: created before the conflict log started" if d["ts"] - first_ts < 900 or
                       not d.get("epoch") else "terminal only: its creation was logged under no record")
            cls[why] += 1
            out_rows.append({"id": cid, "class": why, "created": (c or {}).get("ts"), "resolved": (d or {}).get("ts"),
                             "epoch_created": (c or {}).get("epoch"), "epoch_resolved": (d or {}).get("epoch"),
                             "state": (d or {}).get("state") or (d or {}).get("status"), "proof": (d or {}).get("proof")
                             or (d or {}).get("method"), "code": (d or c or {}).get("code")})
        durations.sort()
        q = (lambda f: round(durations[min(len(durations) - 1, int(f * len(durations)))], 1)) if durations else (lambda f: None)
        out = {"records": len(rows), "bad_lines": bad, "conflicts": len(out_rows), "created_records": len(made),
               "terminal_records": len(done), "classes": dict(cls), "epochs": epochs,
               "created_to_terminal_s": {"n": len(durations), "median": q(0.5), "p90": q(0.9), "max": q(1.0)},
               "log_span_utc": [first_ts, cutoff]}
        (self.out / "fork_conflicts_summary.json").write_text(json.dumps(out, indent=1))
        with open(self.out / "fork_conflicts.jsonl", "w") as f:
            for r in out_rows:
                f.write(json.dumps(r) + "\n")
        self._note("fork_conflicts.jsonl", rows=len(out_rows))

    def t9_rerun(self) -> None:
        """The corrected T9 replay (t9_replay_v2.py): its whole summary, and every trade of the +40% rule family with
        status, entry/exit times and prices, episode and coverage reason."""
        src = self.data / "research" / "exploratory" / "t9_replay_v2"
        if not (src / "t9_replay_v2.json").exists():
            self.unavailable.append(("t9_replay_v2_*", "the corrected T9 replay hasn't been run on this machine"))
            return
        self.inputs += [src / "t9_replay_v2.json", src / "t9_replay_v2_trades.jsonl"]
        shutil.copy2(src / "t9_replay_v2.json", self.out / "t9_replay_v2_summary.json")
        n = 0
        with open(src / "t9_replay_v2_trades.jsonl") as f, open(self.out / "t9_replay_v2_trades_40pct.jsonl", "w") as g:
            for line in f:
                if '"rule": "momentum: +40%' in line:
                    g.write(line)
                    n += 1
        self._note("t9_replay_v2_trades_40pct.jsonl", rows=n)

    def models(self) -> None:
        d = self.out / "models"
        d.mkdir(exist_ok=True)
        meta = []
        for name in ("model.json", "model-trees.json", "model-candidate.json"):
            p = self.data / name
            if not p.exists():
                continue
            self.inputs.append(p)
            shutil.copy2(p, d / name)
            m = json.loads(p.read_text())
            meta.append({"file": name, "sha256": sha256(p), "kind": m.get("kind", "logistic"),
                         "feature_version": m.get("feature_version", 1), "features": m.get("features"),
                         "info": m.get("info")})
        (d / "models.json").write_text(json.dumps(meta, indent=1))
        self._note("models/", files=[m["file"] for m in meta])
        self.unavailable.append(("models/: predictions and labels for an untouched evaluation slice",
                                 "the held-out evaluations were exploratory scripts on 10-03..10-06 (see research "
                                 "scripts); no slice was kept untouched after the models were chosen."))

    def day_quality(self, trades: list[dict], scan_feeds: bool) -> None:
        days: dict[str, dict] = defaultdict(dict)
        for t in trades:
            if t["day_utc"]:
                days[t["day_utc"]].setdefault("trades_by_interval", Counter())[t["interval"]] += 1
        for p in (self.out / "revival_signals.jsonl",):
            if p.exists():
                for line in open(p):
                    r = json.loads(line)
                    days[day_of(r.get("signal_t"))]["revival_follows"] = days[day_of(r.get("signal_t"))].get(
                        "revival_follows", 0) + 1
        for p in sorted(self.data.glob("journal-*.jsonl")):
            for r in read_jsonl(p)[0]:
                d = day_of(r.get("ts"))
                days[d]["journal_events"] = days[d].get("journal_events", 0) + 1
        feeds = sorted(self.data.glob("feed-*.jsonl.gz")) + sorted(self.data.glob("feed-*.jsonl"))
        for p in feeds:
            day = p.name[5:15]
            q = days[day]
            q["feed_file"] = p.name
            q["feed_sealed"] = p.suffix == ".gz"
            if scan_feeds:
                self.inputs.append(p)
                q.update(scan_feed(p))
        cols = ["day_utc", "feed_file", "feed_sealed", "span_h", "first_ts", "last_ts", "events", "trades", "launches",
                "migrations", "malformed_lines", "gaps_over_60s", "gap_minutes", "lag_p50_s", "lag_p90_s", "lag_p99_s",
                "lag_samples", "health_rows", "degraded_minutes", "health_lag_median_s", "health_gap_pct_median",
                "providers", "repeat_same_slot", "repeat_other_slot_same_amounts", "repeat_other_slot_new_amounts",
                "event_index_share", "journal_events", "revival_follows",
                "trades_by_interval"]
        with open(self.out / "day_quality.csv", "w", newline="") as f:
            w = csv.DictWriter(f, cols, extrasaction="ignore")
            w.writeheader()
            span = _days(sorted(d for d in days if d))  # every calendar day from the first to the last
            for day in span:
                q = dict(days.get(day, {}))
                q["trades_by_interval"] = json.dumps(dict(q.get("trades_by_interval", {})))
                w.writerow({"day_utc": day, **q})
        self._note("day_quality.csv", rows=len(span), feeds_scanned=scan_feeds)
        if not scan_feeds:
            self.unavailable.append(("day_quality.csv: per-day feed statistics", "run with --scan-feeds"))
        self.unavailable.append(("day_quality.csv: eligible universe size, reordered counts",
                                 "not recorded per day; reordering is only measured live (FeedQuality)."))
        self.findings.append("Repeated trade events (see day_quality.csv `repeat_*` and evidence/evidence_fork_packet_*): "
                             "the paid feed, subscribed at `confirmed`, delivers ~2.8% of trade events twice with the "
                             "same content and ~0.84% twice with DIFFERENT content - always two copies from consecutive "
                             "slots, ~0.2 s apart. Checked on chain (10-07, a deterministic sample of 200, fetched at "
                             "`finalized` from the paid provider and independently from the public endpoint): in "
                             "200/200 the transaction is in the later slot with exactly the second copy's content, and "
                             "200 events delivered once all match the chain. (An earlier sample of 9, before event "
                             "indexes, read 7 of 9 matching; it can't be re-identified.) Since the eighth review the "
                             "engine treats differing copies as one fork-conflicted event: automated entries wait for "
                             "the chain's answer, verified against the decoded transaction, and the surviving version "
                             "is kept. Before 10-07 (no event index) repeats were mostly same-slot transport "
                             "duplicates (~0.3% of trades) that replays count twice.")


def scan_feed(p: Path) -> dict:
    """One pass over a day's recording: span, counts, gaps, receive-minus-chain lag, feed health, duplicates."""
    kinds, bad, gaps, gap_s, prev = Counter(), 0, 0, 0.0, None
    lags, hl, hg, hosts, degraded, seen, indexed = [], [], [], Counter(), 0, {}, 0
    rep = Counter()
    first = last = None
    op = gzip.open if p.suffix == ".gz" else open
    with op(p, "rt") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                bad += 1
                continue
            ts = r.get("ts")
            if not isinstance(ts, (int, float)):
                continue
            first = ts if first is None else first
            last = ts
            if prev is not None and ts - prev > 60:
                gaps += 1
                gap_s += ts - prev
            prev = ts
            k = r.get("kind")
            kinds[k] += 1
            if k == "trade":
                c = r.get("chain_ts")
                if c:
                    lags.append(ts - c)
                # a repeat: the same event seen again - by signature and event index, or (before indexes, 10-07)
                # by signature, trader, side and size. In another slot it's a fork copy: the first came from a block
                # that didn't survive (seen on the paid feed even at confirmed commitment, 10-07)
                ei = r.get("event_index")
                if isinstance(ei, int) and ei >= 0:
                    indexed += 1
                    key = (r.get("signature"), ei)
                else:
                    key = (r.get("signature"), r.get("trader"), r.get("side"), round(r.get("tokens") or 0, -3))
                a = seen.get(key)
                if a is None:
                    seen[key] = (r.get("slot"), r.get("sol"), r.get("tokens"))
                else:
                    same_slot = a[0] == r.get("slot")
                    same_amt = a[1:] == (r.get("sol"), r.get("tokens"))
                    rep["repeat_same_slot" if same_slot else
                        "repeat_other_slot_same_amounts" if same_amt else "repeat_other_slot_new_amounts"] += 1
            elif k == "health":
                hl.append(r.get("lag_s") or 0.0)
                hg.append(r.get("gap_pct") or 0.0)
                hosts[str(r.get("host", "")).split("?")[0].split("/")[0]] += 1
                degraded += bool(r.get("degraded"))

    lags.sort()

    def q(pct):
        return round(lags[min(len(lags) - 1, int(pct / 100 * len(lags)))], 2) if lags else None
    return {"span_h": round((last - first) / 3600, 2) if first else 0, "first_ts": first, "last_ts": last,
            "events": sum(kinds.values()), "trades": kinds["trade"], "launches": kinds["launch"],
            "migrations": kinds["migration"], "malformed_lines": bad, "gaps_over_60s": gaps,
            "gap_minutes": round(gap_s / 60, 1), "lag_p50_s": q(50), "lag_p90_s": q(90),
            "lag_p99_s": q(99), "lag_samples": len(lags), "health_rows": kinds["health"],
            "degraded_minutes": degraded, "health_lag_median_s": round(statistics.median(hl), 2) if hl else None,
            "health_gap_pct_median": round(statistics.median(hg), 2) if hg else None,
            "providers": json.dumps(dict(hosts)), "repeat_same_slot": rep["repeat_same_slot"],
            "repeat_other_slot_same_amounts": rep["repeat_other_slot_same_amounts"],
            "repeat_other_slot_new_amounts": rep["repeat_other_slot_new_amounts"],
            "event_index_share": round(indexed / kinds["trade"], 3) if kinds["trade"] else None}


def safe_config(params) -> dict:
    """The effective settings with anything URL-, key- or wallet-like removed."""
    def clean(x):
        if isinstance(x, dict):
            return {k: clean(v) for k, v in x.items() if not any(s in str(k).lower() for s in SENSITIVE_KEYS)}
        if isinstance(x, list):
            return [clean(v) for v in x]
        return x
    d = params if isinstance(params, dict) else json.loads(json.dumps(params, default=lambda o: getattr(o, "__dict__", str(o))))
    return clean({k: v for k, v in d.items() if k not in ("wallet", "wallets")})


def _quote_version() -> int:
    from .quotes import RECORD_VERSION
    return RECORD_VERSION


def secret_values(root: Path) -> list[str]:
    out = []
    env = root / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                v = line.split("=", 1)[1].strip().strip("'\"")
                if len(v) >= 12:
                    out.append(v)
                out += sorted(secret_pieces(v))                # a URL's key pieces too: code rewrites URLs (wss -> https)
    return out


def scan(out: Path, needles: list[str]) -> list[str]:
    """Files in the package that contain the owner's wallet, a secret value, or a keyed URL."""
    pat = re.compile(r"(api[_-]?key=|[?&]key=|BEGIN [A-Z ]*PRIVATE KEY|rpcfast\.com/\?|helius-rpc\.com/\?)", re.I)
    hits = []
    for p in sorted(out.rglob("*")):
        if not p.is_file():
            continue
        s = p.read_bytes().decode("utf-8", "replace")
        if pat.search(s) or any(n and n in s for n in needles):
            hits.append(str(p.relative_to(out)))
    return hits


def run(data: Path, out: Path, scan_feeds: bool = False, root: Path = ROOT, owner_wallet: str = "",
        config: dict | None = None, hypotheses: Path | None = None) -> dict:
    data, out = Path(data), Path(out)
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} isn't empty: export into a new directory")
    public = set()                                       # coins and pools named anywhere: public, never pseudonymized
    for f in sorted(data.glob("trades-*.jsonl")) + sorted(data.glob("journal-*.jsonl")) + \
            [data / "exit_lab.jsonl", data / "desk_calls.jsonl"]:
        if f.exists():
            for r in read_jsonl(f)[0]:
                public.update(x for x in (r.get("mint"), r.get("pool")) if isinstance(x, str) and x)
    if (data / "revival-v2.db").exists():
        src = sqlite3.connect(f"file:{data / 'revival-v2.db'}?mode=ro", uri=True)
        for (r,) in src.execute("SELECT row FROM results"):
            j = json.loads(r)
            public.update(x for x in (j.get("mint"), j.get("pool")) if x)
        src.close()
    ex = Export(data, out, Pseudo.for_data(data, owner_wallet, public))
    started = ex.now
    trades = ex.trades()
    ex.ledger(trades)
    ex.journal()
    ex.lab()
    ex.revival()
    ex.follows()
    ex.models()
    ex.day_quality(trades, scan_feeds)
    hp = hypotheses or (root / "research" / "hypotheses.csv")
    if hp.exists():
        shutil.copy2(hp, out / "hypotheses.csv")
    prov = data / "research" / "exploratory" / "provenance"
    if prov.exists():
        shutil.copytree(prov, out / "provenance")
    reg = root / "research" / "registrations"
    if not reg.exists():
        reg = ROOT / "research" / "registrations"
    if reg.exists():
        shutil.copytree(reg, out / "registrations")
    # every power output, its .out log and .sha256 - all versions, found rather than listed (an eleventh review: a
    # hand-kept list left version 5 out of the package)
    for p in sorted((data / "research").glob("power_t9*")):
        if p.is_file() and p.suffix in (".out", ".json", ".sha256"):
            (out / "registrations").mkdir(exist_ok=True)
            shutil.copy2(p, out / "registrations" / p.name)
    evidence = sorted((data / "research").glob("evidence_*.json"))      # builder-side evidence (provider packet...)
    if evidence:
        (out / "evidence").mkdir(exist_ok=True)
        for p in evidence:
            shutil.copy2(p, out / "evidence" / p.name)
    ex.as_run()
    ex.t9_rerun()
    ex.fork_conflicts()
    ex.quotes()
    for p in sorted((data / "research").glob("BUILDER_REPLY*.md")):   # the builder's reply to the review
        shutil.copy2(p, out / p.name)
    if config is not None:
        (out / "config_effective.json").write_text(json.dumps(safe_config(config), indent=1, default=str))
    from .research import code_revision

    def _tree() -> str:
        """The commit's tree: a PR's head and its merge into main have different ids but the same tree (the same code)."""
        try:
            return subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD^{tree}"], capture_output=True, text=True,
                                  timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    files = []
    for p in sorted(out.rglob("*")):
        if p.is_file():
            files.append({"file": str(p.relative_to(out)), "sha256": sha256(p), "bytes": p.stat().st_size})
    inputs = []
    today = time.strftime("%Y-%m-%d", time.gmtime(started))
    for p in sorted(set(ex.inputs)):
        dated = re.search(r"(\d{4}-\d{2}-\d{2})\.jsonl$", p.name)
        if p.suffix == ".gz" or (dated and dated.group(1) < today):
            state = "sealed"                             # a finished day: it won't change
        elif dated or p.suffix in (".db", ".jsonl"):
            state = "growing"                            # today's file or a live store: hashed as of the cutoff
        else:
            state = "snapshot"                           # replaced as a whole (models, lab results)
        inputs.append({"file": str(p.relative_to(data)), "bytes": p.stat().st_size, "sha256": sha256(p),
                       "state": state, "mtime": p.stat().st_mtime})
    import importlib.metadata as md
    man = {"generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)), "cutoff_ts": started,
           "code_revision": code_revision(), "code_tree": _tree(), "package_schema": 1, "inputs": inputs,
           "exporter": {"file": "meme_trader/sniper/review_export.py", "sha256": sha256(Path(__file__)),
                        "quote_record_version": _quote_version()},
           "outputs": files,
           "counts": ex.counts, "unavailable": [{"what": a, "why": b} for a, b in ex.unavailable],
           "findings": ex.findings,
           "packages": {n: _ver(md, n) for n in ("numpy", "lightgbm", "pyyaml", "httpx")},
           "pseudonyms": "w_ + 12 hex of HMAC-SHA256(local key, address); the owner's wallet is OWNER_WALLET",
           "config_id": hashlib.sha256(json.dumps(safe_config(config), sort_keys=True, default=str).encode())
           .hexdigest()[:12] if config is not None else None}
    (out / "manifest.json").write_text(json.dumps(man, indent=1))
    (out / "README_REVIEW_PACKAGE.md").write_text(readme(man))
    hits = scan(out, [owner_wallet] + secret_values(root))
    if hits:
        shutil.rmtree(out)
        raise SystemExit(f"secret scan failed ({', '.join(hits)}): nothing was kept")
    return man


def _ver(md, name):
    try:
        return md.version(name)
    except md.PackageNotFoundError:
        return None


def readme(man: dict) -> str:
    c = man["counts"]
    lines = [
        "# Research package: meme_traderv1", "",
        f"Generated {man['generated_utc']} from code `{man['code_revision']}` by "
        "`python -m meme_trader.sniper.review_export` (in the repo). Every number here is **paper trading or replay**; "
        "no real order has been placed. Revival (T9) rows are **exploratory**.", "",
        "## Files", "",
        "| File | What | Rows |", "|---|---|---|"]
    desc = {
        "paper_trades.csv": "every closed paper trade in the trade logs, all intervals; `interval` separates the "
                            "brief's 303-trade cohort (mode paper, sources late/sniper) from manual, other-chain, "
                            "synthetic-demo and untagged rows. One row per position: partial exits are summed into "
                            "`proceeds`, never counted as separate trades. `pnl` is **net** (schema 2: `gross_pnl` minus "
                            "`failed_fees_sol`; older rows upgraded from their own fees, `net_derived`). `day_utc` is "
                            "the ENTRY day; realized P&L by day is in `account_ledger.csv`",
        "paper_orders.jsonl": "every journal event (buys, sells, failures, signals, AI-team notes) verbatim as text",
        "graduation_latency_runs.jsonl": "lab A/B runs (per-day aggregates) and the T8 variant table, one row per "
                                         "variant and day, with the exact code it ran on",
        "revival_outcomes.jsonl": "the forward test's authoritative SQLite results (closed and censored) plus open "
                                  "follows, by follow id and exit",
        "revival_signals.jsonl": "one row per follow taken (signal, control flag, decision clock, cost model)",
        "model_picks_follow.jsonl": "T2: the live follow of the model's picks and the exit lab",
        "ai_team_calls.jsonl": "T10: the AI team's graduation calls",
        "account_ledger.csv": "the paper account's realized P&L by close day (gross, failed-transaction fees, net, "
                              "cumulative), current account vs earlier periods; `account_reconciliation.json` checks "
                              "the current account's cash against it",
        "day_quality.csv": "each UTC day: feed span, gaps, lag percentiles, feed health, duplicates, trades by "
                           "interval, revival follows (days with no trades included)",
        "t9_replay_v2_summary.json": "the corrected T9 replay: every rule x exit x delay x cost with coverage, status "
                                     "counts, episode concentration, leave-one-day-out and unmeasured-trade bounds; "
                                     "`t9_replay_v2_trades_40pct.jsonl` has each +40%-rule trade",
        "models/": "the deployed logistic and tree models (weights, features, feature version, training info)",
    }
    for k, v in desc.items():
        n = c.get(k, {}).get("rows", "")
        lines.append(f"| `{k}` | {v} | {n} |")
    lines += ["", "Also: `hypotheses.csv` (T1-T12: what was searched, on which days, peeked or not), "
              "`config_effective.json` (today's effective settings without URLs, keys or wallets), `provenance/` (the "
              "exact code the T8 table ran on), `registrations/` (the proposed T9-E1 test and its power simulation), "
              "`research_as_run/` and `research_outputs/` (the exploratory scripts exactly as run, local paths blanked, "
              "and what they printed), `BUILDER_REPLY_*.md`, and `manifest.json` (input and output sha256, sealed / "
              "growing / snapshot, counts).", "",
              "## Definitions", "",
              "- Times are UTC Unix seconds. `ts` in recordings is **receive time**; `chain_ts` is the block's whole-second "
              "time. Missing is null, never zero.",
              "- Paper fills land `execution.paper_delay_s` (2.5 s) after the decision on the received-event timeline, at "
              "the curve price then; fees 1.25% curve + 0.5% platform per side plus priority fee. This is not a "
              "simulation of a faster feed.",
              "- `pnl` is SOL after all modelled fees; `pnl_pct` is of the position's initial cost.",
              "- Wallets are pseudonyms (`w_...`, stable across this package; the owner's is `OWNER_WALLET`). Coins and "
              "pools are public addresses.", "",
              "## Not available (and why)", ""]
    for u in man["unavailable"]:
        lines.append(f"- **{u['what']}**: {u['why']}")
    if man.get("findings"):
        lines += ["", "## Found while exporting", ""] + [f"- {x}" for x in man["findings"]]
    lines += ["", "## Known limitations", "",
              "- Four full recorded days (10-03..10-06) plus a growing fifth; every T1-T9 result is development data.",
              "- The feed was degraded on parts of 10-04..10-06 (free RPCs before 10-06); see `day_quality.csv`.",
              "- No live execution: landing times, failed-fill rates and real fees are unmeasured.",
              "- Revival costs use a depth-based impact model, not executable quotes.", ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m meme_trader.sniper.review_export")
    ap.add_argument("out")
    ap.add_argument("--data", default=None)
    ap.add_argument("--scan-feeds", action="store_true", help="per-day feed statistics (reads every recording)")
    ap.add_argument("--root", default=str(ROOT), help="the checkout whose config/params.yaml and .env are the owner's")
    a = ap.parse_args(argv)
    root = Path(a.root)
    from ..journal import DATA
    data = Path(a.data) if a.data else DATA
    owner, conf = "", None
    from .. import config as cfg
    try:                                                 # the owner's effective settings (example + overrides); their
        conf = json.loads(json.dumps(cfg.load(root / "config" / "params.yaml"), default=str))
        owner = str((conf.get("wallet") or {}).get("pubkey") or "")     # wallet is redacted everywhere
    except (OSError, ValueError, cfg.ConfigError):
        pass
    if not owner or not (root / ".env").exists():
        raise SystemExit(f"no owner wallet or .env under {root}: the secret scan would be blind (use --root)")
    man = run(data, Path(a.out), a.scan_feeds, root=root, owner_wallet=owner, config=conf,
              hypotheses=ROOT / "research" / "hypotheses.csv")
    print(f"wrote {a.out}: {len(man['outputs'])} files; {len(man['unavailable'])} items marked unavailable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
