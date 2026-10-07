"""Execution and censoring over recorded trades (replay_exec.py, a sixth review 2026-10-07): a fill is only ever a
recorded trade at or after its time, inside what the recording observed."""
import pytest

from meme_trader.sniper.replay_exec import (CENSORED, CLOSED, PENDING, UNMEASURED, Coverage, fill_at, is_censored,
                                            simulate, timer_due)


def series(points):
    return [t for t, _ in points], [p for _, p in points]


def test_a_horizon_past_the_recording_is_pending_not_the_last_print():
    ts, px = series([(60, 1.0), (120, 10.0)])
    o = simulate(ts, px, 0, 60, 3600, 0.012, Coverage(60, 120))
    assert o.status == PENDING and o.ret is None and o.mark_ret == pytest.approx(10 - 1 - 0.012)   # a mark, not a P&L


def test_a_missing_delayed_exit_never_fills_at_the_trigger():
    ts, px = series([(60, 1.0), (120, 2.0)])
    o = simulate(ts, px, 0, 60, 3600, 0.012, Coverage(60, 120), take=0.5)
    assert o.status == PENDING and o.exit_why == "take" and o.exit_trigger_t == 120 and o.ret is None


def test_a_position_held_across_midnight_gets_the_next_days_first_eligible_exit():
    day = 86400                                          # one series across the day boundary, not two cut ones
    ts, px = series([(day - 600, 1.0), (day + 1000, 1.5), (day + 3500, 1.6)])
    o = simulate(ts, px, day - 660, 60, 3600, 0.0, Coverage(0, 2 * day))
    assert o.status == CLOSED and o.entry_t == day - 600 and o.exit_t == day + 3500 and o.ret == pytest.approx(0.6)
    ts2, px2 = series([(day - 600, 1.0), (day + 1000, 1.5)])     # nothing after due + delay, observed long after
    assert simulate(ts2, px2, day - 660, 60, 3600, 0.0, Coverage(0, 2 * day)).status == CENSORED


def test_the_timer_fills_at_the_first_trade_after_due_plus_delay():
    day = 86400
    ts, px = series([(day - 600, 1.0), (day + 3000, 1.2), (day + 3100, 1.4)])
    o = simulate(ts, px, day - 660, 60, 3600, 0.0, Coverage(0, 2 * day))
    assert o.status == CLOSED and o.exit_t == day + 3100 and o.ret == pytest.approx(0.4)


def test_a_quiet_pool_is_censored_with_a_mark_never_a_sale():
    ts, px = series([(60, 1.0), (100, 0.8)])
    o = simulate(ts, px, 0, 60, 3600, 0.01, Coverage(0, 100_000))
    assert o.status == CENSORED and o.ret is None and o.mark_ret == pytest.approx(-0.21)


def test_a_fill_window_in_a_recording_gap_is_unmeasured():
    ts, px = series([(10, 1.0), (5000, 2.0)])
    o = simulate(ts, px, 0, 60, 3600, 0.0, Coverage(0, 100_000, gaps=[(30, 4000)]))
    assert o.status == UNMEASURED and o.why.startswith("entry")       # the recorder was down when it would fill
    assert timer_due(60, 3600, [(3000, 4500)]) == 4500        # a due time inside a gap: when observation resumes
    ts, px = series([(60, 1.0), (5000, 2.0)])
    o = simulate(ts, px, 0, 60, 3600, 0.0, Coverage(0, 100_000, gaps=[(3000, 4500)]))
    assert o.status == CLOSED and o.exit_t == 5000                     # ...and fills after it


def test_a_stop_fills_at_the_first_trade_after_it_plus_the_delay():
    ts, px = series([(60, 1.0), (200, 0.6), (230, 0.5), (300, 0.55)])
    o = simulate(ts, px, 0, 60, 3600, 0.0, Coverage(0, 10_000), stop=0.3)
    assert o.status == CLOSED and o.exit_why == "stop" and o.exit_trigger_t == 200 and o.exit_t == 300


def test_the_timer_wins_over_a_stop_on_its_due_trade():
    ts, px = series([(60, 1.0), (3660, 0.5), (3720, 0.5)])
    o = simulate(ts, px, 0, 60, 3600, 0.0, Coverage(0, 10_000), stop=0.3)
    assert o.exit_why == "time"


def test_entry_needs_a_trade_inside_the_dead_window():
    ts, px = series([(10, 1.0), (5000, 1.0)])
    assert fill_at(ts, px, 60, Coverage(0, 10_000)).status == CENSORED
    assert fill_at(ts, px, 60, Coverage(0, 1000)).status == PENDING


def test_streaming_and_batch_censoring_agree():
    ts, px = series([(60, 1.0), (100, 0.8)])
    due = 3660
    batch = fill_at(ts, px, due + 60, Coverage(0, 100_000), due=due)
    assert batch.status == CENSORED
    assert not is_censored(due + 1799, 100, due) and is_censored(due + 1800, 100, due)
