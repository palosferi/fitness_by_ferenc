import hashlib
import hmac
import logging
import math
import os
import time
from datetime import date, datetime, timedelta
from threading import Lock

from flask import (Blueprint, Flask, jsonify, redirect, render_template, request,
                   session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix

import config
import demo
import storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dashboard")

app = Flask(__name__)

# Behind NPM, request.remote_addr is the proxy's own address - without this
# every visitor shares one "IP" and the lockout below would bolt the door on
# everyone at once. x_for must match the real number of proxies, or a client
# could spoof X-Forwarded-For and dodge the throttle.
if config.PROXY_HOP_COUNT:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=config.PROXY_HOP_COUNT, x_proto=config.PROXY_HOP_COUNT)

if config.DASHBOARD_SECRET_KEY:
    app.secret_key = config.DASHBOARD_SECRET_KEY
elif config.DASHBOARD_PASSWORD:
    app.secret_key = hashlib.sha256(f"fbf:{config.DASHBOARD_PASSWORD}".encode()).hexdigest()
else:
    app.secret_key = os.urandom(32)  # nobody can authenticate anyway
app.permanent_session_lifetime = timedelta(days=config.SESSION_DAYS)

bp = Blueprint("dashboard", __name__)

_failures = {}
_failures_lock = Lock()


def client_ip():
    return request.remote_addr or "unknown"


def is_locked_out(ip):
    with _failures_lock:
        record = _failures.get(ip)
        if not record:
            return False
        count, last_failure = record
        if count < config.AUTH_MAX_ATTEMPTS:
            return False
        if time.time() - last_failure > config.AUTH_LOCKOUT_SECONDS:
            del _failures[ip]
            return False
        return True


def record_failure(ip):
    with _failures_lock:
        count, _ = _failures.get(ip, (0, 0))
        _failures[ip] = (count + 1, time.time())
        attempts = count + 1
    # Logged in a fixed shape so fail2ban can match it - see
    # systemd/fail2ban/ for the filter and jail.
    log.warning("dashboard auth failure from %s (attempt %s)", ip, attempts)


def clear_failures(ip):
    with _failures_lock:
        _failures.pop(ip, None)


def check_credentials(username, password):
    """Constant-time credential check, shared by the form and basic auth.

    Records a failure against the caller's IP so both routes feed the same
    lockout. No password configured means nobody authenticates.
    """
    if not config.DASHBOARD_PASSWORD:
        return False
    if username is None or password is None:
        return False

    ip = client_ip()
    if is_locked_out(ip):
        return False

    user_ok = hmac.compare_digest(username, config.DASHBOARD_USER)
    password_ok = hmac.compare_digest(password, config.DASHBOARD_PASSWORD)

    if user_ok and password_ok:
        clear_failures(ip)
        return True

    record_failure(ip)
    return False


def is_authenticated():
    """A valid login session, or basic auth on the request.

    The session cookie is what browsers use - a 401 challenge never prompts
    inside iOS webviews, it just renders the error body. Basic auth stays
    supported because it's the right fit for the widget's API calls.
    """
    if session.get("authed") is True:
        return True

    auth = request.authorization
    if not auth or not auth.username or auth.password is None:
        return False
    return check_credentials(auth.username, auth.password)


def ring_color(value, thresholds=(config.RING_LOW_THRESHOLD, config.RING_HIGH_THRESHOLD)):
    if value is None:
        return "#888888"
    if value < thresholds[0]:
        return "#e5484d"
    if value < thresholds[1]:
        return "#f5a623"
    return "#30a46c"


# The Health Monitor bar spans baseline +/-BAR_SPAN; the shaded band marks
# the range we treat as normal, so "in range" is something you see rather
# than something you have to work out from the number.
HEALTH_BAR_SPAN = 0.25
HEALTH_NORMAL_BAND = 0.07
HEALTH_WATCH_BAND = 0.15


def health_metric(label, value, unit, baseline, icon, higher_is_better=True, precision=0):
    """One Health Monitor row: today's value against your own baseline.

    Direction matters - HRV above baseline is a good sign, resting HR above
    baseline is not - so `higher_is_better` decides which way is "green".
    """
    if value is None:
        return None

    shown = round(value, precision) if precision else round(value)
    formatted = f"{shown}{unit}" if unit == "%" else f"{shown} {unit}".strip()
    entry = {"label": label, "value": formatted, "icon": icon, "pct": None,
             "status": "unknown", "delta": None, "band_start": None, "band_width": None}

    if not baseline:
        return entry

    deviation = (value - baseline) / baseline
    entry["pct"] = max(0, min(100, 50 + (deviation / HEALTH_BAR_SPAN) * 50))

    band_half = (HEALTH_NORMAL_BAND / HEALTH_BAR_SPAN) * 50
    entry["band_start"] = 50 - band_half
    entry["band_width"] = band_half * 2

    favourable = deviation >= 0 if higher_is_better else deviation <= 0
    magnitude = abs(deviation)
    if favourable or magnitude <= HEALTH_NORMAL_BAND:
        entry["status"] = "good"
    elif magnitude <= HEALTH_WATCH_BAND:
        entry["status"] = "watch"
    else:
        entry["status"] = "alert"

    baseline_shown = round(baseline, precision) if precision else round(baseline)
    delta = value - baseline
    sign = "+" if delta >= 0 else "-"
    delta_shown = round(abs(delta), precision) if precision else round(abs(delta))
    entry["delta"] = f"{sign}{delta_shown} vs baseline {baseline_shown}"
    return entry


def health_summary(metrics):
    """Roll the rows up into the tile's one-glance verdict."""
    scored = [m for m in metrics if m["status"] != "unknown"]
    if not scored:
        return None
    in_range = sum(1 for m in scored if m["status"] == "good")
    all_good = in_range == len(scored)
    return {
        "status": "good" if all_good else "watch",
        "status_text": "Within Range" if all_good else "Out of Range",
        "count_text": f"{in_range}/{len(scored)} Metrics",
        "detail_text": (f"All {len(scored)} metrics in your normal range" if all_good
                        else f"{in_range} of {len(scored)} metrics in your normal range"),
    }


# Garmin's own stress bands (0-100), which map closely onto what Whoop's
# stress monitor conveys qualitatively.
STRESS_BANDS = ((25, "Rest", "#30a46c"), (50, "Low", "#7cc35f"),
                (75, "Medium", "#f5a623"), (101, "High", "#e5484d"))


def stress_reading(value):
    """Reading plus the marker's x/y on a semicircular gauge.

    The trig lives here rather than in the template so the arc geometry is
    testable and the markup stays declarative.
    """
    if value is None:
        return None

    pct = max(0, min(100, value))
    # Semicircle swept left (180 deg) to right (360 deg) over a 200x100 box.
    angle = math.radians(180 + (pct / 100) * 180)
    radius = 78

    for ceiling, label, color in STRESS_BANDS:
        if value < ceiling:
            return {
                "value": round(value),
                "label": label,
                "color": color,
                "pct": pct,
                "marker_x": round(100 + radius * math.cos(angle), 2),
                "marker_y": round(90 + radius * math.sin(angle), 2),
            }
    return None


def is_stale(updated_at, stale_minutes=config.SYNC_STALE_MINUTES):
    if not updated_at:
        return True
    try:
        updated = datetime.fromisoformat(updated_at)
    except ValueError:
        return True
    return datetime.now() - updated > timedelta(minutes=stale_minutes)


def format_time_only(updated_at):
    if not updated_at:
        return None
    try:
        return datetime.fromisoformat(updated_at).strftime("%H:%M")
    except ValueError:
        return None


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


def _mean_of(rows, field):
    values = [r[field] for r in rows if r.get(field)]
    return sum(values) / len(values) if values else None


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


@bp.route("/api/today")
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


@bp.route("/")
def dashboard():
    demo_mode = not is_authenticated()
    if demo_mode:
        today = demo.generate_day()
    else:
        conn = storage.get_conn(config.DB_PATH)
        today = storage.get_day(conn, date.today().isoformat()) or {}

    steps = today.get("steps")
    sleep_hours = today.get("sleep_recommendation_hours")

    # HRV and SpO2 arrive with Garmin's own baselines; resting HR and
    # respiration don't, so derive those from the last week of our own rows.
    resting_hr_baseline = today.get("resting_hr_baseline")
    if resting_hr_baseline is None and not demo_mode:
        history = storage.get_recent_days(conn, date.today().isoformat(), limit=7)
        resting_hr_baseline = _mean_of(history, "resting_hr")
        today["respiration_baseline"] = _mean_of(history, "respiration_avg")

    # Three rings, Whoop's three: Recovery, Sleep, Strain. Target was dropped
    # because recommend_training() sets it equal to readiness on most days, so
    # it showed the same number twice; Body Battery was dropped because it's a
    # live gauge that a periodically-refreshed ring can't convey anyway.
    readiness = today.get("readiness_score")
    sleep_score = today.get("sleep_score")
    strain_score = today.get("strain_score")

    rings = [
        {
            "label": "Sleep",
            "display": round(sleep_score) if sleep_score is not None else None,
            "pct": pct_for("score", sleep_score),
            "color": ring_color(sleep_score, thresholds=(34, 67)),
        },
        {
            "label": "Recovery",
            "display": round(readiness) if readiness is not None else None,
            "pct": pct_for("score", readiness),
            "color": ring_color(readiness, thresholds=(34, 67)),
        },
        {
            "label": "Strain",
            "display": round(strain_score) if strain_score is not None else None,
            "pct": pct_for("score", strain_score),
            "color": "#5b8def",
        },
    ]

    health_metrics = [m for m in (
        health_metric("Resting heart rate", today.get("resting_hr"), "bpm",
                      resting_hr_baseline, "heart", higher_is_better=False),
        health_metric("Heart rate variability", today.get("hrv_last_night"), "ms",
                      today.get("hrv_baseline"), "pulse", higher_is_better=True),
        health_metric("Respiratory rate", today.get("respiration_avg"), "rpm",
                      today.get("respiration_baseline"), "lungs",
                      higher_is_better=False, precision=1),
        health_metric("Blood oxygen", today.get("spo2_avg"), "%",
                      today.get("spo2_baseline"), "droplet", higher_is_better=True),
        health_metric("VO2 max", today.get("vo2max"), "", None, "gauge"),
    ) if m]

    stress = stress_reading(today.get("stress_avg"))
    if stress:
        stress["time"] = format_time_only(today.get("updated_at"))

    stats = [
        {"label": "Steps", "value": f"{steps:,}" if steps else None},
        {"label": "Sleep target tonight", "value": f"{sleep_hours}h" if sleep_hours else None},
    ]

    warning = acwr_warning_text(today.get("acwr_percent"), today.get("acwr_feedback"), today.get("acute_load"))

    return render_template(
        "dashboard.html",
        rings=rings,
        stats=stats,
        health_metrics=health_metrics,
        health_summary=health_summary(health_metrics),
        stress=stress,
        warning=warning,
        notice=None if demo_mode else partial_sync_text(today.get("sync_errors")),
        stale=False if demo_mode else is_stale(today.get("updated_at")),
        demo_mode=demo_mode,
        recommendation=today.get("recommendation_detail") or "No data yet today - waiting for first sync.",
        recommendation_type=today.get("recommendation_type"),
        synced_at=format_synced_at(today.get("updated_at")),
    )


@bp.route("/login", methods=["GET", "POST"])
def login():
    if is_authenticated():
        return redirect(url_for("dashboard.dashboard"))

    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if check_credentials(username, password):
            session.permanent = True
            session["authed"] = True
            return redirect(url_for("dashboard.dashboard"))
        error = ("Too many attempts - try again in a few minutes."
                 if is_locked_out(client_ip()) else "Wrong username or password.")

    return render_template("login.html", error=error), (200 if error is None else 401)


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("dashboard.dashboard"))


app.register_blueprint(bp, url_prefix=config.URL_PREFIX or None)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=config.DASHBOARD_PORT)
