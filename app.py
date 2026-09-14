from datetime import date, datetime, timedelta

from flask import Flask, jsonify, render_template

import config
import storage

app = Flask(__name__)


def ring_color(value, thresholds=(config.RING_LOW_THRESHOLD, config.RING_HIGH_THRESHOLD)):
    if value is None:
        return "#888888"
    if value < thresholds[0]:
        return "#e5484d"
    if value < thresholds[1]:
        return "#f5a623"
    return "#30a46c"


def is_stale(updated_at, stale_minutes=config.SYNC_STALE_MINUTES):
    if not updated_at:
        return True
    try:
        updated = datetime.fromisoformat(updated_at)
    except ValueError:
        return True
    return datetime.now() - updated > timedelta(minutes=stale_minutes)


def format_synced_at(updated_at):
    if not updated_at:
        return None
    try:
        updated = datetime.fromisoformat(updated_at)
    except ValueError:
        return None
    return updated.strftime("%b %d, %H:%M")


def partial_sync_text(sync_errors):
    if not sync_errors:
        return None
    endpoints = sync_errors.replace("_", " ").replace(",", ", ")
    return f"Partial sync - couldn't reach Garmin for: {endpoints}"


def pct_for(kind, value):
    if value is None:
        return 0
    return max(0, min(100, value))


def acwr_warning_text(percent, feedback, acute_load):
    """Only surface a badge when training load looks genuinely elevated -
    Garmin's own feedback labels (e.g. VERY_GOOD/GOOD) or a comfortable
    percent mean no badge at all, keeping this out of the way most days.
    """
    if percent is None:
        return None
    risky_feedback = feedback and any(word in feedback.upper() for word in ("POOR", "HIGH", "RISK"))
    if percent < config.ACWR_RISK_THRESHOLD_PERCENT or risky_feedback:
        feedback_text = feedback.replace("_", " ").title() if feedback else "elevated"
        return f"Training load risk: {feedback_text} (ACWR {round(percent)}%, acute load {acute_load})"
    return None


@app.route("/api/today")
def api_today():
    conn = storage.get_conn(config.DB_PATH)
    today = storage.get_day(conn, date.today().isoformat())
    if not today:
        return jsonify({"error": "no data yet"}), 404
    today["stale"] = is_stale(today.get("updated_at"))
    return jsonify(today)


@app.route("/")
def dashboard():
    conn = storage.get_conn(config.DB_PATH)
    today = storage.get_day(conn, date.today().isoformat()) or {}

    steps = today.get("steps")
    hrv_baseline = round(today["hrv_baseline"], 1) if today.get("hrv_baseline") else None
    sleep_hours = today.get("sleep_recommendation_hours")
    battery = today.get("body_battery")
    charged = today.get("body_battery_charged")
    drained = today.get("body_battery_drained")

    # Readiness (not Body Battery) gets the ring: it's the one Garmin-computed
    # number that actually drives the training recommendation below. Body
    # Battery is a live reserve gauge that answers a different question ("how
    # much do I have left right now") - useful, but as a supporting stat, not
    # a second big number competing with Readiness for attention.
    rings = [
        {
            "label": "Strain",
            "display": today.get("strain_score"),
            "pct": pct_for("score", today.get("strain_score")),
            "color": ring_color(today.get("strain_score")),
        },
        {
            "label": "Target",
            "display": today.get("target_strain"),
            "pct": pct_for("score", today.get("target_strain")),
            "color": "#5b8def",
        },
        {
            "label": "Readiness",
            "display": today.get("readiness_score"),
            "pct": pct_for("score", today.get("readiness_score")),
            "color": ring_color(today.get("readiness_score")),
        },
        {
            "label": "Sleep",
            "display": today.get("sleep_score"),
            "pct": pct_for("score", today.get("sleep_score")),
            "color": ring_color(today.get("sleep_score")),
        },
    ]

    vo2max = today.get("vo2max")
    vo2max_date = today.get("vo2max_date")

    battery_value = None
    if battery is not None:
        battery_value = f"{round(battery)}"
        if charged is not None:
            battery_value += f" (+{round(charged)} / -{round(drained)} today)"
    elif charged is not None:
        battery_value = f"+{round(charged)} / -{round(drained)} today"

    stats = [
        {"label": "Steps", "value": f"{steps:,}" if steps else None},
        {"label": "Sleep target tonight", "value": f"{sleep_hours}h" if sleep_hours else None},
        {"label": "HRV", "value": f"{today['hrv_last_night']} ms (baseline {hrv_baseline})" if today.get("hrv_last_night") else None},
        {"label": "Resting HR", "value": f"{today['resting_hr']} bpm" if today.get("resting_hr") else None},
        {"label": "Body Battery", "value": battery_value},
        {"label": "Stress (avg today)", "value": round(today["stress_avg"]) if today.get("stress_avg") is not None else None},
        {"label": "Training load (ACWR)", "value": f"{round(today['acwr_percent'])}%" if today.get("acwr_percent") is not None else None},
        {"label": "VO2max", "value": f"{vo2max} (as of {vo2max_date})" if vo2max else None},
    ]

    warning = acwr_warning_text(today.get("acwr_percent"), today.get("acwr_feedback"), today.get("acute_load"))

    return render_template(
        "dashboard.html",
        rings=rings,
        stats=stats,
        warning=warning,
        notice=partial_sync_text(today.get("sync_errors")),
        stale=is_stale(today.get("updated_at")),
        recommendation=today.get("recommendation_detail") or "No data yet today - waiting for first sync.",
        recommendation_type=today.get("recommendation_type"),
        synced_at=format_synced_at(today.get("updated_at")),
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=config.DASHBOARD_PORT)
