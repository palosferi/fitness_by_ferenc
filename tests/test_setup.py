"""Tests for handing an instance to someone else.

The property worth protecting here is that whoever runs the server never has
to be told the account holder's Garmin password: it arrives in a form, is
exchanged for tokens, and is never written down. The setup page is therefore
also the one route that can create an account out of nothing, so most of
these tests are about who is allowed to reach it.
"""

import importlib
import os
import re
from datetime import datetime

os.environ.setdefault("GARMIN_EMAIL", "test@example.com")
os.environ.setdefault("GARMIN_PASSWORD", "test")

import pytest

import app as app_module
import config
import storage


@pytest.fixture
def unclaimed(monkeypatch, tmp_path):
    """A fresh instance: no password in the .env, nothing in the database."""
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD", None)
    monkeypatch.setattr(config, "DASHBOARD_SECRET_KEY", None)
    monkeypatch.setattr(config, "DASHBOARD_USER", "ferenc")
    monkeypatch.setattr(config, "SETUP_TOKEN", "letmein")
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "SETUP_LINK_TIMEOUT_SECONDS", 5)

    original_key = app_module.app.secret_key
    app_module._failures.clear()
    app_module._pending_links.clear()
    app_module.app.config["TESTING"] = True
    yield app_module.app.test_client()
    app_module.app.secret_key = original_key


def stored_real_day(monkeypatch):
    monkeypatch.setattr(
        app_module.storage, "get_day",
        lambda conn, day: {"strain_score": 42.0,
                           "updated_at": datetime.now().isoformat(timespec="seconds")},
    )


def pending_id(html):
    match = re.search(r'name="pending" value="([^"]+)"', html)
    assert match, "MFA form did not carry a pending id"
    return match.group(1)


# ---- settings store ----------------------------------------------------

def test_settings_round_trip(tmp_path):
    conn = storage.get_conn(str(tmp_path / "s.db"))
    assert storage.get_setting(conn, "missing") is None
    assert storage.get_setting(conn, "missing", "fallback") == "fallback"
    storage.set_setting(conn, "garmin_link_state", "linked")
    assert storage.get_setting(conn, "garmin_link_state") == "linked"
    storage.set_setting(conn, "garmin_link_state", "needs_login")
    assert storage.get_setting(conn, "garmin_link_state") == "needs_login"


# ---- who may reach the setup page -------------------------------------

def test_setup_is_hidden_without_the_token(unclaimed):
    assert unclaimed.get("/setup").status_code == 404


def test_setup_is_hidden_when_no_token_is_configured(unclaimed, monkeypatch):
    monkeypatch.setattr(config, "SETUP_TOKEN", None)
    assert unclaimed.get("/setup?token=letmein").status_code == 404


def test_setup_opens_with_the_token(unclaimed):
    resp = unclaimed.get("/setup?token=letmein")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'name="garmin_password"' in body
    # An unclaimed instance also asks them to choose a dashboard password.
    assert 'name="dashboard_password"' in body


def test_a_claimed_instance_stops_asking_for_a_dashboard_password(unclaimed, monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD", "already-set")
    body = unclaimed.get("/setup?token=letmein").get_data(as_text=True)
    assert 'name="dashboard_password"' not in body
    assert 'name="garmin_password"' in body


# ---- linking an account ------------------------------------------------

def test_setup_stores_the_account_and_signs_them_in(unclaimed, monkeypatch):
    seen = {}

    def fake_link(email, password, prompt_mfa):
        seen["credentials"] = (email, password)

    monkeypatch.setattr(app_module, "_link_account", fake_link)
    stored_real_day(monkeypatch)

    resp = unclaimed.post("/setup", data={
        "token": "letmein",
        "garmin_email": "friend@example.com",
        "garmin_password": "garmin-secret",
        "dashboard_user": "friend",
        "dashboard_password": "dashboard-secret",
    })
    assert resp.status_code == 302
    assert seen["credentials"] == ("friend@example.com", "garmin-secret")

    # The redirect carries a session that unlocks real data.
    assert unclaimed.get("/api/today").get_json()["demo"] is False


def test_the_chosen_password_works_and_the_garmin_one_is_not_kept(unclaimed, monkeypatch):
    monkeypatch.setattr(app_module, "_link_account", lambda *a, **k: None)
    stored_real_day(monkeypatch)

    unclaimed.post("/setup", data={
        "token": "letmein",
        "garmin_email": "friend@example.com",
        "garmin_password": "garmin-secret",
        "dashboard_user": "friend",
        "dashboard_password": "dashboard-secret",
    })

    conn = storage.get_conn(config.DB_PATH)
    assert storage.get_setting(conn, "dashboard_user") == "friend"
    assert storage.get_setting(conn, "garmin_link_state") == "linked"

    # Stored as a hash, and the Garmin password appears nowhere at all.
    stored = storage.get_setting(conn, "dashboard_password_hash")
    assert "dashboard-secret" not in stored
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    assert not any("garmin-secret" in (r["value"] or "") for r in rows)

    # And the chosen password is what signs them in from a new session.
    unclaimed.post("/logout")
    assert unclaimed.post("/login", data={"username": "friend",
                                          "password": "dashboard-secret"}).status_code == 302


def test_stored_password_is_used_even_when_the_env_has_one(unclaimed, monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD", "from-the-env")
    monkeypatch.setattr(config, "DASHBOARD_USER", "ferenc")
    conn = storage.get_conn(config.DB_PATH)
    storage.set_setting(conn, "dashboard_user", "friend")
    storage.set_setting(conn, "dashboard_password_hash",
                        app_module.generate_password_hash("chosen"))

    assert unclaimed.post("/login", data={"username": "friend", "password": "chosen"}).status_code == 302

    # Signed in, so /login would redirect on its own - drop the session first
    # or the next assertion proves nothing.
    unclaimed.post("/logout")
    assert unclaimed.post("/login", data={"username": "ferenc",
                                          "password": "from-the-env"}).status_code == 401


def test_a_refused_login_leaves_the_instance_unclaimed(unclaimed, monkeypatch):
    def refuse(email, password, prompt_mfa):
        raise RuntimeError("401 from Garmin")

    monkeypatch.setattr(app_module, "_link_account", refuse)

    resp = unclaimed.post("/setup", data={
        "token": "letmein",
        "garmin_email": "friend@example.com",
        "garmin_password": "wrong",
        "dashboard_user": "friend",
        "dashboard_password": "dashboard-secret",
    })
    assert resp.status_code == 400
    assert "did not accept" in resp.get_data(as_text=True)

    conn = storage.get_conn(config.DB_PATH)
    assert storage.get_setting(conn, "dashboard_password_hash") is None
    assert unclaimed.get("/api/today").get_json()["demo"] is True


def test_missing_fields_do_not_start_a_login(unclaimed, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("tried to reach Garmin with an incomplete form")

    monkeypatch.setattr(app_module, "_link_account", explode)
    resp = unclaimed.post("/setup", data={"token": "letmein", "garmin_email": "a@b.c"})
    assert resp.status_code == 400
    assert "Choose a username and password" in resp.get_data(as_text=True)


# ---- the MFA hop -------------------------------------------------------

def test_mfa_code_is_collected_by_a_second_form(unclaimed, monkeypatch):
    def fake_link(email, password, prompt_mfa):
        if prompt_mfa() != "123456":
            raise RuntimeError("bad code")

    monkeypatch.setattr(app_module, "_link_account", fake_link)
    stored_real_day(monkeypatch)

    resp = unclaimed.post("/setup", data={
        "token": "letmein",
        "garmin_email": "friend@example.com",
        "garmin_password": "garmin-secret",
        "dashboard_user": "friend",
        "dashboard_password": "dashboard-secret",
    })
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'name="code"' in body

    done = unclaimed.post("/setup/mfa", data={
        "token": "letmein", "pending": pending_id(body), "code": "123456",
    })
    assert done.status_code == 302
    assert unclaimed.get("/api/today").get_json()["demo"] is False


def test_an_expired_mfa_attempt_sends_them_back_to_the_start(unclaimed):
    resp = unclaimed.post("/setup/mfa", data={
        "token": "letmein", "pending": "no-such-attempt", "code": "123456",
    })
    assert resp.status_code == 400
    assert "expired" in resp.get_data(as_text=True)


def test_mfa_form_is_also_hidden_without_the_token(unclaimed):
    assert unclaimed.post("/setup/mfa", data={"pending": "x", "code": "1"}).status_code == 404


# ---- an expired Garmin session -----------------------------------------

def test_dashboard_says_so_when_the_garmin_session_has_expired(unclaimed, monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD", "secret")
    monkeypatch.setattr(config, "DASHBOARD_USER", "tester")
    stored_real_day(monkeypatch)
    storage.set_setting(storage.get_conn(config.DB_PATH), "garmin_link_state", "needs_login")

    unclaimed.post("/login", data={"username": "tester", "password": "secret"})
    assert "sign-in has expired" in unclaimed.get("/").get_data(as_text=True)


def test_the_demo_dashboard_never_mentions_an_expired_session(unclaimed):
    storage.set_setting(storage.get_conn(config.DB_PATH), "garmin_link_state", "needs_login")
    assert "sign-in has expired" not in unclaimed.get("/").get_data(as_text=True)


# ---- cookie identity ---------------------------------------------------

def test_session_cookie_is_named_for_this_instance():
    # Flask's default name would collide with a second instance on the same
    # hostname - see the comment in config.py.
    assert app_module.app.config["SESSION_COOKIE_NAME"] == config.SESSION_COOKIE_NAME
    assert config.SESSION_COOKIE_NAME != "session"


def test_a_prefixed_instance_gets_its_own_cookie_name_and_path(monkeypatch):
    monkeypatch.setenv("URL_PREFIX", "/dad")
    monkeypatch.delenv("SESSION_COOKIE_NAME", raising=False)
    monkeypatch.delenv("SESSION_COOKIE_PATH", raising=False)
    try:
        reloaded = importlib.reload(config)
        assert reloaded.SESSION_COOKIE_NAME == "fbf_session_dad"
        assert reloaded.SESSION_COOKIE_PATH == "/dad"
    finally:
        monkeypatch.delenv("URL_PREFIX", raising=False)
        importlib.reload(config)
