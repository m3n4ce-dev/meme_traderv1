"""Second-life momentum's exploratory forward test (sniper/revival.py): it reads the wallet recorder's live trades,
fires on an established coin jumping on volume, and follows each signal (and a random control coin) at two fill
delays on three exits. Decisions happen when a trade is READ; a quiet pool is censored, not sold; it survives
restarts. Nothing is traded."""
import json
import time

from meme_trader.sniper import revival as rv

DAY = 1_791_300_000.0                            # a fixed UTC day for the file name


def rows(pool, t0, t1, every, px_of, sol=0.05):
    out, t = [], t0
    while t < t1:
        out.append({"t": t, "pool": pool, "mint": "M" + pool, "sym": pool, "px": px_of(t), "sol": sol, "side": "buy"})
        t += every
    return out


def write(dirpath, rs):
    p = dirpath / "wallets" / f"trades-{time.strftime('%Y-%m-%d', time.gmtime(DAY))}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        for r in rs:
            f.write(json.dumps(r) + "\n")
    return p


def quiet_then_jump(pool="P1", start=DAY):
    flat = rows(pool, start, start + 4000, 20, lambda t: 1.0)
    jump = rows(pool, start + 4000, start + 4300, 2, lambda t: 1.0 + (t - start - 4000) / 300 * 0.6, sol=0.5)
    return flat, jump


def test_a_jump_is_followed_from_when_it_was_seen_with_a_control_and_scored(tmp_path):
    flat, jump = quiet_then_jump()
    other = rows("P9", DAY, DAY + 4300 + 7600, 20, lambda t: 1.0)            # an ordinary active pool: the control
    after = rows("P1", DAY + 4300, DAY + 4300 + 7600, 20, lambda t: 2.0 if t < DAY + 4600 else 3.0)
    r = rv.Revival(tmp_path, now=DAY + 3990)
    r.tick(now=DAY + 3990)                                                   # running before the trades arrive
    write(tmp_path, sorted(flat + other[:200], key=lambda r: r["t"]))
    r.tick(now=DAY + 4000)
    write(tmp_path, sorted(jump + after + other[200:], key=lambda r: r["t"]))
    r.tick(now=DAY + 4400)                                                   # read 100 s after the jump's end
    mine = [f for fs in r.open.values() for f in fs if not f["control"]] + [x for x in r.done if not x["control"]]
    assert mine and all(f["decided_at"] >= DAY + 4400 for f in mine if "decided_at" in f)   # decided when seen
    v = r.view()
    rows_ = {(x["rule"], x["delay_s"], x["exit"], x["control"]): x for x in v["table"]}
    a = rows_[("+40% in 5 min, volume x4", 60, "hold 1 h", False)]
    assert a["n"] == 1 and 40 < a["mean_pct"] < 50                          # bought at 2.0 when seen, out at 3.0
    assert ("+40% in 5 min, volume x4", 60, "hold 1 h", True) in rows_      # the control coin, same moment
    assert v["status"] == "exploratory" and v["variants"] == 18
    done = next(x for x in r.done if not x["control"])
    assert done["cost_how"].startswith("flat") and done["lag_s"] >= 100      # no depth reading here: flat cost


def test_nothing_fills_before_it_could_have_been_seen(tmp_path):
    flat, jump = quiet_then_jump()
    r = rv.Revival(tmp_path, now=DAY + 3990)
    r.tick(now=DAY + 3990)
    write(tmp_path, flat + jump)
    r.tick(now=DAY + 10_000)                                                 # the batch shows up hours late
    fills = [f["fill_t"] for fs in r.open.values() for f in fs if "fill_t" in f]
    assert not fills                                                         # no trade after the decision yet
    write(tmp_path, rows("P1", DAY + 10_010, DAY + 10_100, 10, lambda t: 1.6))
    r.tick(now=DAY + 10_100)
    fills = [f["fill_t"] for fs in r.open.values() for f in fs if "fill_t" in f]
    assert fills and min(fills) >= DAY + 10_000


def test_a_quiet_pool_is_censored_not_sold_at_a_stale_mark(tmp_path):
    r = rv.Revival(tmp_path, now=100)
    f = {"pool": "P", "symbol": "T", "mint": "M", "rule": "+40% in 5 min, volume x4", "delay": 60, "control": False,
         "signal_t": 100, "decided_at": 100, "lag_s": 0, "ret5": 0.4, "surge": 4, "cost": 0.012, "cost_how": "flat",
         "p0": 1, "fill_t": 160, "exits": {}}
    r.open = {"P": [f]}
    r.pools = {"P": __import__("collections").deque([(160, 1, 0.1), (200, 3, 0.1)])}
    r.newest_t = 20_000                                                      # other pools traded; P never again
    r._expire(20_000)
    assert r.done and all(x["censored"] and x["pnl_pct"] is None for x in r.done)
    assert all(x["mark_pct"] == 200.0 for x in r.done)                       # the mark is kept, apart
    v = r.view()
    assert all(row["n"] == 0 and row["censored"] == 1 and row["mean_pct"] is None for row in v["table"])


def test_open_follows_and_the_file_position_survive_a_restart(tmp_path):
    flat, jump = quiet_then_jump()
    r = rv.Revival(tmp_path, now=DAY + 3990)
    r.tick(now=DAY + 3990)
    write(tmp_path, flat + jump)
    r.tick(now=DAY + 4310)
    assert r.open
    r.save(force=True)
    again = rv.Revival(tmp_path, now=DAY + 4320)
    assert again.open.keys() == r.open.keys() and again.offset == r.offset and again.fired
    write(tmp_path, rows("P1", DAY + 4310, DAY + 4310 + 7400, 20, lambda t: 2.0))
    again.tick(now=DAY + 4400)
    assert any(not x["control"] for x in again.done)                         # the old follows finished after it


def test_history_read_at_startup_never_fires_and_partial_lines_wait(tmp_path):
    flat, jump = quiet_then_jump("P2")
    p = write(tmp_path, flat + jump)
    r = rv.Revival(tmp_path, now=DAY + 9000)                                 # started after all of it
    for _ in range(3):
        r.tick(now=DAY + 9000)
    assert not r.open and not r.done and r.pools["P2"]                      # history only
    with p.open("a") as f:
        f.write('{"t": 1791309100, "pool": "P2", "px": 1.6, "sol"')         # the recorder mid-write
    r.tick(now=DAY + 9100)
    assert r.rest.startswith(b'{"t": 1791309100')
    with p.open("a") as f:
        f.write(': 0.1, "side": "buy"}\n')
    r.tick(now=DAY + 9110)
    assert r.rest == b"" and r.pools["P2"][-1][0] == 1791309100


def test_costs_come_from_the_pools_depth_at_the_signal(tmp_path):
    (tmp_path / "wallets").mkdir()
    (tmp_path / "wallets" / "pools.json").write_text(json.dumps({"P1": {"liq": [[DAY - 100, 24_000.0], [DAY + 99_999, 1e9]]}}))
    (tmp_path / "wallets" / "status.json").write_text(json.dumps({"sol_usd": 120.0}))
    r = rv.Revival(tmp_path, now=DAY)
    r._load_liq(DAY)
    cost, how = r._cost("P1", DAY)                                           # the reading before the signal, not after
    assert abs(cost - (2 * rv.FEE + 2 * rv.ORDER_SOL / 100.0)) < 1e-9 and "100 SOL" in how
    assert r._cost("P?", DAY) == (rv.FLAT_COST, "flat (depth unknown)")
