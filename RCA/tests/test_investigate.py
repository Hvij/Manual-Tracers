from app.investigate import compute_holdout_verdict


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
