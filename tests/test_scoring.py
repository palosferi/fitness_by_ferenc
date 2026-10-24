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

@pytest.mark.parametrize("readiness,expected_effort", [
    (20, 0),
    (30, 0),
    (58, 4),
    (79, 8),
    (100, 10),
])
def test_recommend_training_effort_levels(readiness, expected_effort):
    rec = scoring.recommend_training(readiness, yesterday_strain=None)
    assert rec["effort"] == expected_effort
    assert rec["activity"] == ("rest" if expected_effort == 0 else "train")


def test_recommend_training_effort_never_decreases_with_readiness():
    efforts = [scoring.recommend_training(r, yesterday_strain=None)["effort"] for r in range(0, 101)]
    assert efforts == sorted(efforts)
    assert set(efforts) == set(range(11))


def test_recommend_training_detail_names_the_level():
    rec = scoring.recommend_training(readiness=58, yesterday_strain=None)
    assert rec["detail"].startswith("Effort 4/10, light.")
    assert "run" not in rec["detail"].lower()


def test_recommend_training_says_when_it_eased_off():
    rec = scoring.recommend_training(readiness=80, yesterday_strain=90)
    assert "yesterday's hard day" in rec["detail"]


def test_recommend_training_pulls_target_down_after_hard_day():
    baseline = scoring.recommend_training(readiness=80, yesterday_strain=None)
    after_hard_day = scoring.recommend_training(readiness=80, yesterday_strain=90)
    assert after_hard_day["target_strain"] == baseline["target_strain"] - 15


def test_recommend_training_target_strain_is_clamped():
    rec_low = scoring.recommend_training(readiness=0, yesterday_strain=100)
    rec_high = scoring.recommend_training(readiness=100, yesterday_strain=None)
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


# ---- recommend_sleep_window ------------------------------------------------

def night(start, end, asleep):
    return {"start_local": f"2026-10-01T{start}", "end_local": f"2026-10-02T{end}", "duration_min": asleep}


def test_sleep_window_wakes_at_the_usual_time_not_the_lie_in():
    nights = [night("23:00", "07:00", 450), night("23:10", "07:10", 450),
              night("22:50", "06:50", 450), night("00:30", "10:30", 570),
              night("01:00", "11:00", 570)]
    bedtime, wake = scoring.recommend_sleep_window(480, nights)
    assert wake == "07:10"  # two weekend lie-ins out of five don't move it


def test_sleep_window_bedtime_leaves_room_for_lying_awake():
    # 8h in bed, 7h30 asleep each night: 30 min awake on top of the need.
    nights = [night("23:00", "07:00", 450)] * 3
    assert scoring.recommend_sleep_window(480, nights) == ("22:30", "07:00")


def test_sleep_window_lights_out_allows_for_falling_asleep():
    nights = [night("23:00", "07:00", 450)] * 3
    assert scoring.recommend_sleep_window(480, nights, latency_min=25) == ("22:05", "07:00")


def test_sleep_window_bedtime_wraps_past_midnight():
    nights = [night("03:00", "11:00", 480)] * 3
    assert scoring.recommend_sleep_window(600, nights) == ("01:00", "11:00")


def test_sleep_window_nudges_toward_the_target_a_step_at_a_time():
    nights = [night("01:00", "10:20", 560)] * 3
    assert scoring.recommend_sleep_window(540, nights, target_wake="07:30")[1] == "10:05"
    assert scoring.recommend_sleep_window(540, nights, target_wake="07:30", max_shift_min=30)[1] == "09:50"


def test_sleep_window_steps_on_from_a_morning_that_followed_it():
    # Median still 10:20, but last night got up at 10:05 as told - the next
    # step starts from there rather than waiting four nights for the median.
    nights = [night("00:45", "10:05", 560)] + [night("01:00", "10:20", 560)] * 6
    assert scoring.recommend_sleep_window(540, nights, target_wake="07:30")[1] == "09:50"


def test_sleep_window_ignores_a_lie_in_and_an_overshoot():
    week = [night("00:00", "08:30", 500)] * 6
    lie_in = [night("02:00", "11:00", 520)] + week
    assert scoring.recommend_sleep_window(480, lie_in, target_wake="07:30")[1] == "08:15"
    early_flight = [night("23:00", "04:30", 320)] + week
    assert scoring.recommend_sleep_window(480, early_flight, target_wake="07:30")[1] == "08:15"


def test_sleep_window_lands_on_a_target_within_one_step():
    nights = [night("23:30", "07:40", 480)] * 3
    assert scoring.recommend_sleep_window(480, nights, target_wake="07:30")[1] == "07:30"


def test_sleep_window_nudges_later_too_and_the_short_way_round():
    early = [night("21:00", "05:00", 480)] * 3
    assert scoring.recommend_sleep_window(480, early, target_wake="07:30")[1] == "05:15"
    late = [night("15:00", "23:50", 480)] * 3
    assert scoring.recommend_sleep_window(480, late, target_wake="00:10")[1] == "00:05"


def test_sleep_window_keeps_the_wake_time_inside_its_bounds():
    bounds = ("06:00", "10:00")
    late = [night("01:00", "10:20", 540)] * 3
    assert scoring.recommend_sleep_window(540, late, wake_bounds=bounds) == ("00:40", "10:00")
    early = [night("21:00", "05:00", 480)] * 3
    assert scoring.recommend_sleep_window(480, early, wake_bounds=bounds)[1] == "06:00"
    # Out-of-window goes to the nearer edge round the clock, not the numerically lower one.
    night_shift = [night("15:00", "23:00", 480)] * 3
    assert scoring.recommend_sleep_window(480, night_shift, wake_bounds=bounds)[1] == "06:00"
    inside = [night("23:00", "07:00", 480)] * 3
    assert scoring.recommend_sleep_window(480, inside, wake_bounds=bounds)[1] == "07:00"


def test_sleep_window_wide_shift_makes_the_target_fixed():
    nights = [night("23:00", "08:00", 540)] * 3
    assert scoring.recommend_sleep_window(450, nights, target_wake="06:30", max_shift_min=720) == ("23:00", "06:30")


def test_sleep_window_needs_something_to_anchor_on():
    assert scoring.recommend_sleep_window(480, []) is None
    assert scoring.recommend_sleep_window(480, [{"start_local": None, "end_local": None, "duration_min": 0}]) is None
    assert scoring.recommend_sleep_window(None, [night("23:00", "07:00", 450)]) is None
    assert scoring.recommend_sleep_window(480, [], target_wake="07:00") == ("23:00", "07:00")
