"""Tests for the demo/auth split.

The security-critical property here is that real Garmin data is NEVER served
to an unauthenticated request - the public dashboard exists precisely so the
project can be shown off without exposing personal health data.
"""

import base64
import os
from datetime import datetime

os.environ.setdefault("GARMIN_EMAIL", "test@example.com")
os.environ.setdefault("GARMIN_PASSWORD", "test")

import pytest

import app as app_module
import config
import demo


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DASHBOARD_USER", "tester")
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD", "secret")
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def auth_header(user, password):
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


# ---- demo generator --------------------------------------------------

def test_demo_day_has_same_shape_as_a_stored_row():
    day = demo.generate_day()
    for key in ("strain_score", "sleep_score", "readiness_score", "target_strain",
                "recommendation_type", "recommendation_detail", "updated_at", "steps"):
        assert key in day


def test_demo_scores_are_in_range():
    day = demo.generate_day()
    for key in ("strain_score", "sleep_score", "readiness_score", "target_strain"):
        assert 0 <= day[key] <= 100


def test_demo_is_stable_within_a_day_but_varies_across_days():
    a = demo.generate_day(datetime(2026, 3, 1, 9, 0))
    b = demo.generate_day(datetime(2026, 3, 1, 18, 0))
    c = demo.generate_day(datetime(2026, 3, 2, 9, 0))
    assert a["strain_score"] == b["strain_score"]
    assert (a["strain_score"], a["sleep_score"]) != (c["strain_score"], c["sleep_score"])


# ---- anonymous access gets demo, never real data ----------------------

def test_api_anonymous_gets_demo_flagged_payload(client):
    resp = client.get("/api/today")
    assert resp.status_code == 200
    assert resp.get_json()["demo"] is True


def test_api_anonymous_never_reads_the_real_database(client, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("anonymous request touched the real database")

    monkeypatch.setattr(app_module.storage, "get_day", explode)
    assert client.get("/api/today").status_code == 200
    assert client.get("/").status_code == 200


def test_dashboard_anonymous_shows_demo_banner(client):
    body = client.get("/").get_data(as_text=True)
    assert "Demo data" in body


def test_wrong_credentials_still_get_demo(client):
    resp = client.get("/api/today", headers=auth_header("tester", "wrong"))
    assert resp.get_json()["demo"] is True


def test_no_password_configured_means_nobody_is_authenticated(client, monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD", None)
    resp = client.get("/api/today", headers=auth_header("tester", "secret"))
    assert resp.get_json()["demo"] is True


# ---- authenticated access gets real data ------------------------------

def test_authenticated_request_reads_real_data(client, monkeypatch):
    monkeypatch.setattr(
        app_module.storage, "get_day",
        lambda conn, day: {"strain_score": 42.0, "updated_at": datetime.now().isoformat(timespec="seconds")},
    )
    payload = client.get("/api/today", headers=auth_header("tester", "secret")).get_json()
    assert payload["demo"] is False
    assert payload["strain_score"] == 42.0


# ---- login route ------------------------------------------------------

def test_login_challenges_anonymous_visitors(client):
    resp = client.get("/login")
    assert resp.status_code == 401
    assert "Basic" in resp.headers["WWW-Authenticate"]


def test_login_redirects_once_authenticated(client):
    resp = client.get("/login", headers=auth_header("tester", "secret"))
    assert resp.status_code == 302
