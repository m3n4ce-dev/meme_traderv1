"""Q1 amendment 5: the stress profile as frozen code - exact bounds, partial identification (no duration assigned to
a censored run), the declared edge cases, and the step durations T9-E1's power may use."""
import math

import pytest

from meme_trader.sniper import q1

A = q1.PROFILE_ALPHA


def test_the_alpha_covers_five_horizons_and_the_rate_jointly():
    assert A == pytest.approx(0.05 / 6) and q1.POWER_PRODUCT == "read_path"


def test_exact_bounds_match_their_closed_forms_and_round_up():
    for n in (1, 5, 20, 200):
        assert q1.cp_upper(0, n, A) == pytest.approx(1 - A ** (1 / n), abs=2e-6)
        assert q1.cp_upper(0, n, A) >= 1 - A ** (1 / n)                    # rounded toward the stress, never away
    assert q1.cp_upper(7, 7, A) == 1.0 and q1.cp_upper(0, 0, A) is None
    assert q1.poisson_upper(0, A) == pytest.approx(-math.log(A), abs=2e-6) and q1.poisson_upper(0, A) >= -math.log(A)
    for k in (1, 4, 30):                                                    # P(X <= k | upper) = alpha
        lam = q1.poisson_upper(k, A)
        cdf = sum(math.exp(i * math.log(lam) - lam - math.lgamma(i + 1)) for i in range(k + 1))
        assert cdf == pytest.approx(A, rel=1e-3) and cdf <= A
    for k, n in ((1, 10), (3, 10), (9, 40)):                               # P(X <= k | n, upper) = alpha
        p = q1.cp_upper(k, n, A)
        cdf = sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k + 1))
        assert cdf == pytest.approx(A, rel=1e-3) and cdf <= A


def run(mn, mx):
    return {"min_s": mn, "max_s": mx}


def test_a_censored_run_is_never_given_a_duration():
    """The fourteenth review's counterexample: one bracketed run of at most 30 s and one right-censored run known
    to have lasted at least 900 s. The censored run counts as possibly longer than EVERY horizon."""
    p = q1.stress_profile({"brackets": [run(10, 30), run(900, None)]}, 10.0)
    s = p["survival"]
    assert [s[k]["upper_count"] for k in ("30 s", "60 s", "15 min", "1 h", "2 h")] == [1, 1, 1, 1, 1]
    assert [s[k]["lower_count"] for k in ("30 s", "60 s", "15 min", "1 h", "2 h")] == [1, 1, 0, 0, 0]
    assert p["censored_runs"] == 1 and p["edge"] == ""
    assert p["durations_for_power"][-1] == [q1.HORIZON_STRESS_S, s["2 h"]["stress"]]


def test_the_profile_is_monotone_and_its_durations_are_a_distribution():
    runs = [run(0, 20), run(5, 45), run(40, 70), run(100, 1200), run(0, 5000), run(3000, None)]
    p = q1.stress_profile({"brackets": runs}, 50.0)
    xs = [p["survival"][k]["stress"] for k in ("30 s", "60 s", "15 min", "1 h", "2 h")]
    assert xs == sorted(xs, reverse=True) and all(0 < x <= 1 for x in xs)
    for key in ("durations_for_power", "durations_sensitivity_6h"):
        d = p[key]
        assert sum(m for _, m in d) == pytest.approx(1.0, abs=1e-5) and all(m > 0 for _, m in d)
        assert [t for t, _ in d] == sorted(t for t, _ in d)
    assert p["durations_sensitivity_6h"][-1][0] == q1.HORIZON_SENSITIVITY_S
    assert p["rate_per_observed_hour"]["detected"] == pytest.approx(6 / 50)
    assert p["rate_per_observed_hour"]["stress"] >= q1.poisson_upper(6, A) / 50 - 1e-9


def test_the_declared_edge_cases():
    none = q1.stress_profile({"brackets": []}, 100.0)
    assert none["edge"] == "no runs" and none["durations_for_power"] == [[q1.HORIZON_STRESS_S, 1.0]]
    assert none["rate_per_observed_hour"]["stress"] == pytest.approx(-math.log(A) / 100, abs=1e-5)
    every = q1.stress_profile({"brackets": [run(0, None), run(60, None)]}, 5.0)
    assert every["edge"] == "all censored" and every["durations_for_power"] == [[q1.HORIZON_STRESS_S, 1.0]]
    assert q1.stress_profile({"brackets": [run(0, 10)]}, 0.0)["unavailable"] == "no observed time"


def test_the_observer_reports_both_products_with_the_stress_label():
    def a(t, down=False, deg=False):
        return {"t": float(t), "down": down, "degraded": deg, "host": "H", "reason": "timeout" if down else "ok"}
    trace = [a(0), a(30, True), a(60), a(90, deg=True), a(120)]
    r = q1.transport_brackets(trace, 0, 150)
    sp = r["stress_profile"]
    assert sp["power_product"] == "read_path" and sp["read_path"]["runs"] == 2 and sp["transport"]["runs"] == 1
    assert sp["read_path"]["label"].startswith("STRESS SCENARIO")
