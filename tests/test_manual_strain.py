"""Sessions the watch never saw: off-wrist gap detection, the RPE-to-TRIMP
conversion, and the dashboard's log / dismiss / remove forms.

The properties worth pinning: a logged session lands on the same TRIMP
scale as recorded heart rate, reminders disappear once answered, and the
forms refuse anything without a login session and its CSRF token.
"""

import json
import os
import re
from datetime import date, datetime, timedelta, timezone

os.environ.setdefault("GARMIN_EMAIL", "test@example.com")
os.environ.setdefault("GARMIN_PASSWORD", "test")

import pytest

import app as app_module
import config
import scoring
import storage
from garmin_client import extract_off_wrist_gaps

BASE = int(datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc).timestamp() * 1000)


def at(minutes):
    return BASE + minutes * 60000


def hr(entries):
    return {"heartRateValues": [[at(m), v] for m, v in entries]}


# ---- gap detection ----------------------------------------------------

def test_gap_from_missing_samples():
    gaps = extract_off_wrist_gaps(hr([(0, 70), (2, 72), (50, 80), (52, 81)]), 30)
    assert [g["minutes"] for g in gaps] == [48]
    assert gaps[0]["start"] == datetime.fromtimestamp(at(2) / 1000, tz=timezone.utc).isoformat()


def test_gap_from_explicit_nulls():
    entries = [(0, 70)] + [(m, None) for m in range(2, 40, 2)] + [(40, 75)]
    assert [g["minutes"] for g in extract_off_wrist_gaps(hr(entries), 30)] == [40]


def test_short_gaps_are_ignored():
    assert extract_off_wrist_gaps(hr([(0, 70), (20, 72)]), 30) == []


def test_leading_and_trailing_null_runs_count():
    entries = [(0, None), (60, 70), (62, 71), (100, None)]
    assert [g["minutes"] for g in extract_off_wrist_gaps(hr(entries), 30)] == [60, 38]


def test_open_end_after_last_upload_is_not_a_gap():
    # Nothing after the last reading just means the watch hasn't synced yet.
    assert extract_off_wrist_gaps(hr([(0, 70), (2, 71)]), 30) == []


def test_no_data_means_no_gaps():
    assert extract_off_wrist_gaps({}, 30) == []
    assert extract_off_wrist_gaps(None, 30) == []


# ---- RPE to TRIMP -----------------------------------------------------

def test_logged_session_matches_recorded_hr_at_the_same_intensity():
    # 70% of heart-rate reserve recorded for 60 minutes should score the
    # same as 60 minutes logged at the effort mapped to 70%.
    resting, max_hr = 50, 190
    recorded = scoring.compute_trimp([(resting + 0.70 * (max_hr - resting), 60)], resting, max_hr)
    assert scoring.RPE_TO_HRR[7] == 0.70
    assert scoring.manual_trimp(60, 7) == pytest.approx(recorded, abs=0.2)


def test_manual_trimp_rises_with_effort_and_duration():
    by_effort = [scoring.manual_trimp(60, rpe) for rpe in range(1, 11)]
    assert by_effort == sorted(by_effort) and len(set(by_effort)) == 10
    assert scoring.manual_trimp(90, 6) > scoring.manual_trimp(45, 6)


@pytest.mark.parametrize("minutes, rpe", [(0, 5), (-10, 5), (60, 0), (60, 11), (None, 5)])
def test_manual_trimp_rejects_nonsense(minutes, rpe):
    assert scoring.manual_trimp(minutes, rpe) == 0.0


def test_day_strain_adds_logged_sessions():
    base_total, base_strain = scoring.day_strain(40.0, 0.0, [])
    total, strain = scoring.day_strain(40.0, 60.0, [])
    assert (base_total, total) == (40.0, 100.0)
    assert strain > base_strain


def test_day_strain_stays_unknown_without_hr():
    assert scoring.day_strain(None, 60.0, []) == (None, None)


# ---- dashboard forms --------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DASHBOARD_USER", "tester")
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD", "secret")
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    app_module._failures.clear()
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


GAP_START = "2026-09-30T16:00:00+00:00"


@pytest.fixture
def day_with_gap(client):
    conn = storage.get_conn(config.DB_PATH)
    gaps = [{"start": GAP_START, "end": "2026-09-30T17:30:00+00:00", "minutes": 90}]
    storage.upsert_day(conn, date.today().isoformat(), {
        "trimp": 30.0, "hr_trimp": 30.0, "manual_trimp": 0.0, "strain_score": 28.4,
        "off_wrist_gaps": json.dumps(gaps),
    })
    return conn


def login(client):
    client.post("/login", data={"username": "tester", "password": "secret"})
    page = client.get("/").get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', page).group(1), page


def log_form(csrf, **extra):
    data = {"csrf": csrf, "date": date.today().isoformat(), "gap_start": GAP_START,
            "sport": "Basketball", "minutes": "90", "rpe": "7"}
    data.update(extra)
    return data


def today_row(conn):
    return storage.get_day(conn, date.today().isoformat())


def test_reminder_shows_with_prefilled_duration(client, day_with_gap):
    _, page = login(client)
    assert "No watch" in page and "1h30m" in page
    assert 'value="90"' in page


def test_logging_against_a_gap_raises_strain_and_clears_the_reminder(client, day_with_gap):
    csrf, _ = login(client)
    resp = client.post("/activities", data=log_form(csrf))
    assert resp.status_code == 303

    row = today_row(day_with_gap)
    assert row["manual_trimp"] == scoring.manual_trimp(90, 7)
    assert row["trimp"] == pytest.approx(30.0 + row["manual_trimp"])
    assert row["strain_score"] > 28.4

    page = client.get("/").get_data(as_text=True)
    assert "No watch" not in page
    assert "Basketball" in page


def test_removing_a_session_restores_strain(client, day_with_gap):
    csrf, _ = login(client)
    client.post("/activities", data=log_form(csrf))
    activity_id = storage.get_manual_activities(day_with_gap, [date.today().isoformat()])[0]["id"]

    client.post(f"/activities/{activity_id}/delete", data={"csrf": csrf})
    row = today_row(day_with_gap)
    assert row["trimp"] == 30.0 and row["manual_trimp"] == 0.0
    assert "No watch" in client.get("/").get_data(as_text=True)


def test_dismissing_hides_the_reminder_without_touching_strain(client, day_with_gap):
    csrf, _ = login(client)
    client.post("/gaps/dismiss", data={"csrf": csrf, "date": date.today().isoformat(),
                                       "gap_start": GAP_START})
    assert "No watch" not in client.get("/").get_data(as_text=True)
    assert today_row(day_with_gap)["trimp"] == 30.0


def test_session_cannot_outlast_its_gap(client, day_with_gap):
    csrf, _ = login(client)
    assert client.post("/activities", data=log_form(csrf, minutes="91")).status_code == 400
    assert storage.get_manual_activities(day_with_gap, [date.today().isoformat()]) == []


@pytest.mark.parametrize("field, value", [("rpe", "0"), ("rpe", "11"), ("minutes", "abc"),
                                          ("date", "2020-01-01"), ("gap_start", "nope")])
def test_invalid_log_is_refused(client, day_with_gap, field, value):
    csrf, _ = login(client)
    assert client.post("/activities", data=log_form(csrf, **{field: value})).status_code == 400


def test_session_without_a_gap_can_be_logged_for_today(client, day_with_gap):
    csrf, _ = login(client)
    resp = client.post("/activities", data=log_form(csrf, gap_start="", minutes="45", rpe="5"))
    assert resp.status_code == 303
    assert "No watch" in client.get("/").get_data(as_text=True)  # the gap is still unanswered


def test_forms_refuse_a_missing_csrf_token(client, day_with_gap):
    login(client)
    data = log_form("")
    del data["csrf"]
    assert client.post("/activities", data=data).status_code == 400
    assert storage.get_manual_activities(day_with_gap, [date.today().isoformat()]) == []


def test_forms_send_anonymous_visitors_to_login(client, day_with_gap):
    resp = client.post("/activities", data=log_form("anything"))
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]
    assert storage.get_manual_activities(day_with_gap, [date.today().isoformat()]) == []


def test_demo_page_shows_no_reminders_or_forms(client, day_with_gap):
    page = client.get("/").get_data(as_text=True)
    assert "No watch" not in page and "Without the watch" not in page


def test_yesterdays_gap_is_offered_and_logs_to_yesterday(client, day_with_gap):
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    gap = {"start": "2026-09-29T19:00:00+00:00", "end": "2026-09-29T20:00:00+00:00", "minutes": 60}
    storage.upsert_day(day_with_gap, yesterday, {"trimp": 50.0, "off_wrist_gaps": json.dumps([gap])})

    csrf, page = login(client)
    assert "Yesterday" in page
    client.post("/activities", data=log_form(csrf, date=yesterday, gap_start=gap["start"], minutes="60"))
    row = storage.get_day(day_with_gap, yesterday)
    # A row synced before logging existed has no hr_trimp; its trimp was all HR.
    assert row["hr_trimp"] == 50.0
    assert row["trimp"] == pytest.approx(50.0 + scoring.manual_trimp(60, 7))
