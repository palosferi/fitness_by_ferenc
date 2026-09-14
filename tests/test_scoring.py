"""Unit tests for the pure rule-based math in scoring.py.

These are cheap to run and cover the thresholds/formulas that are easy to
silently break while tuning (dead zone, calibration percentile, recommendation
bands) since nothing else in the codebase exercises this module directly.
"""

import math

import pytest

import scoring


# ---- hrr_fraction ----------------------------------------------------

def test_hrr_fraction_basic():
    # halfway between resting (60) and max (180) -> 0.5
    assert scoring.hrr_fraction(120, 60, 180) == pytest.approx(0.5)


def test_hrr_fraction_missing_inputs_returns_zero():
    assert scoring.hrr_fraction(120, None, 180) == 0.0
    assert scoring.hrr_fraction(120, 60, None) == 0.0
    assert scoring.hrr_fraction(120, 180, 180) == 0.0  # max <= resting


# ---- compute_trimp -----------------------------------------------------

def test_compute_trimp_ignores_dead_zone_and_invalid_samples():
    # resting=60, max=160 -> HRR 100bpm; 20% dead zone is 80bpm (HR<=80 excluded)
    pairs = [(80, 10), (0, 10), (None, 10)]
    assert scoring.compute_trimp(pairs, resting_hr=60, max_hr=160) == 0.0


def test_compute_trimp_increases_with_intensity_and_duration():
    low = scoring.compute_trimp([(120, 10)], resting_hr=60, max_hr=180)
    high = scoring.compute_trimp([(160, 10)], resting_hr=60, max_hr=180)
    longer = scoring.compute_trimp([(120, 20)], resting_hr=60, max_hr=180)
    assert high > low > 0
    assert longer > low


# ---- calibration_k_from_history -----------------------------------------

def test_calibration_k_uses_default_with_too_little_history():
    assert scoring.calibration_k_from_history([10, 20], default_k=90.0) == 90.0


def test_calibration_k_uses_85th_percentile_of_history():
    values = list(range(1, 21))  # 20 days, 1..20
    k = scoring.calibration_k_from_history(values)
    # idx = int(20*0.85)-1 = 16 -> values[16] == 17 (sorted, 1-indexed value 17)
    assert k == pytest.approx(max(17 * 0.9, scoring.MIN_TRIMP_K))


def test_calibration_k_never_below_minimum():
    k = scoring.calibration_k_from_history([1, 1, 1, 1, 1])
    assert k >= scoring.MIN_TRIMP_K


# ---- trimp_to_strain -----------------------------------------------------

def test_trimp_to_strain_zero_trimp_is_zero():
    assert scoring.trimp_to_strain(0.0, calibration_k=90.0) == 0.0


def test_trimp_to_strain_increases_and_stays_under_100():
    low = scoring.trimp_to_strain(20, calibration_k=90.0)
    high = scoring.trimp_to_strain(200, calibration_k=90.0)
    assert 0 < low < high < 100


# ---- fallback_sleep_score -------------------------------------------------

def test_fallback_sleep_score_none_without_duration():
    assert scoring.fallback_sleep_score(0, 0, 0, 0) is None
    assert scoring.fallback_sleep_score(None, 0, 0, 0) is None


def test_fallback_sleep_score_full_credit_for_ideal_night():
    # 8h duration, 20% deep, 22% rem, no awakenings -> full marks
    score = scoring.fallback_sleep_score(480, 96, 105.6, 0)
    assert score == pytest.approx(100.0)


def test_fallback_sleep_score_penalizes_awakenings_up_to_a_cap():
    # awake_penalty is capped at 30, so even a huge awake_count can't push
    # an otherwise-perfect night below 100 - 30 = 70.
    score = scoring.fallback_sleep_score(480, 96, 105.6, awake_count=100)
    assert score == pytest.approx(70.0)


def test_fallback_sleep_score_floors_at_zero():
    score = scoring.fallback_sleep_score(duration_min=10, deep_min=0, rem_min=0, awake_count=20)
    assert score == 0.0


# ---- compute_readiness ----------------------------------------------------

def test_compute_readiness_neutral_without_baselines():
    # no HRV/RHR baselines -> falls back to neutral components
    readiness = scoring.compute_readiness(sleep_score=80, hrv_last_night=None, hrv_baseline=None,
                                           resting_hr=None, resting_hr_baseline=None)
    assert 0 <= readiness <= 100


def test_compute_readiness_rewards_hrv_above_baseline():
    low = scoring.compute_readiness(80, hrv_last_night=40, hrv_baseline=50, resting_hr=None, resting_hr_baseline=None)
    high = scoring.compute_readiness(80, hrv_last_night=60, hrv_baseline=50, resting_hr=None, resting_hr_baseline=None)
    assert high > low


def test_compute_readiness_penalizes_elevated_resting_hr():
    normal = scoring.compute_readiness(80, None, None, resting_hr=50, resting_hr_baseline=50)
    elevated = scoring.compute_readiness(80, None, None, resting_hr=58, resting_hr_baseline=50)
    assert elevated < normal


# ---- recommend_training ----------------------------------------------------

@pytest.mark.parametrize("readiness,expected_activity", [
    (20, "rest"),
    (50, "run"),
    (65, "run"),
    (90, "run"),
])
def test_recommend_training_activity_bands(readiness, expected_activity):
    rec = scoring.recommend_training(readiness, yesterday_strain=None, recent_rest_count=0)
    assert rec["activity"] == expected_activity


def test_recommend_training_suggests_gym_after_rest_streak():
    rec = scoring.recommend_training(readiness=45, yesterday_strain=None, recent_rest_count=2)
    assert rec["activity"] == "gym"


def test_recommend_training_pulls_target_down_after_hard_day():
    baseline = scoring.recommend_training(readiness=80, yesterday_strain=None, recent_rest_count=0)
    after_hard_day = scoring.recommend_training(readiness=80, yesterday_strain=90, recent_rest_count=0)
    assert after_hard_day["target_strain"] == baseline["target_strain"] - 15


def test_recommend_training_target_strain_is_clamped():
    rec_low = scoring.recommend_training(readiness=0, yesterday_strain=100, recent_rest_count=0)
    rec_high = scoring.recommend_training(readiness=100, yesterday_strain=None, recent_rest_count=0)
    assert rec_low["target_strain"] >= 5
    assert rec_high["target_strain"] <= 95


# ---- recommend_sleep_hours -------------------------------------------------

def test_recommend_sleep_hours_scales_with_strain():
    rest_day = scoring.recommend_sleep_hours(today_strain=0, recent_sleep_durations_min=[480, 480, 480])
    hard_day = scoring.recommend_sleep_hours(today_strain=100, recent_sleep_durations_min=[480, 480, 480])
    assert hard_day > rest_day
    assert 7.0 <= rest_day <= 9.5
    assert hard_day <= 10.0


def test_recommend_sleep_hours_adds_debt_makeup():
    well_rested = scoring.recommend_sleep_hours(today_strain=50, recent_sleep_durations_min=[480, 480, 480])
    sleep_deprived = scoring.recommend_sleep_hours(today_strain=50, recent_sleep_durations_min=[300, 300, 300])
    assert sleep_deprived > well_rested


def test_recommend_sleep_hours_caps_at_ten():
    assert scoring.recommend_sleep_hours(today_strain=100, recent_sleep_durations_min=[120, 120, 120]) <= 10.0
