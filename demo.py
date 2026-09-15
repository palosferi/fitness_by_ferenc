"""Synthetic day generator for the public demo view.

Anonymous visitors see this instead of real Garmin data. The numbers are
fake, but they're run through the *real* scoring functions in scoring.py
(synthetic HR series -> TRIMP -> strain, etc.) rather than hardcoded, so
the public dashboard actually demonstrates the algorithms it documents.

Seeded by date, so the demo is stable within a day and shifts overnight -
it looks alive without flickering on every refresh.
"""

import random
from datetime import datetime

from scoring import (
    compute_readiness,
    compute_trimp,
    fallback_sleep_score,
    recommend_sleep_hours,
    recommend_training,
    trimp_to_strain,
)

DEMO_MAX_HR = 188
DEMO_RESTING_HR_BASELINE = 52
DEMO_CALIBRATION_K = 90.0


def _synthetic_hr_series(rng, resting_hr, minutes_of_exercise):
    """A day's worth of (hr, minutes) samples: mostly ambient, with one
    workout block - the same shape extract_hr_series() produces from Garmin.
    """
    series = [(rng.randint(resting_hr, resting_hr + 18), 10.0) for _ in range(80)]
    for _ in range(int(minutes_of_exercise / 5)):
        series.append((rng.randint(135, 168), 5.0))
    return series


def generate_day(day=None):
    """Build a row shaped exactly like a storage.get_day() result."""
    day = day or datetime.now()
    rng = random.Random(day.strftime("%Y-%m-%d"))

    resting_hr = DEMO_RESTING_HR_BASELINE + rng.randint(-3, 4)
    minutes_of_exercise = rng.choice([0, 0, 25, 40, 55, 70])
    hr_series = _synthetic_hr_series(rng, resting_hr, minutes_of_exercise)

    trimp = compute_trimp(hr_series, resting_hr, DEMO_MAX_HR)
    strain_score = trimp_to_strain(trimp, DEMO_CALIBRATION_K)

    sleep_duration_min = rng.randint(380, 510)
    deep_min = sleep_duration_min * rng.uniform(0.13, 0.22)
    rem_min = sleep_duration_min * rng.uniform(0.17, 0.25)
    sleep_score = fallback_sleep_score(sleep_duration_min, deep_min, rem_min, rng.randint(0, 3))

    hrv_baseline = 58.0
    hrv_last_night = round(hrv_baseline * rng.uniform(0.82, 1.18), 1)
    readiness = compute_readiness(
        sleep_score, hrv_last_night, hrv_baseline, resting_hr, DEMO_RESTING_HR_BASELINE
    )

    yesterday_strain = rng.choice([None, 30.0, 55.0, 80.0])
    rec = recommend_training(readiness, yesterday_strain, rng.randint(0, 2))
    sleep_hours_rec = recommend_sleep_hours(strain_score, [sleep_duration_min] * 3)

    drained = rng.randint(30, 70)
    charged = rng.randint(45, 85)

    return {
        "date": day.date().isoformat(),
        "resting_hr": resting_hr,
        "resting_hr_baseline": DEMO_RESTING_HR_BASELINE,
        "max_hr": DEMO_MAX_HR,
        "hrv_last_night": hrv_last_night,
        "hrv_baseline": hrv_baseline,
        "sleep_score": sleep_score,
        "sleep_duration_min": sleep_duration_min,
        "steps": rng.randint(4200, 14500),
        "trimp": trimp,
        "strain_score": strain_score,
        "readiness_score": readiness,
        "readiness_source": "demo",
        "target_strain": rec["target_strain"],
        "sleep_recommendation_hours": sleep_hours_rec,
        "body_battery": max(5, min(100, 100 - drained + rng.randint(0, 15))),
        "body_battery_charged": charged,
        "body_battery_drained": drained,
        "stress_avg": rng.randint(18, 46),
        "acwr_percent": rng.randint(70, 130),
        "acwr_feedback": rng.choice(["GOOD", "VERY_GOOD", "MAINTAINING"]),
        "acute_load": rng.randint(180, 520),
        "vo2max": rng.randint(46, 54),
        "vo2max_date": day.date().isoformat(),
        "recommendation_type": rec["activity"],
        "recommendation_detail": rec["detail"],
        "sync_errors": None,
        "updated_at": day.isoformat(timespec="seconds"),
    }
