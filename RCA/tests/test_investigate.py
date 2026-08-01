import math

import pytest

from app.investigate import compute_factor_contributions, compute_holdout_verdict, _log_growth


def test_localized_when_residual_near_zero():
    # matches the Android 15 fill_rate incident: candidate delta -0.35, residual ~0
    assert compute_holdout_verdict(candidate_delta=-0.3517, residual_delta=-0.0006) == "localized"


def test_inconclusive_when_residual_comparable_to_candidate():
    assert compute_holdout_verdict(candidate_delta=-0.10, residual_delta=-0.08) == "inconclusive"


def test_inconclusive_when_candidate_delta_is_zero():
    assert compute_holdout_verdict(candidate_delta=0.0, residual_delta=0.01) == "inconclusive"


def test_ratio_threshold_is_a_boundary():
    assert compute_holdout_verdict(candidate_delta=-1.0, residual_delta=0.25, ratio_threshold=0.25) == "localized"
    assert compute_holdout_verdict(candidate_delta=-1.0, residual_delta=0.2500001, ratio_threshold=0.25) == "inconclusive"


def test_log_growth_is_exact_log_ratio():
    assert _log_growth(0.4338, 0.7849) == math.log(0.4338 / 0.7849)


def test_log_growth_guards_nonpositive_inputs():
    assert _log_growth(0.0, 0.5) == 0.0
    assert _log_growth(0.5, 0.0) == 0.0


def test_single_dominant_factor_is_sole_implicated():
    # fill_rate drives the move; requests/render_rate/ecpm barely move — mirrors the
    # Android 15 incident's decomposition (docs/RCA_DECOMPOSITION_MATH.md §3 "corrected" example)
    growth = {"requests": 0.001, "fill_rate": -0.29, "render_rate": 0.0, "ecpm": 0.03}
    min_effect_rel = {"requests": 0.05, "fill_rate": 0.02, "render_rate": 0.02, "ecpm": 0.03}
    result = compute_factor_contributions(growth, total_delta_rel=-0.046, min_effect_rel=min_effect_rel)

    assert result["offsetting"] is False
    verdicts = {f["metric_id"]: f["verdict"] for f in result["factors"]}
    assert verdicts == {"requests": "cleared", "fill_rate": "implicated",
                          "render_rate": "cleared", "ecpm": "cleared"}
    # contributions must sum to the total revenue delta_rel — the whole point of the log-share split
    assert sum(f["contribution_rel"] for f in result["factors"]) == pytest.approx(-0.046)


def test_two_factors_can_both_be_implicated():
    # two independent real faults in the same window — not one masking the other
    growth = {"requests": 0.001, "fill_rate": -0.20, "render_rate": 0.0, "ecpm": -0.15}
    min_effect_rel = {"requests": 0.05, "fill_rate": 0.02, "render_rate": 0.02, "ecpm": 0.03}
    result = compute_factor_contributions(growth, total_delta_rel=-0.10, min_effect_rel=min_effect_rel)

    verdicts = {f["metric_id"]: f["verdict"] for f in result["factors"]}
    assert verdicts["fill_rate"] == "implicated"
    assert verdicts["ecpm"] == "implicated"


def test_offsetting_factors_skip_the_share_split():
    # fill_rate down, ecpm up, nearly cancel — net revenue move is ~0
    growth = {"requests": 0.0, "fill_rate": -0.003, "render_rate": 0.0005, "ecpm": 0.002}
    min_effect_rel = {"requests": 0.05, "fill_rate": 0.02, "render_rate": 0.02, "ecpm": 0.03}
    result = compute_factor_contributions(growth, total_delta_rel=-0.0005, min_effect_rel=min_effect_rel)

    assert result["offsetting"] is True
    # contribution_rel falls back to each factor's own log_growth, not a share of ~0
    fill_rate = next(f for f in result["factors"] if f["metric_id"] == "fill_rate")
    assert fill_rate["contribution_rel"] == growth["fill_rate"]
