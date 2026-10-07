"""The T9 signal's exact boundaries (a seventh review, 2026-10-07): one shared implementation, as registered."""
import pytest

from meme_trader.sniper.t9_signal import signals


def p(*rows):
    return [(t, px, True, sol) for t, px, sol in rows]


def test_a_trade_exactly_at_t_minus_300_is_the_price_5_minutes_ago():
    out = signals(p((0, 1, 1), (3300, 1, 1), (3600, 2, 1), (3900, 2.7, 1)))
    assert out[-1][0] == 3900 and out[-1][1] == pytest.approx(2.7 / 2 - 1)        # +35%, not +170%


def test_the_15_minute_price_is_at_or_before_t_minus_900_too():
    out = signals(p((0, 1, 1), (3000, 1.5, 1), (3600, 2, 1), (3900, 3, 1)))
    assert out[-1][2] == pytest.approx(3 / 1.5 - 1)


def test_the_volume_windows_and_their_edges():
    # v60 = [t-3900, t-300): the trade at 0 is in, the one at 3600 isn't (it's in v5: t-300 <= ts <= t)
    out = signals(p((0, 1, 6.0), (1000, 1, 6.0), (3600, 1, 1.0), (3900, 1, 2.0)))
    assert out[-1][3] == pytest.approx((1.0 + 2.0) / (12.0 / 12))


def test_not_enough_history_and_the_check_cadence():
    assert signals(p((0, 1, 1), (3899, 2, 1))) == []                              # 65 minutes not yet seen
    out = signals(p((0, 1, 1), (3900, 2, 1), (3910, 2, 1), (3930, 2, 1)))
    assert [x[0] for x in out] == [3900, 3930]                                      # at most every 30 s


def test_same_timestamp_trades_count_up_to_the_checked_one():
    out = signals(p((0, 1, 1), (3600, 2, 1), (3900, 3, 1), (3900, 4, 5)))
    assert len(out) == 1 and out[0][1] == pytest.approx(3 / 2 - 1)                 # checked at the first 3900 trade
