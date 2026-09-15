import sqlite3

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
]


def get_conn(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
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
