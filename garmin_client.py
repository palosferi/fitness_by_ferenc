"""Thin wrapper around the unofficial `garminconnect` library.

`Garmin.login(tokenstore)` on this library version (0.3.15+) already handles
everything: it tries the cached token file first, and only falls back to an
email/password login (via the constructor) when there's no valid cached
session, dumping fresh tokens back to the same path afterwards. So the only
thing we need to handle ourselves is MFA: pass a `prompt_mfa` callback that
reads a code from stdin when running interactively, and fails fast (instead
of hanging) when run non-interactively (e.g. from the systemd timer).
"""

import logging
import sys
from datetime import timedelta

from garminconnect import Garmin

import config

log = logging.getLogger("garmin_client")


def _prompt_mfa():
    if not sys.stdin.isatty():
        raise RuntimeError(
            "Garmin requires an MFA code but this isn't an interactive session. "
            "Run `python sync.py` manually from a terminal to complete login "
            "once, then the cached session will carry the scheduled runs."
        )
    return input("Enter Garmin MFA code: ").strip()


def get_client():
    garmin = Garmin(
        email=config.GARMIN_EMAIL,
        password=config.GARMIN_PASSWORD,
        prompt_mfa=_prompt_mfa,
    )
    garmin.login(config.GARMIN_TOKENSTORE)
    return garmin


def fetch_daily_snapshot(garmin, day):
    """Fetch each Garmin endpoint independently - one 404/timeout shouldn't
    take down the others. Failures are collected in "errors" (by endpoint
    name) rather than silently treated the same as a legitimately empty
    result, so callers (sync.py) can tell "Garmin has no data for this"
    apart from "we couldn't ask Garmin" and avoid writing a fake zero/None
    reading into history over a transient fetch failure.
    """
    d = day.isoformat()
    errors = []

    def _safe(label, fn):
        try:
            return fn()
        except Exception as exc:  # e.g. Garmin 404s if last night isn't synced yet
            log.warning("%s failed: %s", label, exc)
            errors.append(label)
            return None

    stats = _safe("get_stats", lambda: garmin.get_stats(d)) or {}
    sleep = _safe("get_sleep_data", lambda: garmin.get_sleep_data(d)) or {}
    hrv = _safe("get_hrv_data", lambda: garmin.get_hrv_data(d)) or {}
    hr = _safe("get_heart_rates", lambda: garmin.get_heart_rates(d)) or {}
    readiness = _safe("get_training_readiness", lambda: garmin.get_training_readiness(d)) or []
    body_battery = _safe("get_body_battery", lambda: garmin.get_body_battery(d)) or []
    stress = _safe("get_stress_data", lambda: garmin.get_stress_data(d)) or {}
    respiration = _safe("get_respiration_data", lambda: garmin.get_respiration_data(d)) or {}
    spo2 = _safe("get_spo2_data", lambda: garmin.get_spo2_data(d)) or {}

    # VO2max only updates every few weeks (after a hard enough effort), so a
    # single day's lookup usually comes back empty - pull a wide window and
    # take whatever the most recent entry is.
    vo2_start = (day - timedelta(days=365)).isoformat()
    vo2_range = _safe("get_max_metrics_range", lambda: garmin.get_max_metrics_range(vo2_start, d)) or []

    return {
        "stats": stats,
        "sleep": sleep,
        "hrv": hrv,
        "hr": hr,
        "readiness": readiness,
        "body_battery": body_battery,
        "stress": stress,
        "respiration": respiration,
        "spo2": spo2,
        "vo2max_range": vo2_range,
        "errors": errors,
    }


def extract_respiration(respiration_data):
    """Average breaths/min during sleep - the same measure Whoop's health
    monitor reports, and the one that's stable enough night to night to be
    worth trending (waking respiration moves with whatever you're doing).
    """
    return (respiration_data or {}).get("avgSleepRespirationValue")


def extract_spo2(spo2_data):
    """Blood oxygen during sleep. Garmin also hands back its own 7-day
    average, so we get the baseline for free rather than deriving one.
    """
    data = spo2_data or {}
    return {
        "avg": data.get("avgSleepSpO2") or data.get("averageSpO2"),
        "baseline": data.get("lastSevenDaysAvgSpO2"),
    }


def _latest_readiness_entry(readiness_entries):
    if not readiness_entries:
        return None
    return max(readiness_entries, key=lambda e: e.get("timestamp") or "")


def extract_garmin_readiness_score(readiness_entries):
    """Garmin returns a list of readiness snapshots through the day (it
    updates after wake-up, after activities, etc.) - take the latest one.
    """
    latest = _latest_readiness_entry(readiness_entries)
    return latest.get("score") if latest else None


def extract_acwr(readiness_entries):
    """Acute:chronic training load ratio - how this week's training load
    compares to your trailing 4-week average. Garmin already computes this
    as one of the readiness sub-factors, so we just read it back out rather
    than recomputing it ourselves from raw activity load.
    """
    latest = _latest_readiness_entry(readiness_entries)
    if not latest:
        return {"percent": None, "feedback": None, "acute_load": None}
    return {
        "percent": latest.get("acwrFactorPercent"),
        "feedback": latest.get("acwrFactorFeedback"),
        "acute_load": latest.get("acuteLoad"),
    }


def extract_watch_sync(stats):
    """When the *watch* last uploaded to Garmin - not when we last fetched.

    These differ by hours: the watch pushes to Garmin Connect periodically,
    so our sync can be minutes old while the numbers in it are stale.
    """
    return (stats or {}).get("lastSyncTimestampGMT")


def extract_resting_hr_baseline(stats):
    """Garmin's own 7-day resting HR average - available from day one, unlike
    a baseline derived from however many rows we happen to have stored."""
    return (stats or {}).get("lastSevenDaysAvgRestingHeartRate")


def extract_hrv_balanced_range(hrv_data):
    """Garmin's "balanced" HRV range - the band it considers normal for you.

    This is the right baseline to compare against. hrvSummary.weeklyAvg is a
    rolling 7-day mean, which chases recent values: a week of illness drags
    it down until a genuinely depressed HRV looks "on baseline". The balanced
    range is built from a much longer history and stays put.
    """
    summary = (hrv_data or {}).get("hrvSummary") or {}
    baseline = summary.get("baseline") or {}
    low, high = baseline.get("balancedLow"), baseline.get("balancedUpper")
    if low is None or high is None:
        return {"low": None, "high": None}
    return {"low": low, "high": high}


def extract_sleep_need(sleep):
    """Garmin's own sleep target in minutes, already adjusted for sleep debt
    and HRV - the number the watch shows. Its own formula beats ours."""
    dto = (sleep or {}).get("dailySleepDTO") or {}
    need = dto.get("sleepNeed") or {}
    return need.get("actual") or need.get("baseline")


def extract_stress(stress_data):
    return (stress_data or {}).get("avgStressLevel")


def extract_vo2max(vo2max_range):
    """Take the most recent non-null generic VO2max reading in the range."""
    dated_values = [
        (entry["generic"]["calendarDate"], entry["generic"]["vo2MaxValue"])
        for entry in (vo2max_range or [])
        if entry.get("generic") and entry["generic"].get("vo2MaxValue") is not None
    ]
    if not dated_values:
        return {"value": None, "date": None}
    latest_date, latest_value = max(dated_values, key=lambda pair: pair[0])
    return {"value": latest_value, "date": latest_date}


def extract_body_battery(body_battery_entries):
    """Garmin's own continuously-updating energy-reserve gauge (0-100),
    computed from stress, HRV, sleep and activity - this is what "leftover
    energy for the day" actually looks like when Garmin computes it, rather
    than us approximating it from target minus strain.
    """
    if not body_battery_entries:
        return {"current": None, "charged": None, "drained": None}
    today = body_battery_entries[0]
    values = today.get("bodyBatteryValuesArray") or []
    current = None
    for entry in reversed(values):
        if len(entry) > 1 and entry[1] is not None:
            current = entry[1]
            break
    return {
        "current": current,
        "charged": today.get("charged"),
        "drained": today.get("drained"),
    }


def extract_hr_series(hr_data):
    """Return list of (hr_value, minutes_represented) pairs from intraday data."""
    values = hr_data.get("heartRateValues") or []
    series = []
    for i, entry in enumerate(values):
        if not entry or entry[1] is None:
            continue
        ts_ms, hr = entry
        if i + 1 < len(values) and values[i + 1][1] is not None:
            next_ts_ms = values[i + 1][0]
            minutes = max(0.5, min(10.0, (next_ts_ms - ts_ms) / 60000.0))
        else:
            minutes = 2.0
        series.append((hr, minutes))
    return series


def extract_sleep_fields(sleep):
    dto = (sleep or {}).get("dailySleepDTO") or {}
    duration_sec = dto.get("sleepTimeSeconds") or 0
    deep_sec = dto.get("deepSleepSeconds") or 0
    rem_sec = dto.get("remSleepSeconds") or 0
    awake_count = dto.get("awakeCount") or 0

    score = None
    try:
        score = dto["sleepScores"]["overall"]["value"]
    except (KeyError, TypeError):
        pass

    return {
        "duration_min": duration_sec / 60,
        "deep_min": deep_sec / 60,
        "rem_min": rem_sec / 60,
        "awake_count": awake_count,
        "garmin_score": score,
    }
