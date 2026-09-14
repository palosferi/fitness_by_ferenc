# fitness_by_ferenc

A Whoop-style daily dashboard for a Garmin Epix Gen2, self-hosted on a home
server - no Garmin Connect+ subscription, no LLM calls, everything computed
by plain, documented formulas.

## What it does

- Pulls HR, HRV, sleep, and steps from Garmin Connect (unofficial API).
- Computes, every 30 minutes:
  - **Strain** (1-100) - Edwards TRIMP from intraday heart rate, log-scaled
    against your own trailing training load.
  - **Sleep score** (1-100) - Garmin's own score when available, else a
    duration/stages fallback.
  - **Readiness** (1-100, internal) - blends sleep, HRV vs. your 7-day
    baseline, and resting HR vs. baseline.
  - A **training recommendation** (rest / gym / run, with distance+time for
    runs) derived from readiness and yesterday's strain.
- Serves a small dashboard (4 rings + recommendation) and a JSON API.
- Ships an iOS Scriptable widget that reads the JSON API over Tailscale, so
  you get the numbers on your Home Screen without opening any app.

## Layout

| File | Purpose |
|---|---|
| `garmin_client.py` | Garmin Connect login/session caching + raw data fetch |
| `scoring.py` | All the rule-based math (strain, sleep, readiness, recommendation) |
| `storage.py` | SQLite persistence |
| `sync.py` | Scheduled job: fetch -> score -> save |
| `backup.py` | Scheduled job: daily SQLite backup + retention pruning |
| `app.py` + `templates/dashboard.html` | Flask dashboard + `/api/today` |
| `systemd/` | Unit files to run `sync.py`/`backup.py` on a timer and `app.py` as a service |
| `widget/GarminWidget.js` | iOS Scriptable home-screen widget |
| `tests/test_scoring.py` | Unit tests for the scoring math (`pytest`, see `requirements-dev.txt`) |
| `docs/SETUP.md` | Full deployment walkthrough for the Linux Mint server + Tailscale + iOS |

See `docs/SETUP.md` for the actual deploy steps.

## Explicitly not in v1

No chat interface and no LLM calls anywhere - scores and recommendations
are deterministic and free to run. That can be layered on later without
touching this scoring core.
