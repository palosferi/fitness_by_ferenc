"""A day with no readings must render as "no data", not as data.

Garmin reports "couldn't measure" as -1, which the stress gauge used to show
as a stress of -1 labelled "Rest"; and with nothing to score, the Health
Monitor tile silently vanished, which looks like a layout bug.
"""

import base64
import os
from datetime import datetime

os.environ.setdefault("GARMIN_EMAIL", "test@example.com")
os.environ.setdefault("GARMIN_PASSWORD", "test")

import pytest

import app as app_module
import config


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


def test_negative_stress_is_no_reading():
    assert app_module.stress_reading(-1) is None
    assert app_module.stress_reading(-2) is None
    assert app_module.stress_reading(0)["label"] == "Rest"


def test_extract_stress_drops_the_sentinel():
    pytest.importorskip("garminconnect")
    import garmin_client
    result = garmin_client.extract_stress(
        {"avgStressLevel": -1, "maxStressLevel": -1, "stressValuesArray": [[0, -1], [1, -2]]})
    assert result == {"avg": None, "max": None, "latest": None, "latest_at": None}


def test_health_summary_with_nothing_to_show():
    summary = app_module.health_summary([])
    assert summary["status"] == "unknown"
    assert summary["status_text"] == "No Data"


def test_health_summary_with_nothing_scored():
    summary = app_module.health_summary([{"label": "VO2 max", "status": "unknown"}])
    assert summary["status"] == "unknown"
    assert summary["count_text"] == "0/1 tracked"


def test_empty_day_keeps_both_tiles(client, monkeypatch):
    html = render(client, monkeypatch, {
        "stress_avg": -1, "stress_max": -1,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    })
    assert 'href="#health-detail"' in html
    assert "Stress<br>Monitor" in html
    assert 'id="stress-detail"' not in html
    assert ">-1<" not in html
