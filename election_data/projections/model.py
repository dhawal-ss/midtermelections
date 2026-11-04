"""Live race projection model (heuristic v1).

For each race the model answers: given the votes counted so far, the share of
the expected vote that is in, and a pre-election baseline, how is the race
likely to finish?

Per Monte Carlo draw:
  1. Expected total votes: votes_counted / pct_reporting, with uncertainty that
     shrinks as more is counted (or a turnout prior when nothing is in).
  2. Baseline: prior DEM-minus-REP margin + national shift + a national shock
     shared by every race in the draw (correlation) + race-level noise.
  3. Uncounted-vote composition: a blend of the counted shares and the baseline,
     weighted pct / (pct + k) — early-counted votes are not assumed
     representative — then perturbed with log-share noise.
  4. Final votes = counted + remaining * shares; winner = most votes (top-k for
     multi-seat contests).

Outputs are probabilities and ranges, never calls. The model status ladder
(tossup/lean/likely/projected) is a statement about the model, distinct from
official certification and from media race calls.

All parameters live in config/sources.yaml -> model. Snapshots of every input
and output are stored so the model can be backtested and recalibrated.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

PARTY_CODES = {"DEM": 0, "REP": 1, "OTHER": 2}
_DEM = {"dem", "democrat", "democratic", "democratic party", "dfl", "democratic-farmer-labor", "d"}
_REP = {"rep", "republican", "republican party", "gop", "r"}


def normalize_party(party: str | None) -> str:
    p = (party or "").strip().lower()
    if p in _DEM or p.startswith("democrat"):
        return "DEM"
    if p in _REP or p.startswith("republican"):
        return "REP"
    return "OTHER"


@dataclass
class Candidate:
    label: str
    party: str
    votes: int = 0


@dataclass
class RaceInput:
    contest_key: str
    candidates: list[Candidate]
    pct_reporting: float | None = None       # 0..100, as published by the source
    prior_margin: float = 0.0                 # DEM minus REP two-party share, -1..1
    prior_sd: float = 0.15
    turnout_prior: float | None = None
    prior_basis: str = "none"
    seats: int = 1
    majority_required: bool = False
    final: bool = False                       # certified count: no remaining votes


@dataclass
class ModelParams:
    simulations: int = 10000
    national_shift: float = 0.0
    national_sd: float = 0.04
    observed_weight_k: float = 20.0
    remaining_vote_sd: float = 0.08
    turnout_sd: float = 0.30
    projected: float = 0.995
    likely: float = 0.95
    lean: float = 0.75
    min_pct_for_projection: float = 5.0
    version: str = "live-v1"

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "ModelParams":
        t = cfg.get("status_thresholds", {})
        return cls(
            simulations=int(cfg.get("simulations", 10000)),
            national_shift=float(cfg.get("national_shift", 0.0)),
            national_sd=float(cfg.get("national_sd", 0.04)),
            observed_weight_k=float(cfg.get("observed_weight_k", 20)),
            remaining_vote_sd=float(cfg.get("remaining_vote_sd", 0.08)),
            turnout_sd=float(cfg.get("turnout_sd", 0.30)),
            projected=float(t.get("projected", 0.995)),
            likely=float(t.get("likely", 0.95)),
            lean=float(t.get("lean", 0.75)),
            min_pct_for_projection=float(cfg.get("min_pct_for_projection", 5)),
            version=str(cfg.get("version", "live-v1")),
        )


@dataclass
class RaceProjection:
    contest_key: str
    status: str
    votes_counted: int
    pct_reporting: float | None
    expected_total: tuple[float, float, float]      # mean, p10, p90
    leader_index: int | None
    candidates: list[dict[str, Any]]
    winner_party_draws: np.ndarray = field(repr=False, default_factory=lambda: np.zeros(0, dtype=np.int8))

    @property
    def leader(self) -> dict[str, Any] | None:
        return self.candidates[self.leader_index] if self.leader_index is not None else None


def _softmax_rows(logits: np.ndarray) -> np.ndarray:
    z = logits - logits.max(axis=1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=1, keepdims=True)


def _prior_shares(race: RaceInput, parties: np.ndarray, obs_shares: np.ndarray | None,
                  margin_draws: np.ndarray) -> np.ndarray:
    """Baseline share of each candidate in each draw, shape (N, n)."""
    n, sims = len(parties), len(margin_draws)
    within = np.ones(n)
    if obs_shares is not None:
        within = np.maximum(obs_shares, 1e-6)
    has_dem, has_rep = (parties == 0).any(), (parties == 1).any()
    is_other = parties == 2
    other_total = 0.0
    if is_other.any():
        other_total = float(obs_shares[is_other].sum()) if obs_shares is not None else 0.03 * is_other.sum()
        other_total = min(max(other_total, 0.005), 0.6)

    shares = np.zeros((sims, n))
    if has_dem and has_rep:
        m = np.clip(margin_draws, -0.98, 0.98)
        major = 1.0 - other_total
        party_share = {0: (1 + m) / 2 * major, 1: (1 - m) / 2 * major}
        for code in (0, 1):
            idx = np.where(parties == code)[0]
            w = within[idx] / within[idx].sum()
            shares[:, idx] = party_share[code][:, None] * w[None, :]
        if is_other.any():
            idx = np.where(is_other)[0]
            w = within[idx] / within[idx].sum()
            shares[:, idx] = other_total * w[None, :]
    else:
        # Same-party (e.g. top-two D vs D) or nonpartisan contest: no partisan baseline applies.
        shares[:] = (within / within.sum())[None, :]
    return shares


def simulate_race(race: RaceInput, params: ModelParams, rng: np.random.Generator,
                  national_shock: np.ndarray | None = None) -> RaceProjection:
    sims = params.simulations
    cands = race.candidates
    n = len(cands)
    if national_shock is None:
        national_shock = rng.normal(0.0, params.national_sd, sims)
    votes = np.array([max(int(c.votes or 0), 0) for c in cands], dtype=float)
    parties = np.array([PARTY_CODES[normalize_party(c.party)] for c in cands], dtype=np.int8)
    counted = float(votes.sum())

    if n == 0:
        return RaceProjection(race.contest_key, "no_results", 0, race.pct_reporting, (0, 0, 0), None, [],
                              np.full(sims, -1, dtype=np.int8))

    # -- share of expected vote counted ------------------------------------
    if race.pct_reporting is not None:
        p = min(max(race.pct_reporting / 100.0, 0.0), 1.0)
    elif race.turnout_prior and counted > 0:
        p = min(counted / race.turnout_prior, 0.98)
    else:
        p = 0.0
    if race.final:
        p = 1.0

    # -- remaining votes ---------------------------------------------------
    if p >= 1.0 or race.final:
        remaining = np.zeros(sims)
    elif counted > 0 and p > 0:
        expected_remaining = counted / p - counted
        spread = params.turnout_sd * (1 - p) + 0.05
        remaining = expected_remaining * rng.lognormal(0.0, spread, sims)
    elif race.turnout_prior:
        remaining = np.clip(race.turnout_prior * rng.lognormal(0.0, params.turnout_sd, sims) - counted, 0, None)
    else:
        remaining = np.ones(sims)  # unit electorate: only shares matter

    # -- composition of the uncounted vote ---------------------------------
    obs_shares = votes / counted if counted > 0 else None
    margin_draws = (race.prior_margin + params.national_shift + national_shock
                    + rng.normal(0.0, race.prior_sd, sims))
    prior = _prior_shares(race, parties, obs_shares, margin_draws)
    if obs_shares is not None:
        pct = p * 100.0
        w = pct / (pct + params.observed_weight_k)
        mean_shares = w * obs_shares[None, :] + (1 - w) * prior
    else:
        mean_shares = prior
    logits = np.log(np.maximum(mean_shares, 1e-9)) + rng.normal(0.0, params.remaining_vote_sd, (sims, n))
    remaining_shares = _softmax_rows(logits)

    final_votes = votes[None, :] + remaining[:, None] * remaining_shares
    final_shares = final_votes / np.maximum(final_votes.sum(axis=1, keepdims=True), 1e-9)

    # -- winners -----------------------------------------------------------
    seats = max(1, min(race.seats, n))
    if seats == 1:
        winner_idx = final_votes.argmax(axis=1)
        wins = np.zeros((sims, n), dtype=bool)
        wins[np.arange(sims), winner_idx] = True
        winner_party = parties[winner_idx]
    else:
        top = np.argsort(-final_votes, axis=1)[:, :seats]
        wins = np.zeros((sims, n), dtype=bool)
        np.put_along_axis(wins, top, True, axis=1)
        winner_party = parties[top[:, 0]]
    win_prob = wins.mean(axis=0)

    totals = votes.sum() + remaining
    out = []
    for i, c in enumerate(cands):
        entry = {
            "label": c.label,
            "party": normalize_party(c.party),
            "party_label": c.party,
            "votes": int(votes[i]),
            "share": float(obs_shares[i]) if obs_shares is not None else None,
            "proj_share": float(final_shares[:, i].mean()),
            "proj_low": float(np.percentile(final_shares[:, i], 10)),
            "proj_high": float(np.percentile(final_shares[:, i], 90)),
            "win_prob": float(win_prob[i]),
        }
        if race.majority_required:
            entry["majority_prob"] = float((final_shares[:, i] > 0.5).mean())
        out.append(entry)

    leader = int(np.argmax(win_prob))
    top_prob = float(win_prob[leader])
    if race.final:
        status = "final_count"
    elif counted == 0:
        status = "prior_only" if race.prior_basis != "none" else "no_results"
    elif top_prob >= params.projected and p * 100 >= params.min_pct_for_projection:
        status = "projected"
    elif top_prob >= params.likely:
        status = "likely"
    elif top_prob >= params.lean:
        status = "lean"
    else:
        status = "tossup"

    return RaceProjection(
        contest_key=race.contest_key,
        status=status,
        votes_counted=int(counted),
        pct_reporting=race.pct_reporting,
        expected_total=(float(totals.mean()), float(np.percentile(totals, 10)), float(np.percentile(totals, 90)))
        if counted > 0 or race.turnout_prior else (0.0, 0.0, 0.0),
        leader_index=leader,
        candidates=out,
        winner_party_draws=winner_party.astype(np.int8),
    )


def prior_only_draws(prior_margin: float, prior_sd: float, params: ModelParams, rng: np.random.Generator,
                     national_shock: np.ndarray) -> np.ndarray:
    """Winner party per draw for a race with no candidate list yet (DEM vs REP baseline only)."""
    margin = prior_margin + params.national_shift + national_shock + rng.normal(0.0, prior_sd, len(national_shock))
    return np.where(margin > 0, PARTY_CODES["DEM"], PARTY_CODES["REP"]).astype(np.int8)


@dataclass
class ChamberProjection:
    chamber: str
    simulations: int
    seats_needed: int
    expected_seats: dict[str, float]
    control_prob: dict[str, float]
    seat_distribution: dict[str, dict[str, float]]
    seats_decided: dict[str, int]
    holdovers: dict[str, int]


def simulate_chamber(chamber: str, winner_party_draws: list[np.ndarray], decided_parties: list[str],
                     holdovers: dict[str, int], seats_needed: int, tie_breaker: str | None = None
                     ) -> ChamberProjection:
    """Aggregate per-race winner draws (all from the same national shocks) into control odds."""
    if not winner_party_draws:
        raise ValueError(f"No races to simulate for {chamber}")
    draws = np.vstack(winner_party_draws)            # (races, sims)
    sims = draws.shape[1]
    seats = {}
    for party, code in PARTY_CODES.items():
        seats[party] = (draws == code).sum(axis=0) + int(holdovers.get(party, 0))
    dem, rep = seats["DEM"], seats["REP"]
    dem_control = dem >= seats_needed
    rep_control = rep >= seats_needed
    if tie_breaker:
        tie = (dem == seats_needed - 1) & (rep == seats_needed - 1)
        if tie_breaker == "DEM":
            dem_control |= tie
        elif tie_breaker == "REP":
            rep_control |= tie
    control = {"DEM": float(dem_control.mean()), "REP": float(rep_control.mean())}
    control["NONE"] = float(max(0.0, 1.0 - control["DEM"] - control["REP"]))

    distribution = {}
    for party in ("DEM", "REP"):
        values, counts = np.unique(seats[party], return_counts=True)
        distribution[party] = {str(int(v)): float(c / sims) for v, c in zip(values, counts)}
    decided = {p: decided_parties.count(p) for p in ("DEM", "REP", "OTHER")}
    return ChamberProjection(
        chamber=chamber,
        simulations=sims,
        seats_needed=seats_needed,
        expected_seats={p: float(v.mean()) for p, v in seats.items()},
        control_prob=control,
        seat_distribution=distribution,
        seats_decided=decided,
        holdovers={k: int(v) for k, v in holdovers.items()},
    )
