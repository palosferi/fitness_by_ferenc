"""The recommendation card shows a 0-10 effort level rather than a run.

Rows synced before the scale existed carry only the old run prescription
and no level, so they must fall back to showing that text as-is.
"""

import base64
import os
import re

os.environ.setdefault("GARMIN_EMAIL", "test@example.com")
os.environ.setdefault("GARMIN_PASSWORD", "test")

import pytest

import app as app_module
import config
import scoring


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DASHBOARD_USER", "tester")
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD", "secret")
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    app_module._failures.clear()
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def render(client, monkeypatch, day):
    monkeypatch.setattr(app_module.storage, "get_day", lambda conn, d: day)
    token = base64.b64encode(b"tester:secret").decode()
    return client.get("/", headers={"Authorization": f"Basic {token}"}).get_data(as_text=True)


def test_card_shows_the_level_and_advice_without_repeating_it(client, monkeypatch):
    rec = scoring.recommend_training(readiness=80, yesterday_strain=90)
    page = render(client, monkeypatch, {"recommended_effort": rec["effort"],
                                        "recommendation_detail": rec["detail"],
                                        "recommendation_type": rec["activity"]})
    assert "Effort today" in page
    assert f'<span class="effort-value">{rec["effort"]}</span>' in page
    assert len(re.findall(r'<span class="on">', page)) == rec["effort"]
    assert "Eased off after yesterday" in page
    assert "Effort 6/10," not in page  # the prefix is stripped, the number stands alone


def test_rest_day_fills_no_segments(client, monkeypatch):
    rec = scoring.recommend_training(readiness=20, yesterday_strain=None)
    page = render(client, monkeypatch, {"recommended_effort": 0, "recommendation_detail": rec["detail"]})
    assert '<span class="effort-label">rest</span>' in page
    assert '<span class="on">' not in page


def test_old_rows_fall_back_to_their_text(client, monkeypatch):
    page = render(client, monkeypatch, {"recommendation_type": "run",
                                        "recommendation_detail": "Moderate run: 7.5 km / 45 min."})
    assert "Effort today" not in page
    assert "Moderate run: 7.5 km / 45 min." in page


def test_log_card_is_last_on_the_page(client, monkeypatch):
    page = render(client, monkeypatch, {"steps": 1000, "recommended_effort": 5})
    assert page.index("Effort today") < page.index('class="stats"') < page.index("Without the watch")
