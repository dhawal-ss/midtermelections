"""Behavioural tests for the live projection model (no database)."""
import numpy as np
import pytest

from election_data.projections.model import (
    Candidate, ModelParams, RaceInput, normalize_party, simulate_chamber, simulate_race,
)

P = ModelParams(simulations=20000)


def run(race, seed=1):
    return simulate_race(race, P, np.random.default_rng(seed))


def dr(d_votes, r_votes, pct, **kw):
    return RaceInput("2026:general:TEST", [Candidate("Dem", "Democratic", d_votes),
                                           Candidate("Rep", "Republican", r_votes)], pct_reporting=pct, **kw)


def test_party_normalization():
    assert normalize_party("Democratic") == "DEM" and normalize_party("DFL") == "DEM"
    assert normalize_party("REP") == "REP" and normalize_party("Libertarian") == "OTHER"


def test_certified_count_is_final():
    p = run(dr(1000, 999, 100, final=True))
    assert p.status == "final_count"
    assert p.candidates[0]["win_prob"] == 1.0


def test_complete_count_with_clear_lead_is_projected():
    p = run(dr(550_000, 450_000, 99))
    assert p.status == "projected" and p.leader["label"] == "Dem"


def test_early_lead_is_not_overtrusted():
    # 55-45 with 2% in and an even baseline: should not be anywhere near a projection
    p = run(dr(5_500, 4_500, 2, prior_margin=0.0, prior_sd=0.10))
    assert p.leader["win_prob"] < 0.95
    assert p.status in ("tossup", "lean")


def test_more_counted_means_more_confidence():
    probs = [run(dr(52 * k, 48 * k, pct)).leader["win_prob"] for k, pct in ((100, 10), (1000, 50), (1900, 95))]
    assert probs[0] < probs[1] < probs[2]


def test_baseline_drives_probability_before_results():
    p = run(dr(0, 0, 0, prior_margin=0.20, prior_sd=0.10, prior_basis="2024 House result"))
    assert p.status == "prior_only"
    assert p.candidates[0]["win_prob"] > 0.9


def test_strong_baseline_tempers_early_contrary_lead():
    # Early R lead in a D+20 seat at 3% counted: D should still be favoured
    p = run(dr(4_000, 6_000, 3, prior_margin=0.20, prior_sd=0.08))
    assert p.candidates[0]["win_prob"] > 0.5


def test_razor_thin_near_complete_is_tossup():
    p = run(dr(500_100, 500_000, 97))
    assert p.status in ("tossup", "lean")


def test_projected_share_interval_contains_mean():
    p = run(dr(300_000, 200_000, 60))
    for c in p.candidates:
        assert c["proj_low"] <= c["proj_share"] <= c["proj_high"]
    assert abs(sum(c["proj_share"] for c in p.candidates) - 1) < 1e-6


def test_same_party_runoff_and_majority_probability():
    race = RaceInput("2026:general:CA", [Candidate("A", "Democratic", 400), Candidate("B", "Democratic", 600)],
                     pct_reporting=40)
    p = run(race)
    assert p.leader["label"] == "B"
    ga = RaceInput("GA", [Candidate("D", "DEM", 480), Candidate("R", "REP", 470), Candidate("L", "LIB", 50)],
                   pct_reporting=90, majority_required=True)
    g = run(ga)
    assert all("majority_prob" in c for c in g.candidates)
    assert g.candidates[0]["majority_prob"] < 0.5   # 48% with a 5% third party: runoff likely


def test_reproducible_with_seed():
    a, b = run(dr(100, 90, 20), seed=3), run(dr(100, 90, 20), seed=3)
    assert a.candidates == b.candidates


def test_chamber_tie_breaker_and_holdovers():
    sims = 4
    # Two races: DEM wins race 1 always; race 2 alternates. Holdovers 49 D / 49 R; need 51.
    r1 = np.zeros(sims, dtype=np.int8)
    r2 = np.array([0, 1, 0, 1], dtype=np.int8)
    cp = simulate_chamber("senate", [r1, r2], [], {"DEM": 49, "REP": 49}, 51, tie_breaker="REP")
    # draws: DEM 51 (control), DEM 50 / REP 50 (tie -> REP), ...
    assert cp.control_prob["DEM"] == pytest.approx(0.5)
    assert cp.control_prob["REP"] == pytest.approx(0.5)
    assert cp.expected_seats["DEM"] == pytest.approx(50.5)


def test_correlated_national_shock_widens_chamber_outcomes():
    rng = np.random.default_rng(0)
    params = ModelParams(simulations=5000, national_sd=0.0)
    params_corr = ModelParams(simulations=5000, national_sd=0.06)
    races = [dr(0, 0, 0, prior_margin=0.0, prior_sd=0.05, prior_basis="x") for _ in range(40)]

    def spread(prm):
        shocks = rng.normal(0, prm.national_sd, prm.simulations)
        draws = [simulate_race(r, prm, rng, shocks).winner_party_draws for r in races]
        return (np.vstack(draws) == 0).sum(axis=0).std()

    assert spread(params_corr) > spread(params) * 1.5
