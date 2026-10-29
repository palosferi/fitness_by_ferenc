#!/usr/bin/env python3
"""Pull Garmin daily summaries for a past date range into a CSV, one row per day.

    python analysis/garmin_history.py 2025-12-07 2026-02-06 > data/garmin/daily.csv

For offline comparison only: it writes nothing to the dashboard's database.
Old rows there would land in the trailing history that sync.py calibrates
strain and baselines from. Standard library plus the app's own modules, so
it runs from the server's production venv where the Garmin session lives:

    PYTHONPATH=~/fitness_by_ferenc ~/fitness_by_ferenc/venv/bin/python garmin_history.py ...

Garmin keeps daily summaries indefinitely but drops intraday heart rate after
a few months, so hr_trimp - the input to our strain score - is blank for
older days. intensity minutes, active calories and activity training load
are the load measures that survive.
"""

import argparse
import csv
import logging
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from garmin_client import (  # noqa: E402
    extract_garmin_readiness_score,
    extract_hr_series,
    extract_respiration,
    extract_sleep_fields,
    extract_sleep_need,
    get_client,
)
from scoring import compute_trimp  # noqa: E402

log = logging.getLogger("garmin_history")

FIELDS = [
    "date", "resting_hr", "hrv_last_night", "respiration_avg",
    "sleep_duration_min", "sleep_start_local", "sleep_end_local", "sleep_score", "sleep_need_minutes",
    "readiness", "steps", "moderate_min", "vigorous_min", "active_kcal", "stress_avg",
    "body_battery_high", "body_battery_drained", "hr_samples", "hr_trimp",
    "activity_count", "activity_min", "activity_load",
]


def _get(label, fn):
    try:
        return fn()
    except Exception as exc:
        log.warning("%s failed: %s", label, exc)
        return None


def activities_by_day(garmin, start, end):
    """Count, minutes and Garmin training load of recorded activities per local date."""
    days = defaultdict(lambda: {"activity_count": 0, "activity_min": 0.0, "activity_load": 0.0})
    for act in _get("get_activities_by_date",
                    lambda: garmin.get_activities_by_date(start.isoformat(), end.isoformat())) or []:
        day = days[(act.get("startTimeLocal") or "")[:10]]
        day["activity_count"] += 1
        day["activity_min"] += round((act.get("duration") or 0) / 60, 1)
        day["activity_load"] += round(act.get("activityTrainingLoad") or 0, 1)
    return days


def day_row(garmin, day):
    d = day.isoformat()
    stats = _get(f"{d} get_stats", lambda: garmin.get_stats(d)) or {}
    sleep = _get(f"{d} get_sleep_data", lambda: garmin.get_sleep_data(d)) or {}
    hrv = ((_get(f"{d} get_hrv_data", lambda: garmin.get_hrv_data(d)) or {}).get("hrvSummary") or {})
    hr = _get(f"{d} get_heart_rates", lambda: garmin.get_heart_rates(d)) or {}
    readiness = _get(f"{d} get_training_readiness", lambda: garmin.get_training_readiness(d)) or []
    respiration = _get(f"{d} get_respiration_data", lambda: garmin.get_respiration_data(d)) or {}

    sleep_fields = extract_sleep_fields(sleep)
    resting_hr = stats.get("restingHeartRate")
    series = extract_hr_series(hr) if hr.get("heartRateValues") else []
    stress_avg = stats.get("averageStressLevel")  # negative when unmeasured
    return {
        "date": d,
        "resting_hr": resting_hr,
        "hrv_last_night": hrv.get("lastNightAvg"),
        "respiration_avg": extract_respiration(respiration),
        # extract_sleep_fields reports a missing night as 0 minutes.
        "sleep_duration_min": round(sleep_fields["duration_min"], 1) or None,
        "sleep_start_local": sleep_fields["start_local"],
        "sleep_end_local": sleep_fields["end_local"],
        "sleep_score": sleep_fields["garmin_score"],
        "sleep_need_minutes": extract_sleep_need(sleep),
        "readiness": extract_garmin_readiness_score(readiness),
        "steps": stats.get("totalSteps"),
        "moderate_min": stats.get("moderateIntensityMinutes"),
        "vigorous_min": stats.get("vigorousIntensityMinutes"),
        "active_kcal": stats.get("activeKilocalories"),
        "stress_avg": stress_avg if stress_avg is not None and stress_avg >= 0 else None,
        "body_battery_high": stats.get("bodyBatteryHighestValue"),
        "body_battery_drained": stats.get("bodyBatteryDrainedValue"),
        "hr_samples": len(series),
        "hr_trimp": (compute_trimp(series, resting_hr or 55, config.USER_MAX_HR,
                                   exponent=config.USER_TRIMP_EXPONENT) if series else None),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("start", type=date.fromisoformat)
    parser.add_argument("end", type=date.fromisoformat)
    parser.add_argument("--pause", type=float, default=0.5,
                        help="seconds between days, to stay well clear of Garmin's rate limits")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s")

    garmin = get_client()
    activities = activities_by_day(garmin, args.start, args.end)
    writer = csv.DictWriter(sys.stdout, fieldnames=FIELDS)
    writer.writeheader()
    day = args.start
    while day <= args.end:
        row = day_row(garmin, day)
        row.update(activities.get(day.isoformat(), {"activity_count": 0, "activity_min": 0, "activity_load": 0}))
        writer.writerow(row)
        sys.stdout.flush()
        log.info("%s rhr=%s sleep=%s need=%s", row["date"], row["resting_hr"],
                 row["sleep_duration_min"], row["sleep_need_minutes"])
        day += timedelta(days=1)
        time.sleep(args.pause)


if __name__ == "__main__":
    main()
