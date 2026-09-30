#!/usr/bin/env python3
"""Entry point run on a schedule (systemd timer, cron, ...).

Pulls today's data from Garmin Connect, computes strain/sleep/readiness
scores and a training recommendation with plain rule-based formulas
(see scoring.py), and upserts today's row in SQLite. Safe to run
repeatedly through the day - strain climbs as new HR data comes in,
sleep score firms up once Garmin's finished processing last night.
"""

import json
import logging
from datetime import date, datetime, timedelta

import config
import storage
from garmin_client import (
    GarminNotLinked,
    extract_acwr,
    extract_body_battery,
    extract_garmin_readiness_score,
    extract_hr_series,
    extract_hrv_balanced_range,
    extract_off_wrist_gaps,
    extract_respiration,
    extract_resting_hr_baseline,
    extract_sleep_need,
    extract_sleep_fields,
    extract_spo2,
    extract_stress,
    extract_vo2max,
    extract_watch_sync,
    fetch_daily_snapshot,
    get_client,
)
from scoring import (
    compute_readiness,
    compute_trimp,
    day_strain,
    fallback_sleep_score,
    logged_sessions_trimp,
    recommend_sleep_hours,
    recommend_training,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sync")


def _derived_baseline(rows, field):
    """Mean of a stored column, or None until config.MIN_BASELINE_SAMPLES rows
    have it. Same rule the dashboard applies to its own derived baselines: too
    few nights and the number is noise wearing a baseline's clothes.
    """
    values = [r[field] for r in rows if r.get(field)]
    if len(values) < config.MIN_BASELINE_SAMPLES:
        return None
    return sum(values) / len(values)


def _record_link_state(state):
    """Remember whether the stored Garmin session still works, so the
    dashboard can say "sign in again" instead of silently freezing on the
    last day that synced."""
    conn = storage.get_conn(config.DB_PATH)
    if storage.get_setting(conn, "garmin_link_state") != state:
        storage.set_setting(conn, "garmin_link_state", state)


def run():
    today = date.today()
    today_str = today.isoformat()

    try:
        garmin = get_client()
    except GarminNotLinked as exc:
        # Nothing to retry against - an instance set up through the web page
        # has no stored password to fall back on. Exit non-zero so the timer
        # shows as failed rather than quietly doing nothing twice an hour.
        _record_link_state("needs_login")
        log.error("%s Reconnect at /setup.", exc)
        raise SystemExit(1)

    _record_link_state("linked")
    snapshot = fetch_daily_snapshot(garmin, today)
    fetch_errors = snapshot["errors"]

    stats = snapshot["stats"]
    resting_hr = stats.get("restingHeartRate")
    steps = stats.get("totalSteps")

    hrv_summary = (snapshot["hrv"] or {}).get("hrvSummary") or {}
    hrv_last_night = hrv_summary.get("lastNightAvg")
    hrv_weekly = hrv_summary.get("weeklyAvg")

    sleep_fields = extract_sleep_fields(snapshot["sleep"])
    sleep_score = sleep_fields["garmin_score"]
    if sleep_score is None:
        sleep_score = fallback_sleep_score(
            sleep_fields["duration_min"],
            sleep_fields["deep_min"],
            sleep_fields["rem_min"],
            sleep_fields["awake_count"],
        )

    hr_series = extract_hr_series(snapshot["hr"])
    max_hr = config.USER_MAX_HR
    hr_fetch_failed = "get_heart_rates" in fetch_errors
    if hr_fetch_failed:
        # We couldn't ask Garmin for today's HR data at all - leave trimp/
        # strain as unknown (None) rather than writing a false "zero effort"
        # day into history, which would corrupt both the calibration curve
        # and tomorrow's "yesterday_strain > 75" check.
        hr_trimp = None
    else:
        hr_trimp = compute_trimp(hr_series, resting_hr or 55, max_hr, exponent=config.USER_TRIMP_EXPONENT) if hr_series else 0.0

    conn = storage.get_conn(config.DB_PATH)
    history = storage.get_recent_days(conn, today_str, limit=60)

    # Sessions logged by hand for time the watch wasn't worn - no HR to
    # double-count, so they simply add to what the watch recorded.
    trailing_trimps = [h["trimp"] for h in history if h.get("trimp") is not None]
    logged_trimp = logged_sessions_trimp(
        storage.get_manual_activities(conn, [today_str]), config.USER_TRIMP_EXPONENT)
    trimp, strain_score = day_strain(hr_trimp, logged_trimp, trailing_trimps)

    hrv_baseline = hrv_weekly
    if not hrv_baseline:
        hrv_baseline = _derived_baseline(history[:7], "hrv_last_night")

    resting_hr_baseline = _derived_baseline(history[:7], "resting_hr")

    garmin_readiness = extract_garmin_readiness_score(snapshot["readiness"])
    if garmin_readiness is not None:
        readiness = garmin_readiness
        readiness_source = "garmin"
    else:
        readiness = compute_readiness(sleep_score, hrv_last_night, hrv_baseline, resting_hr, resting_hr_baseline)
        readiness_source = "fallback"

    yesterday = history[0] if history else None
    yesterday_strain = yesterday["strain_score"] if yesterday else None

    rec = recommend_training(readiness, yesterday_strain)

    hrv_range = extract_hrv_balanced_range(snapshot["hrv"])
    resting_hr_baseline_garmin = extract_resting_hr_baseline(stats)
    watch_synced_at = extract_watch_sync(stats)
    sleep_need_minutes = extract_sleep_need(snapshot["sleep"])

    if sleep_need_minutes:
        sleep_hours_rec = round(sleep_need_minutes / 60, 1)
    else:
        recent_sleep_durations = [h["sleep_duration_min"] for h in history[:3] if h.get("sleep_duration_min")]
        sleep_hours_rec = recommend_sleep_hours(strain_score, recent_sleep_durations)

    body_battery = extract_body_battery(snapshot["body_battery"])
    stress = extract_stress(snapshot["stress"])
    stress_avg = stress["avg"]
    acwr = extract_acwr(snapshot["readiness"])
    vo2max = extract_vo2max(snapshot["vo2max_range"])
    respiration_avg = extract_respiration(snapshot["respiration"])
    spo2 = extract_spo2(snapshot["spo2"])

    fields = {
        "resting_hr": resting_hr,
        "max_hr": max_hr,
        "hrv_last_night": hrv_last_night,
        "hrv_baseline": hrv_baseline,
        "sleep_score": sleep_score,
        "sleep_duration_min": sleep_fields["duration_min"],
        "steps": steps,
        "trimp": trimp,
        "hr_trimp": hr_trimp,
        "manual_trimp": logged_trimp,
        "strain_score": strain_score,
        "readiness_score": readiness,
        "readiness_source": readiness_source,
        "target_strain": rec["target_strain"],
        "sleep_recommendation_hours": sleep_hours_rec,
        "body_battery": body_battery["current"],
        "body_battery_charged": body_battery["charged"],
        "body_battery_drained": body_battery["drained"],
        "stress_avg": stress_avg,
        "stress_latest": stress["latest"],
        "stress_latest_at": stress["latest_at"],
        "stress_max": stress["max"],
        "acwr_percent": acwr["percent"],
        "acwr_feedback": acwr["feedback"],
        "acute_load": acwr["acute_load"],
        "vo2max": vo2max["value"],
        "vo2max_date": vo2max["date"],
        "respiration_avg": respiration_avg,
        "resting_hr_baseline": resting_hr_baseline_garmin,
        "hrv_balanced_low": hrv_range["low"],
        "hrv_balanced_high": hrv_range["high"],
        "sleep_need_minutes": sleep_need_minutes,
        "watch_synced_at": watch_synced_at,
        "spo2_avg": spo2["avg"],
        "spo2_baseline": spo2["baseline"],
        "recommendation_type": rec["activity"],
        "recommendation_detail": rec["detail"],
        "recommended_effort": rec["effort"],
        "sync_errors": ",".join(fetch_errors) if fetch_errors else None,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    # A failed HR fetch says nothing about the watch being off - keep
    # whatever gaps the last good fetch found.
    if not hr_fetch_failed:
        fields["off_wrist_gaps"] = json.dumps(
            extract_off_wrist_gaps(snapshot["hr"], config.OFF_WRIST_REMINDER_MINUTES))
    storage.upsert_day(conn, today_str, fields)
    refresh_yesterday_gaps(garmin, conn, today - timedelta(days=1))

    log.info(
        "Synced %s: strain=%s (target %s) sleep=%s readiness=%s (%s) battery=%s stress=%s acwr=%s%% (%s) "
        "vo2max=%s steps=%s effort=%s sleep_target=%sh%s",
        today_str, strain_score, rec["target_strain"], sleep_score, readiness, readiness_source,
        body_battery["current"], stress_avg, acwr["percent"], acwr["feedback"], vo2max["value"],
        steps, rec["effort"], sleep_hours_rec,
        f" [partial sync, failed: {', '.join(fetch_errors)}]" if fetch_errors else "",
    )


def refresh_yesterday_gaps(garmin, conn, yesterday):
    """An evening session without the watch often only reaches Garmin with
    the next morning's upload, after yesterday's last sync - so re-read
    yesterday's HR for its gaps. Strain for the day is left as it was."""
    try:
        hr = garmin.get_heart_rates(yesterday.isoformat()) or {}
    except Exception as exc:
        log.warning("yesterday's get_heart_rates failed: %s", exc)
        return
    gaps = extract_off_wrist_gaps(hr, config.OFF_WRIST_REMINDER_MINUTES)
    storage.upsert_day(conn, yesterday.isoformat(), {"off_wrist_gaps": json.dumps(gaps)})


if __name__ == "__main__":
    run()
