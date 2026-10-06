"""Second-life momentum's forward test (sniper/revival.py): it reads the wallet recorder's live trades, fires on an
established coin jumping on volume, and follows each signal at two fill delays on three exits. Nothing is traded."""
import json
import time

from meme_trader.sniper import revival as rv

DAY = 1_791_300_000.0                            # a fixed UTC day for the file name


def rows(pool, t0, t1, every, px_of, sol=0.05):
    out, t = [], t0
    while t < t1:
        out.append({"t": t, "pool": pool, "mint": "M" + pool, "sym": "OLD", "px": px_of(t), "sol": sol, "side": "buy"})
        t += every
    return out


def write(dirpath, rs, mode="a"):
    p = dirpath / "wallets" / f"trades-{time.strftime('%Y-%m-%d', time.gmtime(DAY))}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open(mode) as f:
        for r in rs:
            f.write(json.dumps(r) + "\n")
    return p


def test_a_jump_on_volume_is_followed_at_both_delays_and_scored(tmp_path):
    flat = rows("P1", DAY, DAY + 4000, 20, lambda t: 1.0)                       # an hour+ of quiet trading
    jump = rows("P1", DAY + 4000, DAY + 4300, 2, lambda t: 1.0 + (t - DAY - 4000) / 300 * 0.6, sol=0.5)
    after = rows("P1", DAY + 4300, DAY + 4300 + 7400, 20, lambda t: 2.0)        # it keeps the gain
    write(tmp_path, flat)
    r = rv.Revival(tmp_path, now=DAY + 3990)                                    # started before the jump
    r.tick(now=DAY + 4000)
    assert not r.open                                                          # the quiet hour: nothing
    write(tmp_path, jump + after)
    r.tick(now=DAY + 4000)
    names = {(x["rule"], x["delay"], x["exit"]) for x in r.done}
    assert ("+40% in 5 min, volume x4", 60, "hold 2 h") in names and ("+20% in 5 min, volume x4", 5, "hold 1 h") in names
    hold = next(x for x in r.done if x["rule"].startswith("+40") and x["delay"] == 60 and x["exit"] == "hold 1 h")
    assert hold["pnl_pct"] > 20 and hold["why"] == "time"
    v = r.view()
    assert v["signals"] == 3 and {row["delay_s"] for row in v["table"]} == {5, 60}
    assert (tmp_path / "revival.jsonl").exists() and rv.Revival(tmp_path).done          # kept across restarts


def test_history_read_at_startup_never_fires_and_partial_lines_wait(tmp_path):
    old = rows("P2", DAY, DAY + 4000, 20, lambda t: 1.0) + rows("P2", DAY + 4000, DAY + 4300, 2, lambda t: 1.6, sol=0.5)
    p = write(tmp_path, old)
    r = rv.Revival(tmp_path, now=DAY + 9000)                                    # started after all of it
    r.tick(now=DAY + 9000)
    assert not r.open and not r.done and r.pools["P2"]                         # history only
    with p.open("a") as f:
        f.write('{"t": 1791309100, "pool": "P2", "px": 1.6, "sol"')            # the recorder mid-write
    r.tick(now=DAY + 9000)
    assert r.rest.startswith(b'{"t": 1791309100')
    with p.open("a") as f:
        f.write(': 0.1, "side": "buy"}\n')
    r.tick(now=DAY + 9000)
    assert r.rest == b"" and r.pools["P2"][-1][0] == 1791309100
