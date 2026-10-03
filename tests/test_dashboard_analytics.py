"""Analytics, live controls, gate audit, defensive mode, dashboard API and feed recording."""
import asyncio
import copy
import gzip
import json

import pytest
import yaml

from meme_trader import config
from meme_trader.sniper import jsonsafe
from meme_trader.sniper.analytics import compute, exit_key, monte_carlo
from meme_trader.sniper.engine import Engine
from meme_trader.sniper.events import Launch, Trade, dumps
from meme_trader.sniper.execution import PaperExecutor
from meme_trader.sniper.feeds import FileFeed, SyntheticFeed, compress_file, dedupe_feed_paths
from meme_trader.sniper.report import write as write_report
from meme_trader.sniper.tracker import TokenState
from tests.test_review_fixes import Quiet

P = config.load(config.EXAMPLE)
MINT = "A" * 40 + "pump"


@pytest.fixture(autouse=True)
def tmp_data(monkeypatch, tmp_path):
    for mod in ("meme_trader.journal", "meme_trader.sniper.engine"):
        monkeypatch.setattr(f"{mod}.DATA", tmp_path)


def engine(realtime=False, **kw):
    p = copy.deepcopy(P)
    return Engine(p, Quiet(realtime), PaperExecutor(p.sniper.execution), mode="paper", log_to_journal=False, **kw)


def trade_row(pnl_pct, i=0, source="sniper", exit_="trailing stop -20% from peak (+60%)", p=None, cost=0.05):
    t = 1_780_000_000 + i * 120
    pnl = cost * pnl_pct / 100
    return {"mint": f"m{i}", "symbol": f"S{i}", "opened": t, "closed": t + 60, "cost": cost, "proceeds": cost + pnl,
            "pnl": pnl, "pnl_pct": pnl_pct, "peak_gain_pct": max(pnl_pct, 0) + 20, "score": 70, "initials": False,
            "exit": exit_, "source": source, "desk": "", "p": p, "mae_pct": -5.0}


# ------------------------------------------------------------------ analytics
def test_kpis_breakdowns_and_callouts_kept_apart():
    rows = [trade_row(100, 0), trade_row(-25, 1, exit_="stop loss -25%"), trade_row(50, 2, source="copy:alpha"),
            trade_row(-10, 3, source="callout", exit_="callout hold done")]
    a = compute(rows, [], 1.0, 40)
    k = a["kpis"]
    assert k["n"] == 3 and k["wins"] == 2 and k["win_rate"] == pytest.approx(2 / 3)
    assert k["profit_factor"] == pytest.approx((0.05 + 0.025) / 0.0125)
    assert k["payoff"] == pytest.approx(75 / 25)
    assert {r["key"] for r in a["by_source"]} == {"sniper", "copy"}
    assert {r["key"] for r in a["by_exit"]} == {"trailing stop", "stop loss"}
    assert a["callouts"]["n"] == 1
    assert a["monte_carlo"] is None and a["insights"][0]["level"] == "warn"     # 3 trades: noisy, no projection


def test_all_wins_profit_factor_survives_json_for_the_browser():
    a = compute([trade_row(40, i) for i in range(12)], [], 1.0, 40)
    assert a["kpis"]["profit_factor"] == float("inf")
    text = jsonsafe.dumps(a)

    def reject(c):
        raise ValueError(f"bare {c} would break JSON.parse")
    json.loads(text, parse_constant=reject)                 # strict: what a browser accepts
    assert '"Infinity"' in text


def test_monte_carlo_is_deterministic_and_sees_the_kill_switch():
    win = [0.01, -0.005, 0.02, -0.004] * 5
    assert monte_carlo(win, 1.0, 1.0, 40) == monte_carlo(win, 1.0, 1.0, 40)
    good = monte_carlo(win, 1.0, 1.0, 40)
    assert good["p_profit"] > 0.9 and good["p_kill_switch"] == 0
    bad = monte_carlo([-0.06] * 12, 1.0, 1.0, 40)
    assert bad["p_kill_switch"] == 1.0 and bad["median_final"] >= 0.6 - 0.06   # trading stops at the switch
    fan = good["fan"]
    assert fan[0]["trade"] == 0 and fan[-1]["trade"] == 100
    assert all(f["p5"] <= f["p50"] <= f["p95"] for f in fan)


def test_exit_reasons_group_cleanly():
    assert exit_key("trailing stop -22% from peak (+80%)") == "trailing stop"
    assert exit_key("leader sim-bait-kol sold 100%") == "leader sold"
    assert exit_key("pre-graduation exit (curve 99%)") == "pre-graduation exit"
    assert exit_key(None) == "?"


def test_report_writes_html_and_csv(tmp_path):
    rows = [trade_row(p, i) for i, p in enumerate([60, -20, 35, -15, 120, -30, 10, 25, -5, 80, 15, -12])]
    eq = [(r["closed"], 1.0 + sum(x["pnl"] for x in rows[: i + 1])) for i, r in enumerate(rows)]
    html_path, csv_path = write_report(rows, eq, 1.0, 40, tmp_path / "r.html", "Test", "unit test")
    page = html_path.read_text()
    assert "<svg" in page and "Highlights" in page and "Infinity" not in page
    assert len(csv_path.read_text().splitlines()) == len(rows) + 1


# ------------------------------------------------------------------ gate audit / defense / controls
def priced(eng, mint=MINT):
    s = eng.tokens[mint] = TokenState(mint, Launch(mint, 0, "dev"), 0)
    s.on_launch(s.launch)
    return s


def trade_at(s, mult, ts):
    k = s.curve.v_sol * s.curve.v_tokens
    v_sol = (k * s.launch.v_sol / s.launch.v_tokens * mult) ** 0.5
    return Trade(s.mint, ts, "w", "buy", 0.1, 0, v_sol, k / v_sol)


def test_gate_audit_follows_rejects_by_first_passage():
    eng = engine()
    s = priced(eng)
    eng._audit_start(s, "dev bought")
    eng._audit_start(s, "bundle")                           # first decision wins
    for ts, mult in ((5, 1.5), (10, 2.1), (20, 0.5)):       # doubled before -30%: a missed runner
        asyncio.run(eng.handle(trade_at(s, mult, ts)))
    assert eng._audit_view(MINT)["outcome"] == "win"
    eng.now = 1000
    eng._audit_settle()
    row = eng.gate_audit()[0]
    assert row["gate"] == "dev bought" and row["n"] == 1 and row["win_first_pct"] == 100 and row["avg_peak_x"] >= 2


def test_backtest_end_drops_audits_cut_short():
    eng = engine()
    s = priced(eng)
    eng._audit_start(s, "curve")
    eng.now = 30
    eng._audit_settle(final=True)
    assert not eng.audit and not eng.gate_audit()


def test_defense_mode_after_a_losing_streak():
    eng = engine()
    eng.book.closed = [trade_row(-20, i) for i in range(4)] + [trade_row(-2, 4, source="callout")]
    eng._update_defense()
    assert not eng._defensive()                             # callout bags don't count toward the streak
    eng.book.closed.append(trade_row(-20, 5, source="copy:x"))
    eng._update_defense()
    assert eng._defensive() and "5 losses" in eng.defense_reason
    snap = eng.snapshot()
    assert snap["defense"]["on"] and snap["defense"]["minutes_left"] > 0


def test_controls_validate_apply_and_save_without_touching_other_settings(tmp_path):
    eng = engine()
    assert "between" in eng.set_control("entry.min_score", 101)
    assert "one of" in eng.set_control("exit.profile", "moon")
    assert eng.set_control("nope.key", 1).startswith("unknown")
    assert "max buy" in eng.set_control("sizing.base_usd", eng.p.sizing.max_usd + 1)
    assert eng.set_control("entry.min_score", "61") == "" and eng.p.entry.min_score == 61
    assert eng.set_control("late.enabled", "on") == "" and eng.p.late.enabled is True
    path = tmp_path / "params.yaml"
    path.write_text("mode: paper\nwallet:\n  pubkey: KEEPME\nsniper:\n  copy:\n    leaders: []\n  late:\n")
    eng.save_controls(path)
    saved = yaml.safe_load(path.read_text())
    assert saved["wallet"]["pubkey"] == "KEEPME" and saved["sniper"]["copy"]["leaders"] == []
    assert saved["sniper"]["entry"]["min_score"] == 61 and saved["sniper"]["late"]["enabled"] is True
    reloaded = config.load(path)                            # still a valid config, merged over the defaults
    assert reloaded.sniper.entry.min_score == 61 and reloaded.sniper.exit.profile == "trail"
    assert {c["key"] for c in eng.controls()} >= {"entry.enabled", "predict.min_p", "exit.profile"}


def test_entries_off_still_tracks_tokens_for_callouts():
    eng = engine()
    eng.set_control("entry.enabled", False)
    s = priced(eng)
    asyncio.run(eng._check_entry(s))
    assert s.decided == "sniper off"


def test_snapshot_fields_and_cached_analytics():
    feed = SyntheticFeed(seed=6, speed=0, launches=120, start_ts=1_780_000_000)
    p = copy.deepcopy(P)
    eng = Engine(p, feed, PaperExecutor(p.sniper.execution), mode="backtest", log_to_journal=False)
    asyncio.run(eng.run())
    snap = eng.snapshot()
    assert snap["type"] == "snapshot" and snap is eng.snapshot()          # built once per tick
    assert {"defense", "model", "strategies", "tracked_tokens"} <= set(snap)
    assert all({"p", "links"} <= set(w) for w in snap["watching"])
    a = eng.analytics()
    assert a is eng.analytics() and "kpis" in a
    mint = next(iter(eng.tokens))
    d = eng.token_detail(mint)
    assert d["checklist"] and len(d["features"]) == 36 and eng.token_detail("nope") is None


# ------------------------------------------------------------------ dashboard API
def test_dashboard_api_and_settings_over_the_socket():
    from aiohttp.test_utils import TestClient, TestServer

    from meme_trader.ui.server import make_app

    eng = engine()
    eng.book.closed = [trade_row(40, i) for i in range(3)]         # all wins: profit factor = inf
    priced(eng)

    async def go():
        async with TestClient(TestServer(make_app(eng))) as c:
            host = f"127.0.0.1:{c.port}"
            h = {"Host": host}
            rebound = await c.get("/api/analytics", headers={"Host": "attacker.example"})
            a = await c.get("/api/analytics", headers=h)
            text = await a.text()
            tok = await c.get(f"/api/token/{MINT}", headers=h)
            missing = await c.get("/api/token/xyz", headers=h)
            ctl = await c.get("/api/controls", headers=h)
            ws = await c.ws_connect("/ws", headers={"Origin": f"http://{host}", **h})
            snap = json.loads((await ws.receive()).data)
            await ws.send_str("not json")
            await ws.send_json({"action": "set", "key": "entry.min_score", "value": 64})
            while True:
                msg = json.loads((await ws.receive()).data)
                if msg.get("type") == "ack":
                    break
            await ws.close()
            return rebound.status, a.status, text, tok.status, (await tok.json()), missing.status, \
                (await ctl.json()), snap, msg
    rebound, a_status, text, tok_status, tok, missing, ctl, snap, ack = asyncio.run(go())
    assert rebound == 403 and a_status == 200 and tok_status == 200 and missing == 404
    json.loads(text, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
    assert tok["mint"] == MINT and tok["checklist"]
    assert any(c["key"] == "exit.profile" for c in ctl)
    assert snap["type"] == "snapshot"
    assert ack["ok"] and ack["key"] == "entry.min_score" and eng.p.entry.min_score == 64


# ------------------------------------------------------------------ recording
def _events(n=30):
    return [Launch(f"M{i}", float(i), "dev", symbol=f"T{i}") for i in range(n)]


def _read(*paths):
    f = FileFeed(*paths)

    async def go():
        return [e async for e in f.events()]
    return asyncio.run(go()), f


def test_gz_feeds_replay_and_survive_crash_damage(tmp_path):
    plain = tmp_path / "feed-2026-10-01.jsonl"
    plain.write_text("".join(dumps(e) + "\n" for e in _events()) + '{"kind": "launch", "mint": "half')
    evs, f = _read(plain)
    assert len(evs) == 30 and f.bad_lines == 1                         # half-written last line skipped
    gz = compress_file(plain)
    assert gz.exists() and not plain.exists()
    evs, _ = _read(gz)
    assert [e.mint for e in evs] == [f"M{i}" for i in range(30)]
    raw = gzip.compress("".join(dumps(e) + "\n" for e in _events(500)).encode())
    cut = tmp_path / "feed-2026-10-02.jsonl.gz"
    cut.write_bytes(raw[: len(raw) // 2])                              # writer killed mid-file
    evs, f = _read(cut)
    assert 0 < len(evs) < 500 and f.bad_lines >= 1
    both = [tmp_path / "feed-2026-10-03.jsonl", tmp_path / "feed-2026-10-03.jsonl.gz", tmp_path / "x.jsonl"]
    assert [p.name for p in dedupe_feed_paths(both)] == ["feed-2026-10-03.jsonl.gz", "x.jsonl"]


def test_recording_rotates_at_utc_midnight(tmp_path):
    eng = engine(record_path=tmp_path / "feed-2026-10-01.jsonl")
    eng.now = 1_790_000_000                                            # some other UTC day
    eng._rotate_record()
    assert eng.record_path.name != "feed-2026-10-01.jsonl" and eng.record_path.exists()
    eng.record_file.close()
