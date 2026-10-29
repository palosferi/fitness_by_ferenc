#!/usr/bin/env python3
"""Compare a WHOOP export against Garmin history for the same days, and check
our strain and sleep-need formulas against WHOOP's.

    python -m analysis.compare --live-k 121.6

Reads data/whoop/ (analysis.whoop) and the newest data/garmin/*.csv
(analysis/garmin_history.py), and writes a merged CSV, a summary and charts
to data/comparison/.

Readiness is scored against WHOOP recovery twice over: on WHOOP's own HRV,
resting HR and sleep (what compute_readiness's weights were fitted on), and
on Garmin's for the same days - the inputs it actually gets in production.

Strain can't be compared day for day: Garmin no longer serves intraday heart
rate for those months, so our TRIMP can't be recomputed. Instead each WHOOP
workout's time-in-zone is run through our own TRIMP, which puts both scores
on WHOOP's heart-rate data and leaves only the formulas differing. The fitted
calibration k is then directly comparable to the live one sync.py derives
from trailing history (--live-k).
"""

import argparse
import math
from pathlib import Path

import numpy as np
import pandas as pd

import config
import scoring
from analysis.whoop import load_whoop, workout_dates

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Midpoints of WHOOP's five %-of-max-HR zones. Time below zone 1 (<50% max
# HR) earns nothing: with resting HR near 58 and max near 199 it sits at or
# under our 20% HRR dead zone.
ZONE_MIDPOINTS = [0.55, 0.65, 0.75, 0.85, 0.95]

# (WHOOP column, Garmin column, label, unit)
PAIRS = [
    ("rhr", "resting_hr", "Resting HR", "bpm"),
    ("hrv", "hrv_last_night", "HRV", "ms"),
    ("resp_rate", "respiration_avg", "Respiratory rate", "rpm"),
    ("sleep_min", "sleep_duration_min", "Sleep duration", "min"),
    ("sleep_need_min", "sleep_need_minutes", "Sleep need", "min"),
]

WHOOP_MAX_STRAIN = 21.0

# Reference palette (dataviz skill), light mode: WHOOP is slot 1, ours/Garmin slot 2.
INK, INK_2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"
WHOOP_C, GARMIN_C, THIRD_C = "#2a78d6", "#eb6834", "#1baf7a"


def spearman(a, b):
    """Rank correlation, as Pearson on ranks - pandas' own needs scipy."""
    both = pd.concat([a, b], axis=1).dropna()
    return both.iloc[:, 0].rank().corr(both.iloc[:, 1].rank())


def load_garmin(path=None):
    if path is None:
        candidates = sorted((DATA_DIR / "garmin").glob("*.csv"))
        if not candidates:
            raise SystemExit("No data/garmin/*.csv - run analysis/garmin_history.py first.")
        path = candidates[-1]
    frame = pd.read_csv(path, parse_dates=["date"]).set_index("date")
    # A day Garmin has nothing for (watch not yet owned, or not worn) comes
    # back as a row of blanks; drop those rather than read them as zeros.
    worn = frame[["resting_hr", "sleep_duration_min", "steps"]].notna().any(axis=1)
    return frame[worn]


def workout_trimp(row, max_hr, exponent):
    """Our Banister TRIMP for a WHOOP workout, from its time in each zone."""
    if not row["rhr"] or max_hr <= row["rhr"]:
        return float("nan")
    total = 0.0
    for i, mid in enumerate(ZONE_MIDPOINTS, 1):
        fraction = scoring.hrr_fraction(mid * max_hr, row["rhr"], max_hr)
        if fraction > scoring.TRIMP_DEAD_ZONE:
            total += row["minutes"] * row[f"zone{i}_pct"] / 100 * scoring._trimp_rate(fraction, exponent)
    return total


def fit_k(trimp, whoop_strain):
    """The calibration k that best maps TRIMP onto WHOOP strain through our
    curve, 100 * (1 - e^(-T/k)), with WHOOP's 0-21 read as 0-100."""
    target = whoop_strain / WHOOP_MAX_STRAIN * 100
    grid = np.arange(scoring.MIN_TRIMP_K, 1000.0, 0.5)
    errors = [np.mean((100 * (1 - np.exp(-trimp / k)) - target) ** 2) for k in grid]
    best = int(np.argmin(errors))
    return float(grid[best]), math.sqrt(errors[best])


def fit_log(trimp, whoop_strain):
    """WHOOP strain = a * ln(1 + T / b): the shape WHOOP describes for its own
    scale, fitted for comparison with our saturating curve."""
    best = None
    for b in np.arange(1.0, 400.0, 1.0):
        x = np.log1p(trimp / b)
        a = float(np.dot(x, whoop_strain) / np.dot(x, x))
        rmse = math.sqrt(np.mean((a * x - whoop_strain) ** 2))
        if best is None or rmse < best[2]:
            best = (a, float(b), rmse)
    return best


def agreement(merged):
    rows = []
    for whoop_col, garmin_col, label, unit in PAIRS:
        both = merged[[whoop_col, garmin_col]].dropna()
        if len(both) < 3:
            rows.append({"metric": label, "unit": unit, "n": len(both)})
            continue
        diff = both[garmin_col] - both[whoop_col]
        rows.append({
            "metric": label, "unit": unit, "n": len(both),
            "whoop_mean": both[whoop_col].mean(), "garmin_mean": both[garmin_col].mean(),
            "offset_mean": diff.mean(), "offset_sd": diff.std(),
            "pearson_r": both[whoop_col].corr(both[garmin_col]),
            "spearman_r": spearman(both[whoop_col], both[garmin_col]),
        })
    return pd.DataFrame(rows)


def sleep_need_model(whoop):
    """Least-squares split of WHOOP's sleep need into a base, a per-strain-
    point term for the day before the night, and a debt term."""
    frame = whoop[["sleep_need_min", "sleep_debt_min"]].copy()
    frame["prev_strain"] = whoop["day_strain"].shift(1)
    frame = frame.dropna()
    design = np.column_stack([np.ones(len(frame)), frame["prev_strain"], frame["sleep_debt_min"]])
    coef, *_ = np.linalg.lstsq(design, frame["sleep_need_min"].to_numpy(), rcond=None)
    fitted = design @ coef
    residual = frame["sleep_need_min"].to_numpy() - fitted
    r2 = 1 - residual.var() / frame["sleep_need_min"].var(ddof=0)
    return {"base_min": coef[0], "per_strain_point_min": coef[1], "per_debt_min": coef[2],
            "r2": r2, "n": len(frame)}


def our_sleep_need(whoop):
    """recommend_sleep_hours() as sync.py would call it the evening before
    each night, fed WHOOP's day strain rescaled to 0-100 and the night
    before, in minutes."""
    out = {}
    dates = list(whoop.index)
    for i, day in enumerate(dates):
        if i == 0 or pd.isna(whoop.loc[dates[i - 1], "day_strain"]):
            continue
        strain = whoop.loc[dates[i - 1], "day_strain"] / WHOOP_MAX_STRAIN * 100
        out[day] = scoring.recommend_sleep_hours(strain, whoop.loc[dates[i - 1], "sleep_min"]) * 60
    return pd.Series(out, name="ours_need_min")


def our_readiness(hrv, resting_hr, sleep_score, use_hrv=True):
    """compute_readiness() per day, with baselines as sync.py builds them:
    the mean of the previous seven nights, once MIN_BASELINE_SAMPLES exist."""
    def baseline(series):
        return series.rolling(7, min_periods=config.MIN_BASELINE_SAMPLES).mean().shift(1)

    def clean(value):
        return None if pd.isna(value) else value

    frame = pd.DataFrame({"hrv": hrv, "hrv_base": baseline(hrv), "rhr": resting_hr,
                          "rhr_base": baseline(resting_hr), "sleep": sleep_score})
    return frame.apply(lambda r: scoring.compute_readiness(
        clean(r["sleep"]), clean(r["hrv"]) if use_hrv else None, clean(r["hrv_base"]) if use_hrv else None,
        clean(r["rhr"]), clean(r["rhr_base"])), axis=1)


def readiness_check(whoop, garmin):
    """(label, n, Spearman, mean abs error, mean) against WHOOP recovery, on
    the days where Garmin had every input and both baselines - so the rows
    compare like with like."""
    full = garmin[["hrv_last_night", "resting_hr", "sleep_score"]].notna().all(axis=1)
    candidates = {
        "ours, WHOOP inputs": our_readiness(whoop["hrv"], whoop["rhr"], whoop["sleep_performance"]),
        "ours, Garmin inputs": our_readiness(garmin["hrv_last_night"], garmin["resting_hr"], garmin["sleep_score"]),
        "ours, Garmin inputs, no HRV": our_readiness(garmin["hrv_last_night"], garmin["resting_hr"],
                                                     garmin["sleep_score"], use_hrv=False),
        "Garmin Training Readiness": garmin["readiness"],
    }
    days = garmin.index[full][config.MIN_BASELINE_SAMPLES:].intersection(whoop.index)
    truth = whoop.loc[days, "recovery"]
    rows = []
    for label, series in candidates.items():
        both = pd.concat([series.reindex(days), truth], axis=1).dropna()
        rows.append((label, len(both), spearman(both.iloc[:, 0], both.iloc[:, 1]),
                     (both.iloc[:, 0] - both.iloc[:, 1]).abs().mean(), both.iloc[:, 0].mean()))
    return rows, truth.mean()


def _style(ax, title, unit):
    ax.set_facecolor(SURFACE)
    ax.set_title(title, loc="left", fontsize=10, color=INK, fontweight="bold")
    ax.set_ylabel(unit, color=INK_2, fontsize=8)
    ax.tick_params(colors=INK_2, labelsize=8, length=0)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)


def plot_metrics(merged, path):
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(len(PAIRS), 1, figsize=(9, 2.1 * len(PAIRS)), sharex=True, facecolor=SURFACE)
    for ax, (whoop_col, garmin_col, label, unit) in zip(axes, PAIRS):
        _style(ax, label, unit)
        ax.plot(merged.index, merged[whoop_col], color=WHOOP_C, linewidth=2, marker="o", markersize=3,
                label="WHOOP")
        ax.plot(merged.index, merged[garmin_col], color=GARMIN_C, linewidth=2, marker="o", markersize=3,
                label="Garmin")
    axes[0].legend(loc="upper left", bbox_to_anchor=(0, 1.45), ncol=2, frameon=False, fontsize=8,
                   labelcolor=INK_2)
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def plot_strain(workouts, fitted_k, live_k, log_fit, path):
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(7, 4.6), facecolor=SURFACE)
    _style(ax, "WHOOP activity strain vs our TRIMP, per workout", "WHOOP strain (0-21)")
    ax.set_xlabel("Banister TRIMP from WHOOP time-in-zone", color=INK_2, fontsize=8)
    ax.scatter(workouts["trimp"], workouts["strain"], s=36, color=WHOOP_C, edgecolor=SURFACE,
               linewidth=1.5, zorder=3, label="WHOOP workout")
    x = np.linspace(0, max(workouts["trimp"].max() * 1.05, 10), 200)
    scale = WHOOP_MAX_STRAIN / 100
    ax.plot(x, scale * 100 * (1 - np.exp(-x / fitted_k)), color=GARMIN_C, linewidth=2,
            label=f"Our curve, fitted k={fitted_k:.0f}")
    if live_k:
        ax.plot(x, scale * 100 * (1 - np.exp(-x / live_k)), color=GARMIN_C, linewidth=2, linestyle="--",
                label=f"Our curve, live k={live_k:.0f}")
    a, b, _ = log_fit
    ax.plot(x, a * np.log1p(x / b), color=THIRD_C, linewidth=2, label=f"Log fit {a:.1f}·ln(1+T/{b:.0f})")
    ax.set_ylim(0, WHOOP_MAX_STRAIN)
    ax.legend(frameon=False, fontsize=8, labelcolor=INK_2, loc="lower right")
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def plot_sleep_need(merged, path):
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(9, 3.4), facecolor=SURFACE)
    _style(ax, "Sleep need", "hours")
    for column, color, label in [("sleep_need_min", WHOOP_C, "WHOOP"),
                                 ("sleep_need_minutes", GARMIN_C, "Garmin"),
                                 ("ours_need_min", THIRD_C, "Our fallback formula")]:
        ax.plot(merged.index, merged[column] / 60, color=color, linewidth=2, label=label)
    ax.legend(frameon=False, fontsize=8, labelcolor=INK_2, ncol=3, loc="upper left")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--whoop", default=DATA_DIR / "whoop", type=Path)
    parser.add_argument("--garmin", type=Path, help="garmin_history.py CSV (default: newest in data/garmin)")
    parser.add_argument("--out", default=DATA_DIR / "comparison", type=Path)
    parser.add_argument("--live-k", type=float,
                        help="the dashboard's current calibration k, to draw against the fitted one")
    parser.add_argument("--max-hr", type=float,
                        help="max HR behind WHOOP's zones (default: highest it recorded)")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    whoop = load_whoop(args.whoop)
    garmin = load_garmin(args.garmin)
    merged = whoop.join(garmin, how="inner")
    merged = merged.join(our_sleep_need(whoop), how="left")
    merged.to_csv(args.out / "merged_daily.csv")

    max_hr = args.max_hr or float(whoop["day_max_hr"].max())
    workouts = workout_dates(args.whoop)
    workouts["trimp"] = workouts.apply(workout_trimp, axis=1, max_hr=max_hr, exponent=config.USER_TRIMP_EXPONENT)
    workouts = workouts.dropna(subset=["trimp"])
    workouts.to_csv(args.out / "workouts_trimp.csv", index=False)

    fitted_k, k_rmse = fit_k(workouts["trimp"].to_numpy(), workouts["strain"].to_numpy())
    log_fit = fit_log(workouts["trimp"].to_numpy(), workouts["strain"].to_numpy())
    need_model = sleep_need_model(whoop)
    table = agreement(merged)
    table.round(2).to_csv(args.out / "agreement.csv", index=False)

    # Day level: WHOOP day strain against every load measure Garmin still has.
    day = merged.copy()
    day["intensity_min"] = day["moderate_min"] + 2 * day["vigorous_min"]
    day = day.join(workouts.groupby("date")["trimp"].sum().rename("whoop_workout_trimp"), how="left")
    day["whoop_workout_trimp"] = day["whoop_workout_trimp"].fillna(0)
    load_cols = ["whoop_workout_trimp", "intensity_min", "active_kcal", "activity_load", "steps",
                 "body_battery_drained", "hr_trimp"]
    load_r = {c: spearman(day["day_strain"], day[c]) for c in load_cols if day[c].notna().sum() >= 5}

    lines = [
        f"WHOOP {whoop.index.min():%Y-%m-%d}..{whoop.index.max():%Y-%m-%d} ({len(whoop)} days); "
        f"Garmin overlap {len(merged)} days ({merged.index.min():%Y-%m-%d}..{merged.index.max():%Y-%m-%d})",
        "",
        "Agreement (offset = Garmin - WHOOP):",
        table.round(2).to_string(index=False),
        "",
        f"Strain, {len(workouts)} WHOOP workouts, TRIMP from zones with max HR {max_hr:.0f}:",
        f"  Spearman(TRIMP, WHOOP activity strain) = "
        f"{spearman(workouts['trimp'], workouts['strain']):.2f}",
        f"  our curve fitted k = {fitted_k:.1f}  (RMSE {k_rmse * WHOOP_MAX_STRAIN / 100:.2f} WHOOP strain points)",
        f"  log fit: {log_fit[0]:.2f} * ln(1 + T/{log_fit[1]:.0f})  (RMSE {log_fit[2]:.2f})",
    ]
    if args.live_k:
        lines.append(f"  live k = {args.live_k:.1f} -> ratio live/fitted {args.live_k / fitted_k:.2f}")
    lines += [
        "",
        "Day strain (Spearman with WHOOP day strain, overlap days):",
        *[f"  {c:22s} {r:+.2f}" for c, r in load_r.items()],
        f"  WHOOP day strain on days with no logged workout: median "
        f"{whoop.loc[whoop['workout_count'] == 0, 'day_strain'].median():.1f}",
        "",
        "WHOOP sleep need = base + a * previous day strain + b * debt:",
        f"  base {need_model['base_min']:.0f} min, a {need_model['per_strain_point_min']:.1f} min/strain point, "
        f"b {need_model['per_debt_min']:.2f}, R^2 {need_model['r2']:.2f}, n {need_model['n']}",
    ]
    ours = merged[["sleep_need_min", "sleep_need_minutes", "ours_need_min"]]
    lines.append("Sleep need means (min): " + ", ".join(f"{c} {ours[c].mean():.0f}" for c in ours))
    lines.append("Sleep need Spearman vs WHOOP: "
                 + ", ".join(f"{c} {spearman(ours['sleep_need_min'], ours[c]):+.2f}"
                             for c in ("sleep_need_minutes", "ours_need_min")))
    readiness_rows, recovery_mean = readiness_check(whoop, garmin)
    lines += ["", f"Readiness vs WHOOP recovery (mean {recovery_mean:.0f}), days with full Garmin inputs:"]
    lines += [f"  {label:30s} n {n:2d}  Spearman {rho:+.2f}  MAE {mae:4.1f}  mean {mean:5.1f}"
              for label, n, rho, mae, mean in readiness_rows]
    summary = "\n".join(lines)
    (args.out / "summary.txt").write_text(summary + "\n")
    print(summary)

    plot_metrics(merged, args.out / "metrics.png")
    plot_strain(workouts, fitted_k, args.live_k, log_fit, args.out / "strain_fit.png")
    plot_sleep_need(merged, args.out / "sleep_need.png")
    print(f"\nWrote {args.out}/")


if __name__ == "__main__":
    main()
