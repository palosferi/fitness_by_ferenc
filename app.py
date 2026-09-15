import hmac
import logging
import time
from datetime import date, datetime, timedelta
from threading import Lock

from flask import Blueprint, Flask, jsonify, redirect, render_template, request, url_for
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


def is_authenticated():
    """True only for the configured user with the right password.

    Everyone else - including every anonymous visitor - gets the demo day,
    never real data. No password configured means nobody is authenticated,
    and a locked-out IP is refused even with correct credentials.
    """
    if not config.DASHBOARD_PASSWORD:
        return False

    auth = request.authorization
    if not auth or not auth.username or auth.password is None:
        return False

    ip = client_ip()
    if is_locked_out(ip):
        return False

    user_ok = hmac.compare_digest(auth.username, config.DASHBOARD_USER)
    password_ok = hmac.compare_digest(auth.password, config.DASHBOARD_PASSWORD)

    if user_ok and password_ok:
        clear_failures(ip)
        return True

    record_failure(ip)
    return False


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
    formatted = f"{shown}{unit}" if unit == "%" else f"{shown} {unit}".strip()
    entry = {"label": label, "value": formatted, "pct": None,
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
                      resting_hr_baseline, higher_is_better=False),
        health_metric("Heart rate variability", today.get("hrv_last_night"), "ms",
                      today.get("hrv_baseline"), higher_is_better=True),
        health_metric("Respiratory rate", today.get("respiration_avg"), "rpm",
                      today.get("respiration_baseline"), higher_is_better=False, precision=1),
        health_metric("Blood oxygen", today.get("spo2_avg"), "%",
                      today.get("spo2_baseline"), higher_is_better=True),
        health_metric("VO2 max", today.get("vo2max"), "", None),
    ) if m]

    stress = stress_reading(today.get("stress_avg"))

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
        stress=stress,
        warning=warning,
        notice=None if demo_mode else partial_sync_text(today.get("sync_errors")),
        stale=False if demo_mode else is_stale(today.get("updated_at")),
        demo_mode=demo_mode,
        recommendation=today.get("recommendation_detail") or "No data yet today - waiting for first sync.",
        recommendation_type=today.get("recommendation_type"),
        synced_at=format_synced_at(today.get("updated_at")),
    )


@bp.route("/login")
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
    return redirect(url_for("dashboard.dashboard"))


app.register_blueprint(bp, url_prefix=config.URL_PREFIX or None)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=config.DASHBOARD_PORT)
