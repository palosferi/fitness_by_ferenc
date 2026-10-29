"""The WHOOP export loader (analysis/whoop.py), on a synthetic export.

The properties worth pinning: a cycle is dated by the morning its sleep
ended, sleep metrics come from sleeps.csv rather than the cycles file's
copy (which can carry a nap's), and workouts and naps land on their cycle.
"""

import pytest

pd = pytest.importorskip("pandas")

from analysis.whoop import load_whoop, workout_dates  # noqa: E402

CYCLES = """\
Cycle start time,Cycle end time,Cycle timezone,Recovery score %,Resting heart rate (bpm),Heart rate variability (ms),Skin temp (celsius),Blood oxygen %,Day Strain,Energy burned (cal),Max HR (bpm),Average HR (bpm),Sleep onset,Wake onset,Sleep performance %,Respiratory rate (rpm),Asleep duration (min),In bed duration (min),Light sleep duration (min),Deep (SWS) duration (min),REM duration (min),Awake duration (min),Sleep need (min),Sleep debt (min),Sleep efficiency %,Sleep consistency %
2026-02-01 23:30:00,,UTC+01:00,60,55,30,34.0,96.0,,,,,2026-02-01 23:30:00,2026-02-02 07:30:00,90,14.0,450,480,200,120,130,30,500,20,94,80
2026-01-31 23:00:00,2026-02-01 23:30:00,UTC+01:00,40,60,22,34.1,95.0,12.5,2500,180,75,2026-02-01 15:00:00,2026-02-01 16:00:00,50,15.0,55,60,40,10,5,5,520,40,92,70
"""

SLEEPS = """\
Cycle start time,Cycle end time,Cycle timezone,Sleep onset,Wake onset,Sleep performance %,Respiratory rate (rpm),Asleep duration (min),In bed duration (min),Light sleep duration (min),Deep (SWS) duration (min),REM duration (min),Awake duration (min),Sleep need (min),Sleep debt (min),Sleep efficiency %,Sleep consistency %,Nap
2026-02-01 23:30:00,,UTC+01:00,2026-02-01 23:30:00,2026-02-02 07:30:00,90,14.0,450,480,200,120,130,30,500,20,94,80,false
2026-01-31 23:00:00,2026-02-01 23:30:00,UTC+01:00,2026-02-01 15:00:00,2026-02-01 16:00:00,50,15.0,55,60,40,10,5,5,520,40,92,70,true
2026-01-31 23:00:00,2026-02-01 23:30:00,UTC+01:00,2026-01-31 23:00:00,2026-02-01 08:00:00,85,13.5,510,540,250,130,130,30,520,40,94,75,false
"""

WORKOUTS = """\
Cycle start time,Cycle end time,Cycle timezone,Workout start time,Workout end time,Duration (min),Activity name,Activity Strain,Energy burned (cal),Max HR (bpm),Average HR (bpm),HR Zone 1 %,HR Zone 2 %,HR Zone 3 %,HR Zone 4 %,HR Zone 5 %,GPS enabled
2026-01-31 23:00:00,2026-02-01 23:30:00,UTC+01:00,2026-02-01 10:00:00,2026-02-01 10:40:00,40,Running,11.0,500,180,150,10,20,30,30,10,false
2026-01-31 23:00:00,2026-02-01 23:30:00,UTC+01:00,2026-02-01 18:00:00,2026-02-01 18:20:00,20,Activity,5.0,150,130,110,60,10,0,0,0,false
"""


@pytest.fixture
def export(tmp_path):
    (tmp_path / "physiological_cycles.csv").write_text(CYCLES)
    (tmp_path / "sleeps.csv").write_text(SLEEPS)
    (tmp_path / "workouts.csv").write_text(WORKOUTS)
    return tmp_path


def test_cycles_are_dated_by_wake_morning(export):
    days = load_whoop(export)
    assert [d.strftime("%Y-%m-%d") for d in days.index] == ["2026-02-01", "2026-02-02"]
    assert days.loc["2026-02-01", "day_strain"] == 12.5
    assert days.loc["2026-02-01", "rhr"] == 60


def test_sleep_comes_from_the_night_not_the_nap(export):
    # The cycles file holds the nap's 55 minutes for this cycle.
    day = load_whoop(export).loc["2026-02-01"]
    assert day["sleep_min"] == 510
    assert day["resp_rate"] == 13.5
    assert day["nap_min"] == 55


def test_open_cycle_keeps_its_sleep_without_strain(export):
    day = load_whoop(export).loc["2026-02-02"]
    assert day["sleep_min"] == 450
    assert pd.isna(day["day_strain"])
    assert day["workout_count"] == 0 and day["nap_min"] == 0


def test_workouts_land_on_their_cycle(export):
    days = load_whoop(export)
    assert days.loc["2026-02-01", "workout_count"] == 2
    assert days.loc["2026-02-01", "workout_min"] == 60
    workouts = workout_dates(export)
    assert set(workouts["date"].dt.strftime("%Y-%m-%d")) == {"2026-02-01"}
    assert workouts["zone4_pct"].tolist() == [30, 0]
