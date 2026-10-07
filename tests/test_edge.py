"""The edge check: verdicts a skeptical group would agree with, and the dashboard feed."""
import random

from meme_trader.sniper.edge import as_text, check, report

DAY = 86400


def trades(source, rets, start=1_780_000_000, gap=3600, config="c1", mode="paper"):
    return [{"source": source, "cost": 0.1, "pnl": 0.1 * r, "pnl_pct": r * 100, "opened": start + i * gap,
             "closed": start + i * gap + 60, "config": config, "mode": mode, "symbol": f"T{i}", "mint": f"M{i}",
             "proceeds": 0.1 * (1 + r), "peak_gain_pct": max(r, 0) * 100, "mae_pct": min(r, 0) * 100, "exit": "test",
             "initials": False, "score": 50.0, "p": None, "desk": "", "model": None, "session": "s1", "start_sol": 1.0,
             "entry_delay_s": 0, "exit_delay_s": 0, "entry_vs_signal_pct": 0.0, "exit_vs_signal_pct": 0.0,
             "failed_fees_sol": 0.0} for i, r in enumerate(rets)]


def test_a_steady_winner_over_a_week_looks_real():
    rng = random.Random(1)
    t = trades("late", [rng.gauss(0.08, 0.2) for _ in range(200)], gap=DAY * 8 / 200)
    v = check(t, "late")
    assert v["level"] == "good" and v["lo_pct"] > 0 and v["without_best3_sol"] > 0


def test_three_runners_carrying_everything_is_fragile_not_proof():
    rets = [-0.12] * 120 + [6.0, 5.0, 4.0]
    v = check(trades("late", rets, gap=DAY * 8 / 123), "late")
    assert v["without_best3_sol"] < 0
    assert v["level"] in ("fragile", "unclear")                 # never "looks real"
    assert any("best three trades made" in c for c in v["caveats"])


def test_a_loser_and_a_small_sample_are_called_what_they_are():
    assert check(trades("sniper", [-0.15, -0.1, -0.2] * 20), "sniper")["level"] == "bad"
    assert check(trades("late", [0.5] * 10), "late")["level"] == "early"


def test_short_runs_and_changing_settings_are_flagged():
    rng = random.Random(2)
    t = []
    for k in range(6):                                           # six settings versions in two days
        t += trades("late", [rng.gauss(0.1, 0.1) for _ in range(20)], start=1_780_000_000 + k * 28_000, gap=1000, config=f"c{k}")
    v = check(t, "late")
    assert v["level"] == "early" and "Too few days" in v["verdict"]   # positive, but two days aren't 5 independent ones
    assert any("settings changed 5 times" in c for c in v["caveats"])
    assert any("Paper trades" in c for c in v["caveats"])


def test_the_group_text_is_labelled_and_leaves_your_own_trades_out():
    rep = report(trades("late", [0.1, -0.05] * 20) + trades("manual", [-0.5] * 5), "paper")
    txt = as_text(rep)
    assert "paper trades" in txt and "Graduation plays" in txt and "Your own trades" not in txt
    assert "Your own trades" in as_text(rep, include_manual=True)


def test_the_analytics_feed_carries_the_edge_check(tmp_path):
    import asyncio

    from aiohttp.test_utils import TestClient, TestServer

    from meme_trader.ui.server import make_app
    from tests.test_manual import market

    e = market()
    e.book.closed = trades("late", [0.1, -0.05] * 20, mode="paper")

    async def go():
        async with TestClient(TestServer(make_app(e, data_dir=tmp_path))) as c:
            return await (await c.get("/api/analytics")).json()
    d = asyncio.run(go())
    assert d["edge_check"]["strategies"][0]["source"] == "late" and "Edge check" in d["edge_check"]["text"]
    assert "edge" not in d or "p_positive" in (d["edge"] or {}) or d["edge"] is None    # analytics' own edge confidence stays


def test_endpoint_tests_never_echo_the_url_and_helius_fills_the_rpc(tmp_path, monkeypatch):
    import asyncio

    from meme_trader.ui import keys

    for k in ("SOLANA_RPC_URL", "HELIUS_API_KEY", "SOLANA_WS_URL"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    monkeypatch.setattr(keys, "ENV", env)
    keys.set_key("HELIUS_API_KEY", "abcd1234efgh5678", env)
    assert keys._file_values(env)["SOLANA_RPC_URL"] == keys.HELIUS_RPC + "abcd1234efgh5678"
    st = {s["name"]: s for s in keys.status(env)}
    assert st["SOLANA_RPC_URL"]["hint"] == "mainnet.helius-rpc.com" and st["HELIUS_API_KEY"]["testable"]
    keys.set_key("SOLANA_RPC_URL", "http://127.0.0.1:9/?api-key=secret123", env)   # nothing listens on port 9
    ok, text = asyncio.run(keys.test_key("SOLANA_RPC_URL"))
    assert not ok and "secret123" not in text and "127.0.0.1" not in text
