import sqlite3
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily (
    date TEXT PRIMARY KEY,
    resting_hr REAL,
    max_hr REAL,
    hrv_last_night REAL,
    hrv_baseline REAL,
    sleep_score REAL,
    sleep_duration_min REAL,
    steps INTEGER,
    trimp REAL,
    strain_score REAL,
    readiness_score REAL,
    readiness_source TEXT,
    target_strain REAL,
    sleep_recommendation_hours REAL,
    body_battery REAL,
    body_battery_charged REAL,
    body_battery_drained REAL,
    stress_avg REAL,
    acwr_percent REAL,
    acwr_feedback TEXT,
    acute_load REAL,
    vo2max REAL,
    vo2max_date TEXT,
    recommendation_type TEXT,
    recommendation_detail TEXT,
    updated_at TEXT
);
"""

# Instance-level settings that aren't per-day data: the dashboard password
# chosen on the setup page, and whether the stored Garmin session still
# works. These live in the database rather than the .env because the whole
# point of the setup page is that the person running the server never types
# - or sees - the values.
SETTINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);
"""

# Columns added after the initial release - added via ALTER TABLE for
# existing databases, since CREATE TABLE IF NOT EXISTS won't retrofit them.
MIGRATIONS = [
    ("readiness_source", "TEXT"),
    ("target_strain", "REAL"),
    ("sleep_recommendation_hours", "REAL"),
    ("body_battery", "REAL"),
    ("body_battery_charged", "REAL"),
    ("body_battery_drained", "REAL"),
    ("stress_avg", "REAL"),
    ("acwr_percent", "REAL"),
    ("acwr_feedback", "TEXT"),
    ("acute_load", "REAL"),
    ("vo2max", "REAL"),
    ("vo2max_date", "TEXT"),
    ("sync_errors", "TEXT"),
    ("respiration_avg", "REAL"),
    ("spo2_avg", "REAL"),
    ("spo2_baseline", "REAL"),
    ("resting_hr_baseline", "REAL"),
    ("hrv_balanced_low", "REAL"),
    ("hrv_balanced_high", "REAL"),
    ("sleep_need_minutes", "REAL"),
    ("watch_synced_at", "TEXT"),
    ("stress_latest", "REAL"),
    ("stress_latest_at", "TEXT"),
    ("stress_max", "REAL"),
    ("hr_trimp", "REAL"),
    ("manual_trimp", "REAL"),
    ("off_wrist_gaps", "TEXT"),
]

# Sessions the watch never saw, logged by hand with a perceived effort.
# gap_start ties an entry to the off-wrist reminder it answered.
MANUAL_ACTIVITIES_SCHEMA = """
CREATE TABLE IF NOT EXISTS manual_activities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    sport TEXT NOT NULL,
    minutes REAL NOT NULL,
    rpe INTEGER NOT NULL,
    gap_start TEXT,
    created_at TEXT NOT NULL
);
"""

# Off-wrist reminders answered with "not a workout" - charging, the shower.
DISMISSED_GAPS_SCHEMA = """
CREATE TABLE IF NOT EXISTS dismissed_gaps (
    gap_start TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    dismissed_at TEXT NOT NULL
);
"""


def get_conn(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    conn.execute(SETTINGS_SCHEMA)
    conn.execute(MANUAL_ACTIVITIES_SCHEMA)
    conn.execute(DISMISSED_GAPS_SCHEMA)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(daily)")}
    for column, coltype in MIGRATIONS:
        if column not in existing:
            conn.execute(f"ALTER TABLE daily ADD COLUMN {column} {coltype}")
    conn.commit()
    return conn


def upsert_day(conn, date_str, fields):
    columns = ["date"] + list(fields.keys())
    placeholders = ",".join("?" for _ in columns)
    updates = ",".join(f"{k}=excluded.{k}" for k in fields.keys())
    values = [date_str] + list(fields.values())
    conn.execute(
        f"INSERT INTO daily ({','.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(date) DO UPDATE SET {updates}",
        values,
    )
    conn.commit()


def get_day(conn, date_str):
    row = conn.execute("SELECT * FROM daily WHERE date = ?", (date_str,)).fetchone()
    return dict(row) if row else None


def get_recent_days(conn, before_date_str, limit=60):
    rows = conn.execute(
        "SELECT * FROM daily WHERE date < ? ORDER BY date DESC LIMIT ?",
        (before_date_str, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()


def add_manual_activity(conn, date_str, sport, minutes, rpe, gap_start=None):
    conn.execute(
        "INSERT INTO manual_activities (date, sport, minutes, rpe, gap_start, created_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now'))",
        (date_str, sport, minutes, rpe, gap_start),
    )
    conn.commit()


def get_manual_activities(conn, date_strs):
    marks = ",".join("?" for _ in date_strs)
    rows = conn.execute(
        f"SELECT * FROM manual_activities WHERE date IN ({marks}) ORDER BY date DESC, id",
        list(date_strs),
    ).fetchall()
    return [dict(r) for r in rows]


def delete_manual_activity(conn, activity_id):
    """Delete and return the removed row's date, or None if it didn't exist."""
    row = conn.execute("SELECT date FROM manual_activities WHERE id = ?", (activity_id,)).fetchone()
    if not row:
        return None
    conn.execute("DELETE FROM manual_activities WHERE id = ?", (activity_id,))
    conn.commit()
    return row["date"]


def dismiss_gap(conn, date_str, gap_start):
    conn.execute(
        "INSERT OR IGNORE INTO dismissed_gaps (gap_start, date, dismissed_at) VALUES (?, ?, datetime('now'))",
        (gap_start, date_str),
    )
    conn.commit()


def get_dismissed_gap_starts(conn, date_strs):
    marks = ",".join("?" for _ in date_strs)
    rows = conn.execute(
        f"SELECT gap_start FROM dismissed_gaps WHERE date IN ({marks})", list(date_strs)
    ).fetchall()
    return {r["gap_start"] for r in rows}
