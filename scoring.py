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
        trimp += minutes * _trimp_rate(delta_ratio, exponent)
    return round(trimp, 1)


def _trimp_rate(delta_ratio, exponent):
    """TRIMP accrued per minute at a given fraction of heart-rate reserve."""
    return delta_ratio * 0.64 * math.exp(exponent * delta_ratio)


# Session RPE (Borg CR10, 1-10) to the heart-rate-reserve fraction it
# corresponds to, following ACSM's intensity table: light ~30-39% HRR,
# moderate 40-59%, vigorous 60-89%, near-maximal 90%+. Used for sessions
# the watch never saw (basketball, swimming without it), so they land on
# the same TRIMP scale - and the same calibration - as recorded heart rate.
RPE_TO_HRR = {1: 0.25, 2: 0.32, 3: 0.40, 4: 0.47, 5: 0.54,
              6: 0.62, 7: 0.70, 8: 0.79, 9: 0.88, 10: 0.95}


def manual_trimp(minutes, rpe, exponent=TRIMP_EXPONENT):
    """TRIMP for a logged session: its duration held at the HRR its
    perceived effort implies. Team sports are intermittent, but session RPE
    is rated for the session as a whole, so a flat equivalent is the honest
    reading of it."""
    fraction = RPE_TO_HRR.get(rpe)
    if not fraction or not minutes or minutes <= 0:
        return 0.0
    return round(minutes * _trimp_rate(fraction, exponent), 1)



def logged_sessions_trimp(sessions, exponent=TRIMP_EXPONENT):
    """Total TRIMP of logged sessions (dicts with "minutes" and "rpe")."""
    return round(sum(manual_trimp(s["minutes"], s["rpe"], exponent) for s in sessions), 1)


def day_strain(hr_trimp, logged_trimp, trailing_trimps):
    """(total TRIMP, strain) for a day: watch-recorded plus logged sessions.

    Unknown HR (a failed fetch) keeps the whole day unknown rather than
    letting a logged session pass for the day's total.
    """
    if hr_trimp is None:
        return None, None
    total = round(hr_trimp + (logged_trimp or 0.0), 1)
    return total, trimp_to_strain(total, calibration_k_from_history(trailing_trimps))


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


# Each effort level's name and what it asks of you. The scale is the same
# 1-10 perceived effort used to log a session without the watch, so "effort
# 6 today" and "logged at effort 6" mean the same thing. It says how hard,
# not what - the sport is yours to pick.
EFFORT_GUIDANCE = {
    0: ("rest", "No exercise today. A walk is fine."),
    1: ("very easy", "Easy movement only - a walk, mobility, stretching."),
    2: ("easy", "Easy movement only - a walk, mobility, stretching."),
    3: ("light", "Keep it easy enough to hold a conversation throughout."),
    4: ("light", "Keep it easy enough to hold a conversation throughout."),
    5: ("moderate", "A solid session, but stop short of pushing hard."),
    6: ("moderate", "A solid session, but stop short of pushing hard."),
    7: ("hard", "A demanding session is fine - push, but leave a little in the tank."),
    8: ("hard", "A demanding session is fine - push, but leave a little in the tank."),
    9: ("very hard", "Well recovered - go hard."),
    10: ("all out", "Fully recovered - go as hard as you like."),
}

# Target strain at or below REST_TARGET is a rest day (level 0); the scale
# then climbs linearly to 10 at the clamp ceiling.
REST_TARGET = 30
MAX_TARGET = 95


def effort_prefix(effort):
    """The "Effort 6/10, moderate. " lead-in of a stored recommendation,
    which the dashboard strips because it shows the level on its own."""
    return f"Effort {effort}/10, {EFFORT_GUIDANCE[effort][0]}. "


def recommend_training(readiness, yesterday_strain):
    """How hard to go today, 0 (no exercise) to 10 (all out).

    Target strain starts at readiness and drops 15 after a very hard day, so
    one big session isn't immediately followed by another.
    """
    target_strain = readiness
    eased = yesterday_strain is not None and yesterday_strain > 75
    if eased:
        target_strain -= 15
    target_strain = max(5, min(MAX_TARGET, target_strain))

    fraction = (target_strain - REST_TARGET) / (MAX_TARGET - REST_TARGET)
    effort = max(0, min(10, math.floor(fraction * 10 + 0.5)))
    advice = EFFORT_GUIDANCE[effort][1]
    if eased:
        advice += " Eased off after yesterday's hard day."

    return {
        "activity": "rest" if effort == 0 else "train",
        "effort": effort,
        "target_strain": round(target_strain, 1),
        # Self-contained, for the widget and /api/today.
        "detail": f"{effort_prefix(effort)}{advice}",
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
