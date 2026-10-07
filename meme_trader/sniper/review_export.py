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
import time
from collections import Counter, defaultdict
from pathlib import Path

from ..config import ROOT

B58 = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")
WALLET_FIELDS = {"leader", "creator", "trader", "wallet", "funder", "owner", "user", "dev", "buyer", "seller", "caller"}
SENSITIVE_KEYS = ("url", "key", "secret", "token", "password", "wallet", "pubkey", "webhook")
BRIEF_SOURCES = ("late", "sniper")                       # the brief's 303 bot trades: paper mode, these strategies


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
                 "chain", "pool", "opened", "closed", "cost", "proceeds", "pnl", "pnl_pct", "peak_gain_pct", "mae_pct",
                 "exit", "score", "initials", "desk", "p", "start_sol", "entry_delay_s", "exit_delay_s",
                 "entry_vs_signal_pct", "exit_vs_signal_pct", "failed_fees_sol", "entry_mcap_sol", "exit_mcap_sol",
                 "cost_usd", "proceeds_usd", "pnl_usd", "leader"]
        extra = set()
        out = []
        for r in rows:
            r = self.ps.row(r)
            o = {k: r.get(k) for k in fixed}
            o["trade_id"] = hashlib.sha1(f"{r.get('mint')}|{r.get('opened')}|{r.get('source')}|{r.get('session')}"
                                         .encode()).hexdigest()[:16]
            o["interval"], o["day_utc"] = interval_of(r), day_of(r.get("opened"))
            for k in ("fees", "real"):
                for kk, vv in (r.get(k) or {}).items():
                    o[f"{k}_{kk}"] = vv
                    extra.add(f"{k}_{kk}")
            o["feat_json"] = json.dumps(r["feat"]) if r.get("feat") else ""
            out.append(o)
        cols = fixed + sorted(extra) + ["feat_json"]
        with open(self.out / "paper_trades.csv", "w", newline="") as f:
            w = csv.DictWriter(f, cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(out)
        self._note("paper_trades.csv", rows=len(out), malformed_input_lines=bad,
                   by_interval=dict(Counter(o["interval"] for o in out)),
                   brief_cohort_by_source=dict(Counter(o["source"] for o in out if o["interval"].startswith("brief"))))
        return out

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
        with open(self.out / "revival_outcomes.jsonl", "w") as f:
            for r in results:
                f.write(json.dumps({"follow_id": r["id"], "status": "censored" if r.get("censored") else "closed",
                                    "exploratory": True, **r}) + "\n")
                signals.setdefault(r["id"], {k: r.get(k) for k in keep})
            for fo in follows:
                for name, x in (fo.get("exits") or {}).items():
                    f.write(json.dumps({"follow_id": fo["id"], "status": "open", "exploratory": True, "exit": name,
                                        **{k: fo.get(k) for k in keep}, "p0": fo.get("p0"), "fill_t": fo.get("fill_t"),
                                        "state": x}) + "\n")
                signals.setdefault(fo["id"], {k: fo.get(k) for k in keep})
        with open(self.out / "revival_signals.jsonl", "w") as f:
            for sid, s in signals.items():
                f.write(json.dumps({"signal_id": sid, "eligible": True, **s}) + "\n")
        self._note("revival_outcomes.jsonl", rows=len(results), open_follows=len(follows),
                   closed=sum(1 for r in results if not r.get("censored")),
                   censored=sum(1 for r in results if r.get("censored")), checkpoint_saved=st.get("saved"))
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
            known = sorted(d for d in days if d)
            span = []
            if known:                                    # every calendar day from the first to the last, gaps too
                t = calendar.timegm(time.strptime(known[0], "%Y-%m-%d"))
                while day_of(t) <= known[-1]:
                    span.append(day_of(t))
                    t += 86400
            for day in span:
                q = dict(days.get(day, {}))
                q["trades_by_interval"] = json.dumps(dict(q.get("trades_by_interval", {})))
                w.writerow({"day_utc": day, **q})
        self._note("day_quality.csv", rows=len(span), feeds_scanned=scan_feeds)
        if not scan_feeds:
            self.unavailable.append(("day_quality.csv: per-day feed statistics", "run with --scan-feeds"))
        self.unavailable.append(("day_quality.csv: eligible universe size, reordered counts",
                                 "not recorded per day; reordering is only measured live (FeedQuality)."))
        self.findings.append("Repeated trade events (see day_quality.csv `repeat_*`): on 10-07, with the paid feed at "
                             "processed commitment, ~4% of trade events arrive a second time in another slot - fork "
                             "copies; ~1% with different amounts. The engine drops the second copy, so for those it keeps "
                             "whichever arrived first, which may be the abandoned fork's. Before 10-07 (no event index) "
                             "repeats were mostly same-slot transport duplicates (~0.3% of trades) that replays count twice.")


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
                # by signature, trader, side and size. In another slot it's a fork copy (processed commitment)
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


def secret_values(root: Path) -> list[str]:
    out = []
    env = root / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                v = line.split("=", 1)[1].strip().strip("'\"")
                if len(v) >= 12:
                    out.append(v)
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
    for name in ("power_t9.out", "power_t9.json"):
        if (data / "research" / name).exists():
            (out / "registrations").mkdir(exist_ok=True)
            shutil.copy2(data / "research" / name, out / "registrations" / name)
    ex.as_run()
    for p in sorted((data / "research").glob("BUILDER_REPLY*.md")):   # the builder's reply to the review
        shutil.copy2(p, out / p.name)
    if config is not None:
        (out / "config_effective.json").write_text(json.dumps(safe_config(config), indent=1, default=str))
    from .research import code_revision
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
           "code_revision": code_revision(), "package_schema": 1, "inputs": inputs, "outputs": files,
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
                            "`proceeds`, never counted as separate trades",
        "paper_orders.jsonl": "every journal event (buys, sells, failures, signals, AI-team notes) verbatim as text",
        "graduation_latency_runs.jsonl": "lab A/B runs (per-day aggregates) and the T8 variant table, one row per "
                                         "variant and day, with the exact code it ran on",
        "revival_outcomes.jsonl": "the forward test's authoritative SQLite results (closed and censored) plus open "
                                  "follows, by follow id and exit",
        "revival_signals.jsonl": "one row per follow taken (signal, control flag, decision clock, cost model)",
        "model_picks_follow.jsonl": "T2: the live follow of the model's picks and the exit lab",
        "ai_team_calls.jsonl": "T10: the AI team's graduation calls",
        "day_quality.csv": "each UTC day: feed span, gaps, lag percentiles, feed health, duplicates, trades by "
                           "interval, revival follows (days with no trades included)",
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
