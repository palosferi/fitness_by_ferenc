import hmac
from datetime import date, datetime, timedelta

from flask import Flask, jsonify, redirect, render_template, request

import config
import demo
import storage

app = Flask(__name__)


def is_authenticated():
    """True only for the configured user with the right password.

    Everyone else - including every anonymous visitor - gets the demo day,
    never real data. No password configured means nobody is authenticated.
    """
    if not config.DASHBOARD_PASSWORD:
        return False
    auth = request.authorization
    if not auth or not auth.username or auth.password is None:
        return False
    user_ok = hmac.compare_digest(auth.username, config.DASHBOARD_USER)
    password_ok = hmac.compare_digest(auth.password, config.DASHBOARD_PASSWORD)
    return user_ok and password_ok


def ring_color(value, thresholds=(config.RING_LOW_THRESHOLD, config.RING_HIGH_THRESHOLD)):
    if value is None:
        return "#888888"
    if value < thresholds[0]:
        return "#e5484d"
    if value < thresholds[1]:
        return "#f5a623"
    return "#30a46c"


def health_metric(label, value, unit, baseline, higher_is_better=True, precision=0):
    """One Health Monitor row: a value plus where it sits against your own
    baseline. The bar spans baseline +/-25%; the marker shows today.

    Direction matters - HRV above baseline is a good sign, resting HR above
    baseline is not - so `higher_is_better` decides which way is "green".
    """
    if value is None:
        return None

    shown = round(value, precision) if precision else round(value)
    entry = {"label": label, "value": f"{shown} {unit}".strip(), "pct": None,
             "status": "unknown", "delta": None}

    if not baseline:
        return entry

    deviation = (value - baseline) / baseline
    entry["pct"] = max(0, min(100, 50 + (deviation / 0.25) * 50))

    favourable = deviation >= 0 if higher_is_better else deviation <= 0
    magnitude = abs(deviation)
    if favourable or magnitude <= 0.07:
        entry["status"] = "good"
    elif magnitude <= 0.15:
        entry["status"] = "watch"
    else:
        entry["status"] = "alert"

    baseline_shown = round(baseline, precision) if precision else round(baseline)
    entry["delta"] = f"baseline {baseline_shown}"
    return entry


# Garmin's own stress bands (0-100), which map closely onto what Whoop's
# stress monitor conveys qualitatively.
STRESS_BANDS = ((25, "Rest", "#30a46c"), (50, "Low", "#7cc35f"),
                (75, "Medium", "#f5a623"), (101, "High", "#e5484d"))


def stress_reading(value):
    if value is None:
        return None
    for ceiling, label, color in STRESS_BANDS:
        if value < ceiling:
            return {"value": round(value), "label": label, "color": color,
                    "pct": max(0, min(100, value))}
    return None


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
    if not is_authenticated():
        payload = demo.generate_day()
        payload["demo"] = True
        payload["stale"] = False
        return jsonify(payload)

    conn = storage.get_conn(config.DB_PATH)
    today = storage.get_day(conn, date.today().isoformat())
    if not today:
        return jsonify({"error": "no data yet"}), 404
    today["demo"] = False
    today["stale"] = is_stale(today.get("updated_at"))
    return jsonify(today)


@app.route("/")
def dashboard():
    demo_mode = not is_authenticated()
    if demo_mode:
        today = demo.generate_day()
    else:
        conn = storage.get_conn(config.DB_PATH)
        today = storage.get_day(conn, date.today().isoformat()) or {}

    steps = today.get("steps")
    sleep_hours = today.get("sleep_recommendation_hours")
    battery = today.get("body_battery")
    charged = today.get("body_battery_charged")
    drained = today.get("body_battery_drained")

    resting_hr_baseline = today.get("resting_hr_baseline")
    if resting_hr_baseline is None and not demo_mode:
        history = storage.get_recent_days(conn, date.today().isoformat(), limit=7)
        recent_rhr = [h["resting_hr"] for h in history if h.get("resting_hr")]
        resting_hr_baseline = sum(recent_rhr) / len(recent_rhr) if recent_rhr else None

    # Three rings, Whoop's three: Recovery, Sleep, Strain. Target was dropped
    # because recommend_training() sets it equal to readiness on most days, so
    # it showed the same number twice; Body Battery was dropped because it's a
    # live gauge that a periodically-refreshed ring can't convey anyway.
    readiness = today.get("readiness_score")
    sleep_score = today.get("sleep_score")
    strain_score = today.get("strain_score")

    rings = [
        {
            "label": "Recovery",
            "display": round(readiness) if readiness is not None else None,
            "suffix": "%",
            "pct": pct_for("score", readiness),
            "color": ring_color(readiness, thresholds=(34, 67)),
        },
        {
            "label": "Sleep",
            "display": round(sleep_score) if sleep_score is not None else None,
            "suffix": "%",
            "pct": pct_for("score", sleep_score),
            "color": ring_color(sleep_score, thresholds=(34, 67)),
        },
        {
            "label": "Day Strain",
            "display": round(strain_score) if strain_score is not None else None,
            "suffix": "",
            "pct": pct_for("score", strain_score),
            "color": "#5b8def",
        },
    ]

    health_metrics = [m for m in (
        health_metric("Resting heart rate", today.get("resting_hr"), "bpm",
                      resting_hr_baseline, higher_is_better=False),
        health_metric("Heart rate variability", today.get("hrv_last_night"), "ms",
                      today.get("hrv_baseline"), higher_is_better=True),
        health_metric("VO2 max", today.get("vo2max"), "", None),
    ) if m]

    stress = stress_reading(today.get("stress_avg"))

    battery_value = None
    if battery is not None:
        battery_value = f"{round(battery)}"
        if charged is not None:
            battery_value += f" (+{round(charged)} / -{round(drained)})"
    elif charged is not None:
        battery_value = f"+{round(charged)} / -{round(drained)}"

    stats = [
        {"label": "Steps", "value": f"{steps:,}" if steps else None},
        {"label": "Sleep target tonight", "value": f"{sleep_hours}h" if sleep_hours else None},
        {"label": "Body Battery", "value": battery_value},
        {"label": "Training load (ACWR)", "value": f"{round(today['acwr_percent'])}%" if today.get("acwr_percent") is not None else None},
    ]

    warning = acwr_warning_text(today.get("acwr_percent"), today.get("acwr_feedback"), today.get("acute_load"))

    return render_template(
        "dashboard.html",
        rings=rings,
        stats=stats,
        health_metrics=health_metrics,
        stress=stress,
        warning=warning,
        notice=None if demo_mode else partial_sync_text(today.get("sync_errors")),
        stale=False if demo_mode else is_stale(today.get("updated_at")),
        demo_mode=demo_mode,
        recommendation=today.get("recommendation_detail") or "No data yet today - waiting for first sync.",
        recommendation_type=today.get("recommendation_type"),
        synced_at=format_synced_at(today.get("updated_at")),
    )


@app.route("/login")
def login():
    """Basic-auth entry point.

    The dashboard itself never returns 401 (anonymous visitors get the demo
    instead of a password prompt), so this is what actually triggers the
    browser's credential dialog. Once authenticated, the browser sends the
    header on subsequent requests and "/" starts serving real data.
    """
    if not is_authenticated():
        return (
            "Authentication required.",
            401,
            {"WWW-Authenticate": 'Basic realm="fitness_by_ferenc"'},
        )
    return redirect("/")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=config.DASHBOARD_PORT)
