"""Tests for derived-baseline sample gating and the Health Monitor icon.

Both guard the same failure: a metric judged against a baseline drawn from
one or two nights. The number renders identically to a well-founded one, so
nothing on screen says "this verdict is thin" - which is how a two-sample
respiratory baseline came to drive a red alert.
"""

import os
import re

os.environ.setdefault("GARMIN_EMAIL", "test@example.com")
os.environ.setdefault("GARMIN_PASSWORD", "test")

import pytest

import app as app_module
import config


@pytest.fixture
def client(monkeypatch, tmp_path):
    """Unauthenticated client - serves the demo day, which carries baselines
    for every health row, so the tile always renders."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def rows(field, values):
    """History rows as storage hands them over - dicts, some missing the key."""
    return [{field: v} for v in values]


# ---- derived baseline gating -----------------------------------------

def test_mean_of_returns_none_below_minimum(monkeypatch):
    monkeypatch.setattr(config, "MIN_BASELINE_SAMPLES", 5)
    assert app_module._mean_of(rows("respiration_avg", [12.0, 14.0]), "respiration_avg") is None


def test_mean_of_returns_mean_at_minimum(monkeypatch):
    monkeypatch.setattr(config, "MIN_BASELINE_SAMPLES", 5)
    result = app_module._mean_of(rows("respiration_avg", [12, 13, 14, 15, 16]), "respiration_avg")
    assert result == pytest.approx(14.0)


def test_mean_of_counts_only_rows_carrying_the_field(monkeypatch):
    """Seven rows, three usable - a gap-filled week must not pass as a week."""
    monkeypatch.setattr(config, "MIN_BASELINE_SAMPLES", 5)
    history = rows("respiration_avg", [12.0, None, 14.0, None, 13.0, None, None])
    assert app_module._mean_of(history, "respiration_avg") is None


def test_sync_derived_baseline_applies_the_same_rule(monkeypatch):
    # sync imports garmin_client, which needs the garminconnect package - it is
    # installed on the server but not necessarily on a dev checkout.
    pytest.importorskip("garminconnect")
    import sync
    monkeypatch.setattr(config, "MIN_BASELINE_SAMPLES", 5)
    assert sync._derived_baseline(rows("resting_hr", [50, 52]), "resting_hr") is None
    assert sync._derived_baseline(
        rows("resting_hr", [50, 52, 54, 56, 58]), "resting_hr") == pytest.approx(54.0)


# ---- an ungrounded metric is shown but not scored --------------------

def test_metric_without_baseline_still_shows_its_value():
    entry = app_module.health_metric("Respiratory rate", 16.0, "rpm", None, "lungs",
                                     higher_is_better=False, precision=1)
    assert entry["value"] == "16.0 rpm"
    assert entry["status"] == "unknown"
    assert entry["delta"] is None


def test_summary_counts_ungrounded_metrics_separately():
    metrics = [
        {"label": "a", "status": "good"},
        {"label": "b", "status": "alert"},
        {"label": "c", "status": "unknown"},
    ]
    summary = app_module.health_summary(metrics)
    assert summary["count_text"] == "1/2 tracked"
    assert "1 without a baseline yet" in summary["detail_text"]


# ---- the tile icon must agree with the tile's verdict ----------------

CHECK_PATH = "m5 13"      # check.svg
ALERT_PATH = "M12 6v8"    # alert.svg


def test_health_tile_icon_matches_its_status(client):
    """A tick next to "Out of Range" reads as a pass however it is coloured."""
    html = client.get("/").get_data(as_text=True)
    spans = re.findall(r'<span class="tile-check (\w+)">(.*?)</span>', html, re.S)
    assert spans, "health tile did not render"
    for status, inner in spans:
        if status == "good":
            assert CHECK_PATH in inner and ALERT_PATH not in inner
        else:
            assert ALERT_PATH in inner and CHECK_PATH not in inner
