"""The eleventh external review's acceptance cases, as our own tests: credentials never written down, quotes accepted
only when in hand by their deadline and qualified only with every check, the price mark's chain order and provenance,
the risk policies, and power v6's paired variants. (The ledger's revision 3 is in test_ledger_design.py; the after-exit
source clock in test_review_sol10.py.)"""
import importlib.util
import json
import logging
import sqlite3
import statistics
from pathlib import Path

import httpx
import pytest

from meme_trader import redact
from meme_trader.sniper import quotes as q
from meme_trader.sniper.curve import Curve
from meme_trader.sniper.events import Trade
from meme_trader.sniper.t9_portfolio import DAY_STOP, SIZE, Portfolio
from meme_trader.sniper.tracker import TokenState

try:
    import numpy
except ImportError:                                       # (the power simulation needs it)
    numpy = None

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("quote_fixtures", ROOT / "tests" / "test_quotes.py")
qt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qt)
DUMMY = "TEST_DUMMY_NOT_A_REAL_KEY_123"


# --------------------------------------------------------------------------- R11-5: no credential is ever written down
@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_an_http_error_is_kept_as_its_class_status_and_host_never_its_url(status):
    req = httpx.Request("POST", f"https://rpc.example/?api-key={DUMMY}")

    def failing(url, keys):
        httpx.Response(status, request=req).raise_for_status()
    r = qt.quoter(failing).quote(qt.POOL, "buy", 250_000_000)
    assert DUMMY not in json.dumps(r) and "SECRET" not in json.dumps(r)
    assert r["reason"] == "rpc_error" and r["error"] == {"error": "HTTPStatusError", "host": "rpc.example",
                                                         "status": status, "reason": f"HTTP {status}"}


def test_a_timeout_or_other_error_text_is_redacted():
    def slow(url, keys):
        raise httpx.ReadTimeout(f"timed out talking to https://rpc.example/?api-key={DUMMY}")
    r = qt.quoter(slow).quote(qt.POOL, "buy", 250_000_000)
    assert r["reason"] == "timeout" and DUMMY not in json.dumps(r)


@pytest.mark.parametrize("text", [
    f"Client error '403 Forbidden' for url 'https://rpc.example/?api-key={DUMMY}'",
    f"wss://user:{DUMMY}@node.example/ closed",
    f"https://node.example/{DUMMY}/ refused",
    f"connecting with api_key={DUMMY} failed",
    f"Authorization: Bearer {DUMMY}"])
def test_redact_removes_credentials_wherever_a_text_carries_them(text):
    assert DUMMY not in redact.redact(text)


def test_ordinary_text_stays_readable():
    t = "token: 7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU sold at https://pump.fun/coin"
    assert redact.redact(t) == t


def test_registered_and_env_secrets_are_redacted_everywhere(monkeypatch):
    monkeypatch.setenv("SOME_RPC_URL", f"https://rpc.example/v1/{DUMMY}")
    monkeypatch.setattr(redact, "_env_checked", 0.0)
    assert DUMMY in redact.secret_values()
    assert redact.redact(f"rewritten to wss://rpc.example/v1/{DUMMY} and {DUMMY[:20]}xx") == \
        "rewritten to wss://rpc.example/v1/<redacted> and " + DUMMY[:20] + "xx"
    redact.register("https://other.example/?k=ANOTHER_DUMMY_VALUE_42")
    assert "ANOTHER_DUMMY_VALUE_42" not in redact.redact("got ANOTHER_DUMMY_VALUE_42 back")


def test_log_records_journal_rows_and_dashboard_messages_are_redacted(tmp_path, monkeypatch, caplog):
    redact.install()
    with caplog.at_level(logging.WARNING):
        logging.getLogger("meme_trader.test").warning("feed error: %s", f"https://rpc.example/?api-key={DUMMY}")
        try:
            raise RuntimeError(f"boom https://rpc.example/?api-key={DUMMY}")
        except RuntimeError:
            logging.getLogger("meme_trader.test").exception("with a traceback")
    text = "\n".join(r.getMessage() + (r.exc_text or "") for r in caplog.records)
    assert DUMMY not in text and "<redacted>" in text
    from meme_trader import journal
    monkeypatch.setattr(journal, "DATA", tmp_path)
    journal.record("sniper", "error", text=f"lookup https://rpc.example/?api-key={DUMMY}", nested={"u": DUMMY + "?"},
                   url=f"https://x.example/?token={DUMMY}")
    rows = "".join(p.read_text() for p in tmp_path.glob("journal-*.jsonl"))
    assert DUMMY not in rows and json.loads(rows.splitlines()[0])["event"] == "error"


def test_the_exporters_scan_checks_a_urls_key_pieces(tmp_path):
    from meme_trader.sniper import review_export as rx
    (tmp_path / ".env").write_text(f"SOLANA_WS_URL=wss://rpc.example/?api_key={DUMMY}\n")
    vals = rx.secret_values(tmp_path)
    assert DUMMY in vals                                     # the piece, not only the whole wss:// value
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "x.json").write_text(json.dumps({"url": f"https://rpc.example/x{DUMMY}"}))   # rewritten, not the .env text
    assert rx.scan(pkg, vals) == ["x.json"]


# --------------------------------------------------------------------------- R11-3/4: the job clock and the lock
def run_job(tmp_path, kind, start, deadline, takes):
    tmp_path.mkdir(parents=True, exist_ok=True)
    c = qt.Clock(start)
    good = qt.chain()

    def fetch(url, keys):
        c.t += takes
        return good(url, keys)
    quoter = qt.quoter(fetch, clock=c)
    quoter.static[qt.POOL] = {"pool_base_token_account": qt.BASE_VAULT, "pool_quote_token_account": qt.QUOTE_VAULT,
                              "base_mint": qt.BASE_MINT, "quote_mint": q.ps.WSOL}
    b = q.QuoteBook(tmp_path / "q.db", quoter, c)
    b.request("J", kind, qt.POOL, "buy" if kind == "entry" else "sell", 250_000_000, start, deadline,
              {"follow": "F", "holds": {"hold": 3600}})
    b.run()
    state, result = b.db.execute("SELECT state, result FROM jobs WHERE id = 'J'").fetchone()
    att = [json.loads(r) for (r,) in b.db.execute("SELECT rec FROM attempts")]
    return b, state, json.loads(result or "{}"), att


def test_a_quote_in_hand_at_the_deadline_counts_and_one_after_it_never_does(tmp_path):
    b, state, r, att = run_job(tmp_path / "a", "entry", 1059, 1060, 1.0)           # in hand at 1060: inclusive
    assert state == "ok" and r["job"]["usable_at"] == 1060
    assert b.db.execute("SELECT due FROM jobs WHERE kind = 'exit'").fetchone()[0] == 1060 + 3600
    b, state, r, att = run_job(tmp_path / "b", "entry", 1059, 1060, 2.0)           # in hand at 1061
    assert state == "skipped" and r["reason"] == "late_response" and r["job"]["overrun_s"] == 1.0
    assert att[0]["reason"] == "ok" and att[0]["job"]["usable_at"] == 1061         # kept, never re-timed
    assert not b.db.execute("SELECT 1 FROM jobs WHERE kind = 'exit'").fetchone()
    b, state, r, att = run_job(tmp_path / "c", "exit", 1059, 1060, 2.0)
    assert state == "unmeasured" and r["reason"] == "late_response" and r["raw_output"]


def test_a_failed_begin_releases_the_lock_and_a_later_run_succeeds(tmp_path):
    c = qt.Clock(1000)
    b = q.QuoteBook(tmp_path / "q.db", qt.quoter(qt.chain(), clock=c), c)
    b.request("J", "entry", qt.POOL, "buy", 250_000_000, 1000, 1060, {})
    b.db.execute("PRAGMA busy_timeout = 1")
    other = sqlite3.connect(tmp_path / "q.db", isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.OperationalError):
        b.run()
    assert not b.lock.locked()
    other.execute("ROLLBACK")
    assert b.run() == 1 and b.db.execute("SELECT state, tries FROM jobs").fetchone() == ("ok", 1)


# --------------------------------------------------------------------------- R11-6: raw vs qualified
def test_a_quote_records_what_it_priced_and_its_qualification(tmp_path):
    r = qt.quoter(qt.chain()).quote(qt.POOL, "buy", 250_000_000)
    assert r["v"] == q.RECORD_VERSION and r["qualified"] and r["unqualified"] == []
    assert set(r["accounts"]) == {"pool", "base_vault", "quote_vault", "base_mint", "global_config", "fee_config"}
    assert all(len(a["sha256"]) == 64 for a in r["accounts"].values())
    assert r["mint_supply"] == str(10 ** 13) and r["mint_decimals"] == 6 and r["ref"] == q.FRESHNESS_REF
    rt = r["round_trip"]
    assert rt["model"].startswith("post-buy") and -10 < rt["pct"] < 0               # a round trip costs something
    no_ref = qt.quoter(qt.chain(), ref=0).quote(qt.POOL, "buy", 250_000_000)
    assert no_ref["reason"] == "ok" and not no_ref["qualified"] and no_ref["unqualified"] == ["no_freshness_reference"]
    tail = qt.chain(**{qt.POOL: (q.ps.PUMP_AMM_PROGRAM, qt.pool_bytes()[:-1] + b"\x07")})
    prefix = qt.quoter(tail).quote(qt.POOL, "buy", 250_000_000)
    assert q.qualification(prefix) == "prefix-only" and q.qualification(no_ref) == "unqualified"
    assert q.qualification({"reason": "ok", "undocumented_tail": False}) == "unknown"     # a legacy record


def test_a_vault_of_another_token_program_than_its_mint_is_refused_for_good():
    bad = qt.chain(**{qt.BASE_VAULT: (q.ps.TOKEN_2022_PROGRAM, qt.token_account(qt.BASE_MINT, qt.POOL, 8 * 10 ** 14))})
    r = qt.quoter(bad).quote(qt.POOL, "buy", 250_000_000)
    assert r["reason"] == "vault_program_mismatch" and "vault_program_mismatch" in q.PERMANENT


def put(b, key, kind, result, meta, amount=250_000_000, due=0.0):
    b.request(key, kind, "POOL", "buy" if kind == "entry" else "sell", amount, due, 99999, meta)
    b.db.execute("UPDATE jobs SET state = 'ok', tries = 1, result = ? WHERE id = ?", (json.dumps(result), key))


def test_coverage_and_pnl_keep_raw_and_qualified_populations_apart(tmp_path):
    b = q.QuoteBook(tmp_path / "q.db", qt.quoter(qt.chain()), qt.Clock())
    holds = {"hold": 3600}
    ok = {"reason": "ok", "v": 2, "qualified": True, "unqualified": []}
    for f, entry_q, exit_q in (("A", ok, ok), ("B", ok, {**ok, "qualified": False,
                                                            "unqualified": ["undocumented_pool_bytes"]})):
        meta = {"follow": f, "rule": "R", "delay": 60, "holds": holds}
        put(b, f"{f}|entry", "entry", {**entry_q, "output": "1000", "job": {"usable_at": 100.0}}, meta)
        put(b, f"{f}|hold", "exit", {**exit_q, "output": "300000000"},
            {**meta, "exit": "hold", "entry_lamports": "250000000"}, amount=1000, due=3700.0)
    meta = {"follow": "C", "rule": "R", "delay": 60, "holds": holds, "exit": "hold", "entry_lamports": "250000000"}
    put(b, "C|hold", "exit", {**ok, "output": "300000000"}, meta, amount=1000, due=3700.0)     # no entry found
    put(b, "D|entry", "entry", {"reason": "ok", "output": "5", "undocumented_tail": True}, {"follow": "D"})  # legacy
    v = b.view(0)
    cov = v["coverage"]["entry / signal"]
    assert cov["ok (qualified)"] == 2 and cov["ok, qualification unknown (a record from before the rules)"] == 1
    assert cov["first_try_raw_ok"] == 3 and cov["first_try_qualified_ok"] == 2 and "ok" not in cov
    pops = {r["qualification"]: r["n"] for r in v["quote_pnl"]}
    assert pops == {"qualified": 1, "prefix-only": 1, "unknown": 1}
    assert [r["qualified"] for r in v["quote_pnl"] if r["qualification"] == "qualified"] == [True]


def test_a_qualified_round_trip_needs_matching_ends(tmp_path):
    b = q.QuoteBook(tmp_path / "q.db", qt.quoter(qt.chain()), qt.Clock())
    ok = {"reason": "ok", "v": 2, "qualified": True, "unqualified": []}
    meta = {"follow": "A", "rule": "R", "delay": 60, "holds": {"hold": 3600}}
    put(b, "A|entry", "entry", {**ok, "output": "1000", "job": {"usable_at": 100.0}}, meta)
    put(b, "A|hold", "exit", {**ok, "output": "300000000"}, {**meta, "exit": "hold", "entry_lamports": "250000000"},
        amount=999, due=3700.0)                                                     # sells other than it bought
    assert [r["qualification"] for r in b.view(0)["quote_pnl"]] == ["unknown"]


def test_the_quote_pnl_median_is_the_ordinary_median(tmp_path):
    b = q.QuoteBook(tmp_path / "q.db", qt.quoter(qt.chain()), qt.Clock())
    got = [250_000_000, 300_000_000, 260_000_000, 400_000_000]
    for i, g in enumerate(got):
        put(b, str(i), "exit", {"reason": "ok", "v": 2, "qualified": True, "unqualified": [], "output": str(g)},
            {"follow": str(i), "rule": "R", "delay": 60, "exit": "hold", "entry_lamports": "250000000"})
    rets = [100 * ((g / 1e9 - q.TX_COST_SOL) / (0.25 + q.TX_COST_SOL) - 1) for g in got]
    assert b.view(0)["quote_pnl"][0]["median_pct"] == round(statistics.median(rets), 2)


def test_every_job_is_exported_including_those_missed_with_no_attempt(tmp_path):
    c = qt.Clock(2000)
    b = q.QuoteBook(tmp_path / "q.db", qt.quoter(qt.chain(), clock=c), c)
    b.request("late", "entry", qt.POOL, "buy", 1, 1000, 1060, {"follow": "F", "signal_t": 990, "decided_at": 995})
    b.run()
    jobs = b.jobs()
    assert [(j["job"], j["state"], j["tries"], j["reason"]) for j in jobs] == [("late", "skipped", 0, "missed")]
    assert jobs[0]["signal_t"] == 990 and jobs[0]["deadline"] == 1060 and not b.attempts()


# --------------------------------------------------------------------------- R11-1: the price mark's chain order
def amm(slot, cap, sig, ts, idx=0):
    return Trade("M", ts, "W", "buy", 1, 10, 40, 1000, signature=sig, pool="pump-amm", mcap_sol=cap, slot=slot,
                 chain_ts=slot, event_index=idx)


def curve_trade(slot, v_sol, v_tokens, sig, ts):
    return Trade("M", ts, "W", "buy", 1, 10, v_sol, v_tokens, signature=sig, pool="pump", slot=slot, chain_ts=slot,
                 event_index=0)


@pytest.mark.parametrize("order", [(0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0)])
def test_the_newest_slot_wins_in_every_arrival_order_and_older_ones_keep_their_other_facts(order):
    trades = [amm(198, 40, "a", 1), amm(199, 50, "b", 2), amm(200, 100, "c", 3)]
    s = TokenState("M", None, 0)
    for i, k in enumerate(order):
        s.on_trade(Trade(**{**trades[k].__dict__, "ts": 10 + i}), 30, 30)
    assert s.curve.price == pytest.approx(100 / s.supply) and s.mark["slot"] == 200 and s.mark["signature"] == "c"
    assert s.buys == 3 and s.volume_sol == 3                                        # every event's facts counted
    assert s.marks_refused == sum(1 for i, k in enumerate(order) if any(order[j] > k for j in range(i)))


def test_within_a_slot_one_transactions_event_order_is_used_and_other_transactions_are_flagged_not_ordered():
    s = TokenState("M", None, 0)
    s.on_trade(amm(300, 100, "t", 1, idx=1), 30, 30)
    s.on_trade(amm(300, 90, "t", 2, idx=0), 30, 30)                                 # earlier in the same transaction
    assert s.curve.price == pytest.approx(100 / s.supply) and not s.price_ambiguous
    s.on_trade(amm(300, 120, "u", 3), 30, 30)                                       # another transaction, same slot
    assert s.curve.price == pytest.approx(100 / s.supply) and s.price_ambiguous     # kept, flagged
    s.on_trade(amm(301, 130, "v", 4), 30, 30)                                       # a later slot settles it
    assert s.curve.price == pytest.approx(130 / s.supply) and not s.price_ambiguous


def test_the_pool_takes_over_from_the_curve_and_a_late_curve_event_never_rolls_it_back():
    s = TokenState("M", None, 0)
    s.on_trade(curve_trade(100, 80, 300_000_000, "c1", 1), 30, 30)
    assert s.mark["src"] == "curve"
    s.on_trade(amm(100, 400, "p1", 2), 30, 30)                                      # the curve's last slot: after it
    assert s.mark["src"] == "amm" and s.migrated
    price = s.curve.price
    s.on_trade(curve_trade(99, 70, 320_000_000, "c0", 3), 30, 30)                    # delayed, older
    assert s.curve.price == price and s.mark["src"] == "amm" and s.marks_refused == 1


def test_dexscreener_never_overrides_a_fresher_chain_price_but_fills_in_a_stale_one():
    s = TokenState("M", None, 0)
    s.on_trade(amm(500, 100, "a", 1000), 30, 30)
    price = s.curve.price
    assert s.external_price(Curve(1, 1, amm=True), True, 1050) and s.curve.price == price      # 50 s old: kept
    assert s.mark["src"] == "amm"
    assert s.external_price(Curve(2 * 280_000_000, 280_000_000, amm=True), True, 1200)        # 200 s old: replaced
    assert s.mark["src"] == "dexscreener" and s.price_at == 1200 and s.mark["seq"] == 2
    s.on_trade(amm(499, 50, "old", 1201), 30, 30)                                    # still older than the chain mark
    assert s.mark["src"] == "dexscreener"
    s.on_trade(amm(501, 110, "new", 1202), 30, 30)
    assert s.mark["src"] == "amm" and s.curve.price == pytest.approx(110 / s.supply)


def test_a_fork_rebuild_keeps_the_newest_mark_and_its_observation_ids_never_repeat():
    from meme_trader.sniper.tracker import TRADE_FIELDS
    assert {"mark", "mark_slot", "mark_seq", "price_ambiguous", "marks_refused"} <= set(TRADE_FIELDS)
    s = TokenState("M", None, 0)
    for t in (amm(200, 100, "c", 3), amm(199, 50, "b", 4), amm(198, 40, "x", 5)):
        s.on_trade(t, 30, 30)
    before = s.mark_seq
    assert s.on_trade(amm(201, 45, "x", 6), 30, 30) == "conflict"       # a fork: x landed again, in a later slot
    assert s.replacements                                                # rebuilt provisionally with that version
    assert s.curve.price == pytest.approx(45 / s.supply) and s.mark["slot"] == 201 and s.mark["signature"] == "x"
    assert s.mark_seq > before and s.mark["seq"] == s.mark_seq


# --------------------------------------------------------------------------- Q1: risk policies
def lose_all(p, coins, t=0.0):
    for c in coins:
        p.try_enter(t, c, -1.0)
    p.close()


def test_the_registered_stop_is_an_entry_trigger_not_a_maximum_daily_loss():
    p = Portfolio(10 * 86400)
    lose_all(p, "abcd")                                                              # four fresh positions, all lost
    assert p.attempted == 4 and -p.economic[0] == pytest.approx(4 * SIZE) and 4 * SIZE > DAY_STOP


def test_the_hard_budget_never_lets_a_day_lose_more_than_its_budget():
    import random
    rng = random.Random(7)
    for trial in range(40):
        p = Portfolio(20 * 86400, risk="hard")
        t = 0.0
        for k in range(300):
            t += rng.expovariate(1 / 900)
            p.try_enter(t, k, rng.choice([-1.0, -0.6, -0.2, 0.1, 0.5, 2.0]))
        p.close()
        assert min(p.economic.values()) >= -DAY_STOP - 1e-9


def test_net_plus_cap_lets_winners_finance_losers_up_to_the_outer_cap():
    p = Portfolio(10 * 86400, risk="net+cap")
    p.try_enter(0, "w", 2.0)                                                         # +0.5 SOL, exits at 3660
    for i, c in enumerate("abcdef"):
        p.try_enter(4000 + i * 3700, c, -1.0)                                        # -0.25 each, one at a time
    p.close()
    assert p.attempted == 1 + 4 and p.lost[0] == pytest.approx(4 * SIZE)             # cap: 1.0 gross


# --------------------------------------------------------------------------- Q1/Q2: power v6, paired
def power():
    spec = importlib.util.spec_from_file_location("power", ROOT / "research/power_t9_e1.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.mark.skipif(not numpy, reason="needs numpy")
def test_power_v6_runs_every_variant_on_the_same_draws():
    import numpy as np
    m = power()
    rng = np.random.default_rng(3)
    out = m.one_test_v6(rng, 20, 1.0, 1.0, "flat", "A1", 0.0, 1.0, 50)
    assert set(out) == {f"{d}/{r}" for d, r in m.VARIANTS}
    base = out["centered/gross"]
    for v in ("positive/gross", "negative/gross"):                                   # same trades admitted...
        assert out[v]["attempted"] == base["attempted"] or abs(out[v]["attempted"] - base["attempted"]) <= 3
    assert out["centered/hard"]["max_open"] <= 1 and out["centered/hard"]["worst_day"] >= -DAY_STOP - 1e-9
    row = m.run_cell_v6((0, dict(scenario="null", win_frac=0.0, magnitude=1.0, ordinary="flat", profile="A2",
                                 extra=0.0, days=20, sims=6), 1.0, 50))
    for v, x in row["variants"].items():
        if v != "centered/gross":
            assert set(x["discordant"]) == {"variant_only", "base_only"} and len(x["paired_pass_diff_ci95"]) == 2
    assert len(m.cells_v6(150, 500)) == 84 and {c["extra"] for c in m.cells_v6(1, 1)} == {0.0, 0.015, 0.028}
