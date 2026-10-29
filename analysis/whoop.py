"""Load a WHOOP data export (the "my_whoop_data_*.zip" CSVs) into one row per day.

WHOOP organises everything by physiological cycle: a cycle starts when you
fall asleep and ends when you next do, so its recovery, resting HR and HRV
come from the sleep at its start and its Day Strain from the waking hours
after. Each cycle is dated by the morning that sleep ended - the same
convention Garmin uses for a night's sleep, and the day the strain mostly
accrues on - which makes the frames join on date without any shifting.

Sleep metrics come from sleeps.csv, not the copies in the cycles file: when
a cycle holds a nap, the cycles file can carry the nap's fields in place of
the night's (2026-02-01 reads as 103 minutes asleep from 17:52 there, against
a 590-minute night in sleeps.csv).

Times in the export are local wall-clock already (the timezone sits in its
own column), so they are read as naive datetimes.
"""

from pathlib import Path

import pandas as pd

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "data" / "whoop"

CYCLE_COLUMNS = {
    "Cycle start time": "cycle_start",
    "Recovery score %": "recovery",
    "Resting heart rate (bpm)": "rhr",
    "Heart rate variability (ms)": "hrv",
    "Skin temp (celsius)": "skin_temp",
    "Blood oxygen %": "spo2",
    "Day Strain": "day_strain",
    "Max HR (bpm)": "day_max_hr",
}

SLEEP_COLUMNS = {
    "Cycle start time": "cycle_start",
    "Respiratory rate (rpm)": "resp_rate",
    "Sleep onset": "sleep_onset",
    "Wake onset": "wake_onset",
    "Asleep duration (min)": "sleep_min",
    "In bed duration (min)": "in_bed_min",
    "Sleep need (min)": "sleep_need_min",
    "Sleep debt (min)": "sleep_debt_min",
    "Sleep performance %": "sleep_performance",
}

ZONE_COLUMNS = [f"HR Zone {i} %" for i in range(1, 6)]


def _read(path, time_columns):
    frame = pd.read_csv(path)
    for column in time_columns:
        frame[column] = pd.to_datetime(frame[column], errors="coerce")
    return frame


def load_workouts(directory=DEFAULT_DIR):
    """One row per logged workout, with its cycle's start so it can be tied
    back to a day. Zone percentages are WHOOP's five %-of-max-HR bands
    (50-60 ... 90-100); whatever they leave short of 100 was below zone 1."""
    frame = _read(Path(directory) / "workouts.csv",
                  ["Cycle start time", "Workout start time", "Workout end time"])
    frame = frame.rename(columns={
        "Cycle start time": "cycle_start",
        "Workout start time": "start",
        "Duration (min)": "minutes",
        "Activity name": "activity",
        "Activity Strain": "strain",
        "Max HR (bpm)": "max_hr",
        "Average HR (bpm)": "avg_hr",
    })
    frame = frame.rename(columns={c: f"zone{i}_pct" for i, c in enumerate(ZONE_COLUMNS, 1)})
    keep = ["cycle_start", "start", "minutes", "activity", "strain", "max_hr", "avg_hr"]
    return frame[keep + [f"zone{i}_pct" for i in range(1, 6)]]


def load_sleeps(directory=DEFAULT_DIR):
    """(main sleep per cycle, nap minutes per cycle). A cycle's main sleep is
    its longest non-nap sleep."""
    frame = _read(Path(directory) / "sleeps.csv", ["Cycle start time", "Sleep onset", "Wake onset"])
    is_nap = frame["Nap"].astype(str).str.lower() == "true"
    naps = (frame[is_nap].groupby("Cycle start time")["Asleep duration (min)"].sum()
            .rename("nap_min").rename_axis("cycle_start"))
    main = (frame[~is_nap].rename(columns=SLEEP_COLUMNS)[list(SLEEP_COLUMNS.values())]
            .sort_values("sleep_min").drop_duplicates("cycle_start", keep="last"))
    return main, naps


def load_whoop(directory=DEFAULT_DIR):
    """Daily frame indexed by date: rhr, hrv, resp_rate, sleep_min,
    recovery, day_strain and the rest of CYCLE_COLUMNS and SLEEP_COLUMNS,
    plus nap_min, workout_count and workout_min.

    The cycle still in progress when the export was taken has no Day Strain
    yet; it is kept, with day_strain NaN, because its sleep is complete.
    """
    directory = Path(directory)
    cycles = _read(directory / "physiological_cycles.csv", ["Cycle start time"])
    cycles = cycles.rename(columns=CYCLE_COLUMNS)[list(CYCLE_COLUMNS.values())]
    main_sleep, naps = load_sleeps(directory)

    workouts = load_workouts(directory)
    per_cycle = workouts.groupby("cycle_start").agg(
        workout_count=("minutes", "size"), workout_min=("minutes", "sum"))
    cycles = (cycles.merge(main_sleep, on="cycle_start", how="left")
              .merge(per_cycle, left_on="cycle_start", right_index=True, how="left")
              .merge(naps, left_on="cycle_start", right_index=True, how="left"))
    cycles[["workout_count", "workout_min", "nap_min"]] = (
        cycles[["workout_count", "workout_min", "nap_min"]].fillna(0))

    cycles["date"] = cycles["wake_onset"].dt.normalize()
    # Two cycles can end on one morning (an early night after a nap-length
    # sleep); the longer sleep is the night that counts.
    cycles = (cycles.dropna(subset=["date"])
              .sort_values(["date", "sleep_min"])
              .drop_duplicates("date", keep="last"))
    return cycles.set_index("date").sort_index()


def workout_dates(directory=DEFAULT_DIR):
    """Workouts with the date of the cycle they belong to, as load_whoop dates it."""
    days = load_whoop(directory).reset_index()[["date", "cycle_start", "rhr"]]
    return load_workouts(directory).merge(days, on="cycle_start", how="left")
