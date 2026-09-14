"""Rule-based scoring - no LLM involved.

Strain: Banister TRIMP - a continuous exponential weighting of heart-rate-
reserve elevation over time above a small dead zone (~20% HRR), so a walk,
stress, or a restless night still contributes some non-zero strain the same
way Whoop's day strain isn't zero just because you haven't "worked out" yet
- without every one of the hundreds of minutes spent barely above resting
(sitting, digesting, ordinary fidgeting) silently adding up to more than an
actual workout would. Formal exercise still dominates the total because the
weighting is exponential in how far above the dead zone you are. The result
is then log-compressed to 1-100 against a personal calibration constant
(your own trailing TRIMP history) so effort has diminishing returns and the
scale stays personal.

Sleep: pass through Garmin's own 0-100 sleep score when available (it
already factors duration/stages/restlessness); fall back to a simple
duration+stages formula only when Garmin hasn't computed one yet.

Readiness: prefer Garmin's own Training Readiness score (it already blends
HRV, sleep, recovery time, acute training load and stress history - far
more inputs than we can easily replicate). We only fall back to a simple
homegrown sleep/HRV/resting-HR blend on days Garmin's isn't available.
"""

import math

DEFAULT_TRIMP_K = 90.0
MIN_TRIMP_K = 40.0

# Banister TRIMP exponential constant - 1.92 is the standard "male" value,
# 1.67 is typically used for women. Doesn't need to be exact; it just shapes
# how much extra weight harder efforts get relative to easier ones.
TRIMP_EXPONENT = 1.92

# Below this fraction of heart-rate-reserve, no strain credit at all. Without
# a dead zone, a whole day's worth of minutes sitting just barely above
# resting (posture, digestion, ambient temperature) adds up to more total
# strain than an actual workout, purely because there are hundreds of them.
# 20% HRR is comfortably above "sitting slightly elevated" but still well
# below a brisk walk, so ordinary daytime movement and stress spikes still
# register - they just don't drown out real exertion.
TRIMP_DEAD_ZONE = 0.20


def hrr_fraction(hr, resting_hr, max_hr):
    """Fraction of heart-rate-reserve (Karvonen method)."""
    if max_hr is None or resting_hr is None or max_hr <= resting_hr:
        return 0.0
    return (hr - resting_hr) / (max_hr - resting_hr)


def compute_trimp(hr_minute_pairs, resting_hr, max_hr, exponent=TRIMP_EXPONENT):
    """Banister TRIMP = duration * delta_ratio * 0.64 * e^(exponent * delta_ratio),
    summed per HR sample above the dead zone. Continuous above that floor -
    no zone steps - so anything from a brisk walk to a hard interval scales
    smoothly, it just doesn't start counting at the very first heartbeat
    above resting.
    """
    trimp = 0.0
    for hr, minutes in hr_minute_pairs:
        if not hr or hr <= 0:
            continue
        delta_ratio = hrr_fraction(hr, resting_hr, max_hr)
        if delta_ratio <= TRIMP_DEAD_ZONE:
            continue
        weight = 0.64 * math.exp(exponent * delta_ratio)
        trimp += minutes * delta_ratio * weight
    return round(trimp, 1)


def calibration_k_from_history(trailing_trimps, default_k=DEFAULT_TRIMP_K):
    """Use roughly your 85th-percentile day as 'a hard day' denominator."""
    values = sorted(t for t in trailing_trimps if t and t > 0)
    if len(values) < 5:
        return default_k
    idx = max(0, int(len(values) * 0.85) - 1)
    typical_hard = values[idx]
    return max(typical_hard * 0.9, MIN_TRIMP_K)


def trimp_to_strain(trimp, calibration_k):
    k = max(calibration_k, MIN_TRIMP_K)
    return round(100 * (1 - math.exp(-trimp / k)), 1)


def fallback_sleep_score(duration_min, deep_min, rem_min, awake_count):
    if not duration_min:
        return None
    duration_score = min(100, (duration_min / 480) * 100)  # target 8h
    deep_pct = (deep_min / duration_min) if duration_min else 0
    deep_score = min(100, (deep_pct / 0.20) * 100)  # target 20% deep
    rem_pct = (rem_min / duration_min) if duration_min else 0
    rem_score = min(100, (rem_pct / 0.22) * 100)  # target 22% rem
    awake_penalty = min(30, awake_count * 5)
    score = 0.5 * duration_score + 0.25 * deep_score + 0.25 * rem_score - awake_penalty
    return round(max(0, min(100, score)), 1)


def compute_readiness(sleep_score, hrv_last_night, hrv_baseline, resting_hr, resting_hr_baseline):
    """Fallback only - used when Garmin's own Training Readiness score isn't
    available for the day. Garmin's version (fetched in sync.py via
    get_training_readiness) is preferred whenever present, since it factors
    in acute training load, recovery time and sleep/stress history that this
    simple blend doesn't have access to.
    """
    sleep_component = sleep_score if sleep_score is not None else 60

    if hrv_baseline and hrv_baseline > 0 and hrv_last_night:
        ratio = hrv_last_night / hrv_baseline
        hrv_component = max(0, min(100, 50 + (ratio - 1.0) * 250))
    else:
        hrv_component = 60

    if resting_hr_baseline and resting_hr:
        delta = resting_hr - resting_hr_baseline
        rhr_component = max(0, min(100, 70 - delta * 8))
    else:
        rhr_component = 70

    readiness = 0.5 * sleep_component + 0.35 * hrv_component + 0.15 * rhr_component
    return round(readiness, 1)


def recommend_training(readiness, yesterday_strain, recent_rest_count, easy_pace_min_per_km=6.0):
    target_strain = readiness
    if yesterday_strain is not None and yesterday_strain > 75:
        target_strain -= 15
    target_strain = max(5, min(95, target_strain))

    if target_strain <= 30:
        activity = "rest"
        detail = "Recovery day: light walk or full rest. No structured training."
    elif target_strain <= 55:
        if recent_rest_count >= 2:
            activity = "gym"
            detail = "Light gym session: mobility + light strength (~30-40 min), keep effort easy."
        else:
            activity = "run"
            duration_min = 30
            distance_km = round(duration_min / easy_pace_min_per_km, 1)
            detail = f"Easy run: {distance_km} km / {duration_min} min at an easy, conversational pace."
    elif target_strain <= 75:
        activity = "run"
        duration_min = 45
        distance_km = round(duration_min / easy_pace_min_per_km, 1)
        detail = f"Moderate run: {distance_km} km / {duration_min} min, comfortably hard."
    else:
        activity = "run"
        duration_min = 60
        distance_km = round(duration_min / (easy_pace_min_per_km * 0.9), 1)
        detail = f"Hard session: {distance_km} km / {duration_min} min with intervals, or a heavy gym session."

    return {
        "activity": activity,
        "target_strain": round(target_strain, 1),
        "detail": detail,
    }


def recommend_sleep_hours(today_strain, recent_sleep_durations_min):
    """How much sleep to aim for tonight - scales with how much strain you've
    put on your body today, plus a small nudge if you've been running a
    sleep deficit the last few days. Meant to be recomputed through the day
    as strain climbs, not just once at bedtime.
    """
    base = 7.0 + (max(0, min(100, today_strain or 0)) / 100) * 2.5  # 7.0h - 9.5h

    if recent_sleep_durations_min:
        avg_hours = (sum(recent_sleep_durations_min) / len(recent_sleep_durations_min)) / 60
        debt = max(0, 8.0 - avg_hours)
        base += min(1.0, debt * 0.3)

    return round(min(10.0, base), 1)
