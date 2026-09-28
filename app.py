import hashlib
import hmac
import logging
import math
import os
import queue
import secrets
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from threading import Event, Lock, Thread

from flask import (Blueprint, Flask, abort, jsonify, redirect, render_template,
                   request, session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

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

app.config["SESSION_COOKIE_NAME"] = config.SESSION_COOKIE_NAME
app.config["SESSION_COOKIE_PATH"] = config.SESSION_COOKIE_PATH
app.permanent_session_lifetime = timedelta(days=config.SESSION_DAYS)


def _password_hash_on_disk():
    """The stored password hash, without creating a database to find out.

    This runs at import to seed the signing key, and opening the real
    database here would litter a fitness.db into whatever directory the
    process happens to start in.
    """
    if not os.path.exists(config.DB_PATH):
        return None
    try:
        return storage.get_setting(storage.get_conn(config.DB_PATH), "dashboard_password_hash")
    except sqlite3.Error:
        return None


def refresh_secret_key():
    """Key the session cookie off whatever authenticates people.

    Deriving it keeps sessions valid across restarts with no extra config,
    and invalidates every session the moment the password changes - which is
    what you would want anyway. Called again after setup, when the password
    that seeds it has only just come into existence.
    """
    if config.DASHBOARD_SECRET_KEY:
        app.secret_key = config.DASHBOARD_SECRET_KEY
        return
    basis = _password_hash_on_disk() or config.DASHBOARD_PASSWORD
    app.secret_key = (hashlib.sha256(f"fbf:{basis}".encode()).hexdigest() if basis
                      else os.urandom(32))  # nobody can authenticate anyway


refresh_secret_key()

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


def stored_account():
    """The username and password hash chosen on the setup page, if any.

    Takes precedence over DASHBOARD_USER/DASHBOARD_PASSWORD, which stay
    supported for an .env-configured deployment. The stored pair is what
    lets a second instance be handed to somebody else without whoever runs
    the server ever knowing their password.
    """
    try:
        conn = storage.get_conn(config.DB_PATH)
        return (storage.get_setting(conn, "dashboard_user"),
                storage.get_setting(conn, "dashboard_password_hash"))
    except sqlite3.Error:
        return (None, None)


def check_credentials(username, password):
    """Constant-time credential check, shared by the form and basic auth.

    Records a failure against the caller's IP so both routes feed the same
    lockout. No password configured means nobody authenticates.
    """
    if username is None or password is None:
        return False

    stored_user, stored_hash = stored_account()
    if stored_hash is None and not config.DASHBOARD_PASSWORD:
        return False

    ip = client_ip()
    if is_locked_out(ip):
        return False

    if stored_hash is not None:
        user_ok = hmac.compare_digest(username, stored_user or config.DASHBOARD_USER)
        password_ok = check_password_hash(stored_hash, password)
    else:
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


def health_metric(label, value, unit, baseline, icon, higher_is_better=True,
                  precision=0, normal_range=None):
    """One Health Monitor row: today's value against what's normal for you.

    `normal_range` (low, high) is preferred when the source gives a real
    band - Garmin's HRV "balanced" range, say - because a band is what
    "normal for you" actually is. Falling back to a single baseline, the
    direction matters: HRV above baseline is a good sign, resting HR above
    baseline is not, which is what `higher_is_better` decides.
    """
    if value is None:
        return None

    shown = round(value, precision) if precision else round(value)
    formatted = f"{shown}{unit}" if unit == "%" else f"{shown} {unit}".strip()
    entry = {"label": label, "value": formatted, "icon": icon, "pct": None,
             "status": "unknown", "delta": None, "band_start": None, "band_width": None}

    if normal_range and normal_range[0] is not None and normal_range[1] is not None:
        low, high = normal_range
        span = max(high - low, 1)
        # Show the band with a quarter-span of headroom on either side.
        axis_low, axis_high = low - span * 0.25, high + span * 0.25
        axis_span = axis_high - axis_low
        entry["pct"] = max(0, min(100, (value - axis_low) / axis_span * 100))
        entry["band_start"] = (low - axis_low) / axis_span * 100
        entry["band_width"] = span / axis_span * 100
        entry["status"] = "good" if low <= value <= high else "watch"
        entry["delta"] = f"normal range {round(low)}-{round(high)}"
        return entry

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
    """Roll the rows up into the tile's one-glance verdict.

    Always returns a summary, so the tile keeps its place on a day with no
    readings (or none that can be judged yet) instead of vanishing.
    """
    scored = [m for m in metrics if m["status"] != "unknown"]
    if not metrics:
        return {"status": "unknown", "status_text": "No Data", "count_text": "no readings yet",
                "detail_text": "No health readings for today yet"}
    if not scored:
        return {"status": "unknown", "status_text": "No Baseline",
                "count_text": f"0/{len(metrics)} tracked",
                "detail_text": f"{len(metrics)} shown, none with a baseline yet"}
    in_range = sum(1 for m in scored if m["status"] == "good")
    all_good = in_range == len(scored)
    # Only metrics with a baseline can be judged; the rest are shown but not
    # scored, so say "of N tracked" rather than implying N is all of them.
    tracked = len(scored)
    untracked = len(metrics) - tracked
    suffix = f" ({untracked} without a baseline yet)" if untracked else ""
    return {
        "status": "good" if all_good else "watch",
        "status_text": "Within Range" if all_good else "Out of Range",
        "count_text": f"{in_range}/{tracked} tracked",
        "detail_text": ((f"All {tracked} tracked metrics in your normal range" if all_good
                         else f"{in_range} of {tracked} tracked metrics in your normal range") + suffix),
    }


# Garmin's own stress bands (0-100), which map closely onto what Whoop's
# stress monitor conveys qualitatively.
STRESS_BANDS = ((25, "Rest", "#30a46c"), (50, "Low", "#7cc35f"),
                (75, "Medium", "#f5a623"), (101, "High", "#e5484d"))


def format_iso_as_local(timestamp):
    """Parse an offset-aware ISO timestamp and render it in local time."""
    if not timestamp:
        return None
    try:
        return datetime.fromisoformat(timestamp).astimezone().strftime("%H:%M")
    except ValueError:
        return None


def stress_reading(value):
    """Reading plus the marker's x/y on a semicircular gauge.

    The trig lives here rather than in the template so the arc geometry is
    testable and the markup stays declarative. Garmin's "no reading" is -1,
    and rows synced before extract_stress filtered it still carry it.
    """
    if value is None or value < 0:
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


def format_gmt_as_local(gmt_timestamp):
    """Garmin's lastSyncTimestampGMT is GMT with no offset marker; rendering
    it raw would show a time hours off from everything else on the page."""
    if not gmt_timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(gmt_timestamp)
    except ValueError:
        return None
    local = parsed.replace(tzinfo=timezone.utc).astimezone()
    return local.strftime("%b %d, %H:%M")


def format_duration(minutes):
    """Minutes as 8h40m. Garmin reports sleep need in whole minutes, so
    showing 8.7h threw away precision and read like a decimal clock."""
    if not minutes:
        return None
    hours, mins = divmod(int(round(minutes)), 60)
    return f"{hours}h{mins:02d}m" if mins else f"{hours}h"


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


def _mean_of(rows, field, min_samples=None):
    """Mean of a stored column, or None until enough nights back it.

    Averaging one or two rows produces a number that sits on the bar looking
    exactly as authoritative as a fortnight's worth, and a metric judged
    against it swings between "normal" and "alert" on noise alone.
    """
    if min_samples is None:
        min_samples = config.MIN_BASELINE_SAMPLES
    values = [r[field] for r in rows if r.get(field)]
    if len(values) < min_samples:
        return None
    return sum(values) / len(values)


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
    sleep_need = today.get("sleep_need_minutes")
    if not sleep_need and today.get("sleep_recommendation_hours"):
        sleep_need = today["sleep_recommendation_hours"] * 60

    # Garmin ships 7-day baselines for resting HR and SpO2 and a long-run
    # "balanced" range for HRV, all available from day one. Only respiration
    # has none, so that falls back to averaging our own rows - and stays empty
    # until config.MIN_BASELINE_SAMPLES of them exist. The row still shows
    # today's value meanwhile; it just isn't scored against a baseline thin
    # enough to invent an alert.
    resting_hr_baseline = today.get("resting_hr_baseline")
    if not demo_mode and today.get("respiration_baseline") is None:
        history = storage.get_recent_days(conn, date.today().isoformat(), limit=7)
        today["respiration_baseline"] = _mean_of(history, "respiration_avg")
        if resting_hr_baseline is None:
            resting_hr_baseline = _mean_of(history, "resting_hr")

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
                      today.get("hrv_baseline"), "pulse", higher_is_better=True,
                      normal_range=(today.get("hrv_balanced_low"), today.get("hrv_balanced_high"))),
        health_metric("Respiratory rate", today.get("respiration_avg"), "rpm",
                      today.get("respiration_baseline"), "lungs",
                      higher_is_better=False, precision=1),
        health_metric("Blood oxygen", today.get("spo2_avg"), "%",
                      today.get("spo2_baseline"), "droplet", higher_is_better=True),
        health_metric("VO2 max", today.get("vo2max"), "", None, "gauge"),
    ) if m]

    # The gauge shows the most recent actual reading, timestamped with when
    # Garmin took it - not the day average stamped with our fetch time, which
    # read as a live measurement it wasn't.
    stress = stress_reading(today.get("stress_latest"))
    if stress is None:
        stress = stress_reading(today.get("stress_avg"))
        if stress:
            stress["time"] = None
            stress["caption"] = "day average"
    else:
        stress["time"] = format_iso_as_local(today.get("stress_latest_at"))
        parts = []
        for key, name in (("stress_avg", "avg"), ("stress_max", "peak")):
            if today.get(key) is not None and today[key] >= 0:
                parts.append(f"{name} {round(today[key])}")
        stress["caption"] = " · ".join(parts) if parts else None

    stats = [
        {"label": "Steps", "value": f"{steps:,}" if steps else None},
        {"label": "Sleep target tonight", "value": format_duration(sleep_need)},
    ]

    warning = acwr_warning_text(today.get("acwr_percent"), today.get("acwr_feedback"), today.get("acute_load"))

    # A partial sync is the more urgent thing to say, so an expired Garmin
    # session only surfaces when nothing else is competing for the line.
    # Without it the dashboard just freezes on the last good day, which reads
    # as "nothing happened today" rather than "sign in again".
    notice = None
    if not demo_mode:
        notice = partial_sync_text(today.get("sync_errors"))
        if notice is None and storage.get_setting(conn, "garmin_link_state") == "needs_login":
            notice = (f"Garmin sign-in has expired - reconnect at {url_for('dashboard.setup')} "
                      "to start syncing again.")

    return render_template(
        "dashboard.html",
        rings=rings,
        stats=stats,
        health_metrics=health_metrics,
        health_summary=health_summary(health_metrics),
        stress=stress,
        warning=warning,
        notice=notice,
        stale=False if demo_mode else is_stale(today.get("updated_at")),
        demo_mode=demo_mode,
        recommendation=today.get("recommendation_detail") or "No data yet today - waiting for first sync.",
        recommendation_type=today.get("recommendation_type"),
        synced_at=format_synced_at(today.get("updated_at")),
        watch_synced_at=format_gmt_as_local(today.get("watch_synced_at")),
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


# ---- account setup ---------------------------------------------------
#
# The setup page exists so an instance can be handed to somebody else without
# them dictating their Garmin password to whoever runs the server. They type
# it into the form, it is exchanged for Garmin tokens once, and it is never
# written to disk.
#
# The exchange runs on a background thread because the library asks for an
# MFA code through a blocking callback, and that code arrives in a second
# HTTP request. Holding the pending login in module state is only safe
# because the dashboard runs a single gunicorn worker (see
# systemd/fitness-dashboard.service) - with more than one, the MFA form could
# land in a process that knows nothing about the login waiting in another.

_pending_links = {}
_pending_lock = Lock()


class _PendingLink:
    def __init__(self, dashboard_user, dashboard_password):
        self.dashboard_user = dashboard_user
        self.dashboard_password = dashboard_password
        self.started = time.time()
        self.mfa_requested = Event()
        self.codes = queue.Queue(maxsize=1)
        self.result = queue.Queue(maxsize=1)

    def prompt_mfa(self):
        self.mfa_requested.set()
        return self.codes.get(timeout=config.SETUP_MFA_TIMEOUT_SECONDS)


def _link_account(email, password, prompt_mfa):
    """Imported lazily on purpose: garminconnect is a production-only
    dependency, and the dashboard and its tests still have to run on a
    checkout without it installed."""
    from garmin_client import link_account
    return link_account(email, password, prompt_mfa)


def _run_link(pending, email, password):
    try:
        _link_account(email, password, pending.prompt_mfa)
        pending.result.put(("ok", None))
    except queue.Empty:
        pending.result.put(("error", "Timed out waiting for the MFA code - start again."))
    except Exception as exc:
        log.warning("Garmin link attempt failed: %s", exc)
        pending.result.put(("error", "Garmin did not accept those details."))


def _await_link(pending, timeout):
    """Wait for the background login to finish, or to ask for an MFA code."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            return pending.result.get(timeout=0.2)
        except queue.Empty:
            pass
        if pending.mfa_requested.is_set():
            pending.mfa_requested.clear()
            return ("mfa", None)
    return ("error", "Garmin didn't respond in time - try again.")


def _drop_pending(pending_id):
    with _pending_lock:
        _pending_links.pop(pending_id, None)


def _drop_abandoned_pending():
    """A setup someone walked away from still holds their chosen password in
    memory. Nothing else clears it, so clear it here - the background thread
    it belongs to has given up by this point anyway."""
    cutoff = time.time() - config.SETUP_MFA_TIMEOUT_SECONDS
    with _pending_lock:
        for pending_id in [k for k, v in _pending_links.items() if v.started < cutoff]:
            del _pending_links[pending_id]


def _finish_link(pending):
    conn = storage.get_conn(config.DB_PATH)
    if pending.dashboard_password:
        storage.set_setting(conn, "dashboard_user", pending.dashboard_user)
        storage.set_setting(conn, "dashboard_password_hash",
                            generate_password_hash(pending.dashboard_password))
        pending.dashboard_password = None
    storage.set_setting(conn, "garmin_link_state", "linked")
    refresh_secret_key()
    session.clear()
    session.permanent = True
    session["authed"] = True


def account_is_claimed():
    return bool(stored_account()[1]) or bool(config.DASHBOARD_PASSWORD)


def setup_allowed(token):
    """Either already signed in, or holding the token for a fresh instance.

    The token travels in the URL and then in a hidden field rather than the
    session, because an unclaimed instance has no password to key a session
    cookie with - its signing key is random per process, so a restart
    mid-setup would silently lose it.
    """
    if is_authenticated():
        return True
    return bool(config.SETUP_TOKEN and token and hmac.compare_digest(token, config.SETUP_TOKEN))


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    token = request.values.get("token", "")
    if not setup_allowed(token):
        # 404 rather than 403 - an instance nobody has claimed yet shouldn't
        # advertise that it is claimable.
        abort(404)

    needs_account = not account_is_claimed()
    error = None

    if request.method == "POST":
        email = request.form.get("garmin_email", "").strip()
        password = request.form.get("garmin_password", "")
        dashboard_user = request.form.get("dashboard_user", "").strip()
        dashboard_password = request.form.get("dashboard_password", "")

        if needs_account and (not dashboard_user or not dashboard_password):
            error = "Choose a username and password for the dashboard itself."
        elif not email or not password:
            error = "Enter your Garmin Connect email and password."
        else:
            pending = _PendingLink(dashboard_user or config.DASHBOARD_USER,
                                   dashboard_password if needs_account else None)
            pending_id = secrets.token_urlsafe(16)
            _drop_abandoned_pending()
            with _pending_lock:
                _pending_links[pending_id] = pending
            Thread(target=_run_link, args=(pending, email, password), daemon=True).start()

            status, detail = _await_link(pending, config.SETUP_LINK_TIMEOUT_SECONDS)
            if status == "ok":
                _drop_pending(pending_id)
                _finish_link(pending)
                return redirect(url_for("dashboard.dashboard"))
            if status == "mfa":
                return render_template("setup.html", step="mfa", token=token,
                                       pending=pending_id, needs_account=needs_account,
                                       error=None)
            _drop_pending(pending_id)
            error = detail

    return render_template("setup.html", step="credentials", token=token,
                           pending=None, needs_account=needs_account,
                           error=error), (200 if error is None else 400)


@bp.route("/setup/mfa", methods=["POST"])
def setup_mfa():
    token = request.values.get("token", "")
    if not setup_allowed(token):
        abort(404)

    pending_id = request.form.get("pending", "")
    with _pending_lock:
        pending = _pending_links.get(pending_id)

    def back_to_credentials(message):
        return render_template("setup.html", step="credentials", token=token,
                               pending=None, needs_account=not account_is_claimed(),
                               error=message), 400

    if pending is None:
        return back_to_credentials("That setup attempt has expired - start again.")

    pending.codes.put(request.form.get("code", "").strip())
    status, detail = _await_link(pending, config.SETUP_LINK_TIMEOUT_SECONDS)
    if status == "ok":
        _drop_pending(pending_id)
        _finish_link(pending)
        return redirect(url_for("dashboard.dashboard"))
    if status == "mfa":
        # Garmin asked a second time, so the code was wrong or had rolled over.
        return render_template("setup.html", step="mfa", token=token,
                               pending=pending_id, needs_account=False,
                               error="That code wasn't accepted - try the current one."), 400

    _drop_pending(pending_id)
    return back_to_credentials(detail)


app.register_blueprint(bp, url_prefix=config.URL_PREFIX or None)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=config.DASHBOARD_PORT)
