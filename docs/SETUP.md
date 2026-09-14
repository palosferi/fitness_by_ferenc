# Setup: deploying to the Fujitsu server (Linux Mint, headless)

## 1. Copy the project over and install dependencies

```bash
# on the server, e.g. under your home directory
git clone <this repo, or scp the folder over> ~/fitness_by_ferenc
cd ~/fitness_by_ferenc

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 2. Configure

```bash
cp .env.example .env
nano .env
```

Fill in:
- `GARMIN_EMAIL` / `GARMIN_PASSWORD` - your normal Garmin Connect login.
- `USER_AGE` (or `USER_MAX_HR` if you know your real tested max HR).
- `USER_EASY_PACE_MIN_PER_KM` - your typical easy run pace.
- `FITNESS_DB_PATH` / `GARMIN_TOKENSTORE` - absolute paths, e.g. `/home/<you>/fitness_by_ferenc/fitness.db`.

## 3. First login (do this interactively, not via systemd)

Garmin sometimes asks for an MFA/verification code on a new login. Run the
sync manually the first time so you can answer any prompt:

```bash
source venv/bin/activate
python sync.py
```

If it succeeds, it caches a session token under `GARMIN_TOKENSTORE` so future
runs (via the systemd timer) don't need your password or a new MFA code.
Check the output - it should log a line like
`Synced 2026-09-14: strain=... sleep=... readiness=... rec=...`.

## 4. Install as systemd services

Edit the three files in `systemd/` and replace `YOURUSER` and
`/home/YOURUSER/fitness_by_ferenc` with your actual username/path, then:

```bash
sudo cp systemd/fitness-sync.service systemd/fitness-sync.timer \
        systemd/fitness-backup.service systemd/fitness-backup.timer \
        systemd/fitness-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload

sudo systemctl enable --now fitness-sync.timer
sudo systemctl enable --now fitness-backup.timer
sudo systemctl enable --now fitness-dashboard.service

# sanity checks
systemctl status fitness-sync.timer
systemctl status fitness-backup.timer
systemctl status fitness-dashboard.service
journalctl -u fitness-sync.service -n 50
```

`fitness-backup.timer` copies `fitness.db` into `BACKUP_DIR` (default
`./backups/`) once a day and prunes anything older than
`BACKUP_RETENTION_DAYS` (default 30) - see `.env.example`.

The dashboard should now be live at `http://<server-lan-ip>:8420` on your
home network.

## 5. Tailscale (reach it from your phone off wifi)

On the server:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Follow the printed login link once (same Tailscale/Google/etc. account
you'll use on the iPhone). Then find the server's Tailscale name:

```bash
tailscale status
# or
tailscale ip -4
```

It'll be something like `fujitsu.tailXXXXX.ts.net`.

On the iPhone: install the **Tailscale** app from the App Store, sign in
with the same account, and turn it on. Once connected, Safari can reach
`http://fujitsu.tailXXXXX.ts.net:8420` from anywhere (cellular included).

Add that page to your Home Screen (Share -> Add to Home Screen) for a
one-tap dashboard.

## 6. iOS widget (Scriptable)

1. Install the free **Scriptable** app from the App Store.
2. Open it, create a new script, paste in the contents of `widget/GarminWidget.js`.
3. Edit the `SERVER_URL` constant at the top to your Tailscale hostname + `:8420`.
4. Run it once inside the app to confirm it pulls real data.
5. Long-press your Home Screen -> add a widget -> Scriptable -> pick Medium size.
6. Edit the widget, set "Script" to the one you just created, "when interacting" to "Run Script".

The widget refreshes roughly every 15 minutes (iOS controls the exact
timing) and shows four rings - Strain, Sleep, Readiness, Steps - plus
today's training recommendation.

## Notes / things that may need tuning

- The unofficial `garminconnect` library logs in through Garmin's normal
  consumer login - it isn't an official/sanctioned API, so a future Garmin
  change could break it. If sync stops working, check
  `pip install -U garminconnect` first.
- The strain/readiness formulas in `scoring.py` are plain, documented
  rules (Edwards TRIMP for strain, a weighted sleep/HRV/RHR blend for
  readiness) - not something Garmin or Whoop compute for you. Expect to
  tune the thresholds in `recommend_training()` after a couple of weeks of
  watching how the numbers track how you actually feel.
- `fitness-sync.timer` runs every 30 minutes; that's frequent enough for
  strain to visibly climb through the day without hammering Garmin's servers.
- If a Garmin endpoint fails mid-sync (timeout, 404, etc.), that field is
  left as `None`/unknown rather than a false zero, and the dashboard shows a
  "Partial sync" notice naming which endpoints failed. If sync stops running
  altogether (session expired, server down), the dashboard/widget switch to
  a "stale" notice after `SYNC_STALE_MINUTES` (default 75) instead of
  silently showing old numbers as if they were current.

## Running tests

```bash
pip install -r requirements-dev.txt
pytest
```

Covers the pure scoring math in `scoring.py` (TRIMP, sleep/readiness
fallbacks, recommendation thresholds) - nothing that touches Garmin or the
DB, so it runs anywhere without credentials.
