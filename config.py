import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

DB_PATH = os.environ.get("FITNESS_DB_PATH", str(BASE_DIR / "fitness.db"))
GARMIN_TOKENSTORE = os.environ.get("GARMIN_TOKENSTORE", str(BASE_DIR / ".garminconnect_tokens"))

GARMIN_EMAIL = os.environ.get("GARMIN_EMAIL")
GARMIN_PASSWORD = os.environ.get("GARMIN_PASSWORD")

USER_AGE = int(os.environ.get("USER_AGE", "32"))
USER_MAX_HR = int(os.environ.get("USER_MAX_HR", str(220 - USER_AGE)))
USER_EASY_PACE_MIN_PER_KM = float(os.environ.get("USER_EASY_PACE_MIN_PER_KM", "6.0"))

# Banister TRIMP exponential constant - 1.92 is the standard "male" value,
# 1.67 is typically used for women. A genuine per-user knob, unlike the
# other constants in scoring.py which just shape the algorithm internally.
USER_TRIMP_EXPONENT = float(os.environ.get("USER_TRIMP_EXPONENT", "1.92"))

DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "8420"))

# Ring/stat coloring thresholds (red below LOW, amber below HIGH, green above).
RING_LOW_THRESHOLD = int(os.environ.get("RING_LOW_THRESHOLD", "40"))
RING_HIGH_THRESHOLD = int(os.environ.get("RING_HIGH_THRESHOLD", "70"))

# Below this ACWR percent (or on a POOR/HIGH/RISK Garmin feedback label),
# surface the training-load-risk badge on the dashboard.
ACWR_RISK_THRESHOLD_PERCENT = int(os.environ.get("ACWR_RISK_THRESHOLD_PERCENT", "60"))

# If the last sync is older than this, the dashboard/widget show a "stale"
# notice instead of quietly displaying old numbers. Default is 2.5x the
# 30-minute sync timer interval, so a single missed/slow run doesn't flap it.
SYNC_STALE_MINUTES = int(os.environ.get("SYNC_STALE_MINUTES", "75"))

BACKUP_DIR = os.environ.get("BACKUP_DIR", str(BASE_DIR / "backups"))
BACKUP_RETENTION_DAYS = int(os.environ.get("BACKUP_RETENTION_DAYS", "30"))

# Credentials for seeing *real* data. Anonymous visitors always get the
# synthetic demo day instead. Fail-closed on purpose: with no password set,
# nobody gets real data - health data should never be one config typo away
# from being public.
DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "ferenc")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD")

# Brute-force throttling. Basic auth on its own lets an attacker guess as
# fast as they can send requests, so lock an IP out after a few failures.
AUTH_MAX_ATTEMPTS = int(os.environ.get("AUTH_MAX_ATTEMPTS", "5"))
AUTH_LOCKOUT_SECONDS = int(os.environ.get("AUTH_LOCKOUT_SECONDS", "300"))

# Signs the login session cookie. Left unset it's derived from the password,
# which keeps sessions valid across restarts with no extra config - and
# invalidates every session when you change the password, which is what you'd
# want anyway.
DASHBOARD_SECRET_KEY = os.environ.get("DASHBOARD_SECRET_KEY")
SESSION_DAYS = int(os.environ.get("SESSION_DAYS", "30"))

# Path the app is served under, when it sits behind a reverse proxy at a
# sub-path (e.g. "/fit"). Empty means it owns the domain root.
URL_PREFIX = os.environ.get("URL_PREFIX", "").rstrip("/")

# Number of reverse proxies in front of the app. Used to read the real
# client IP from X-Forwarded-For; must match reality or the throttle above
# either blocks everyone at once or can be spoofed.
PROXY_HOP_COUNT = int(os.environ.get("PROXY_HOP_COUNT", "1"))
