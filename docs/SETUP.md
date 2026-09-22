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

## 6. Public hostname

The dashboard is served at **https://fit.ferencpalos.is-a.dev** through the
reverse proxy. It owns the whole subdomain, so there is no path prefix:

- Point the proxy host at `http://<server>:8420`.
- Leave `URL_PREFIX` **unset** in `.env`. It only exists for serving under a
  sub-path (the old `ferencpalos.is-a.dev/fit` layout); setting it now would
  put every route one level deep and break the widget's `/api/today` call.

Tailscale (section 5) still works as a private fallback if the proxy is down.

## 7. iOS widget (Scriptable)

1. Install the free **Scriptable** app from the App Store.
2. Open it, create a new script, paste in the contents of `widget/GarminWidget.js`.
3. Check `SERVER_URL` at the top matches your deployment.
4. **Run it once inside Scriptable.** It prompts for your dashboard username
   and password and stores both in the iOS keychain - neither is written into
   the script, so it stays safe to share or screenshot. The username must
   match `DASHBOARD_USER` in the server's `.env` exactly.
5. Long-press your Home Screen -> add a widget -> Scriptable -> Medium size.
6. Edit the widget (long-press -> Edit Widget) and set:
   - **Script**: the one you just created
   - **When Interacting**: `Open URL`
   - **URL**: `https://fit.ferencpalos.is-a.dev/`

   Put the URL in that field rather than letting the script set it - if both
   are set the tap fires twice, leaving two apps to close. Prefer this over
   "Run Script": that routes the tap through Scriptable, which iOS gives no
   way to close afterwards, so you land in its script list on the way back.

   To open straight into a monitor, use a fragment - `/#health-detail` or
   `/#stress-detail` - and the page loads with that panel already expanded.

The tile shows three rings - Sleep, Recovery, Strain - plus today's
recommendation and a steps/sleep-target footer.

Notes on how it authenticates:
- The widget sends HTTP basic auth on the API call, so it gets **your** data.
  Without credentials it would get the public demo day instead - which is
  what the "Tap to sign in" heading means.
- The tap itself relies on the session cookie, which is why it can open in
  Safari rather than going through Scriptable's WebView. Running the script
  by hand still uses that WebView, fetching the page with the auth header
  attached (a plain `loadURL()` would arrive unauthenticated and quietly show
  the demo page).
- The dashboard page uses **no JavaScript at all** - the monitor tiles expand
  via `:target`, and the 5-minute refresh is a `<meta http-equiv="refresh">`.
  That is deliberate: it keeps the tiles working inside the in-app WebView,
  where the script the widget has to strip out used to leave the page inert.
- iOS decides how often the tile actually refreshes (roughly every 10-15
  minutes). It budgets widget refreshes for battery and ignores requests to
  go faster; tapping always fetches fresh.

## 8. A second instance, for someone else

The app is single-user throughout - one Garmin account, one database, one
password - so a second person runs as a second copy of it, not as a second
account. They get their own checkout, their own `.env` and their own port,
and the setup page collects their Garmin login so you never have to be told
it.

Use a separate directory, not a shared one. `config.py` calls `load_dotenv()`,
which does not override real environment variables but does fill gaps, so a
second `.env` missing `FITNESS_DB_PATH` would quietly write their data into
your database.

```bash
# on the server, alongside your own copy
cp -r fitness_by_ferenc fitness_by_them
cd fitness_by_them
rm -f fitness.db && rm -rf .garminconnect_tokens   # yours, if you copied it
python3 -m venv venv && venv/bin/pip install -r requirements.txt
```

Their `.env` differs from yours in a handful of places, and leaves
`GARMIN_EMAIL` and `GARMIN_PASSWORD` out entirely:

```ini
DASHBOARD_PORT=<a free port>
FITNESS_DB_PATH=/path/to/fitness_by_them/fitness.db
GARMIN_TOKENSTORE=/path/to/fitness_by_them/.garminconnect_tokens
URL_PREFIX=/them
SETUP_TOKEN=<generated below>
USER_AGE=41                # theirs, not yours
USER_EASY_PACE_MIN_PER_KM=6.5
```

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(24))"
```

Copy `systemd/` again with the paths and port pointed at the new directory,
give each unit a distinct name (`fitness-sync-them.service` and so on), and
enable them as in step 4. Sync fails until they have signed in; that is
expected, and the log line says `Reconnect at /setup`.

Then give the path somewhere to go. The dashboard's proxy host in NPM has no
Custom Locations - the hand-written rules live in
NPM's `/data/nginx/custom/server_proxy.conf`, which NPM
includes in *every* proxy host, so the rule needs the hostname guard:

```nginx
location /them {
    if ($host != <dashboard-hostname>) { return 404; }
    proxy_pass http://<server-lan-ip>:<their-port>;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

`proxy_pass` deliberately ends at the port with no path of its own, so the
`/them` prefix survives - `URL_PREFIX` has put every route a level deep and
stripping it here would 404 the lot. Then:

```bash
docker exec nginx-proxy-manager nginx -t
docker exec nginx-proxy-manager nginx -s reload
```

Finally, in person, send them to
`https://<dashboard-hostname>/them/setup?token=<the token>`. They enter
their Garmin email and password, a verification code if Garmin asks for one,
and then a username and password of their own choosing for the dashboard
itself. The Garmin password is exchanged for a token and never written to
disk; the dashboard password is stored only as a hash. Afterwards, remove
`SETUP_TOKEN` from their `.env` and restart - the page then opens only for
someone already signed in, which is what they will need when the Garmin
session eventually expires.

If they are on iOS and want the widget, their API path is
`/them/api/today`, not `/api/today`.

Worth saying out loud when you hand it over: none of this hides their data
from whoever runs the server. Root can read the database and the token store.
What it does mean is that their Garmin password is never spoken aloud, never
stored, and never known to you.

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
- The session cookie is named after `URL_PREFIX` rather than Flask's default
  `session`, so that two instances sharing a hostname can't overwrite each
  other's login. Deploying that change signs everyone out once.
- An instance set up through `/setup` has no password to fall back on when
  its Garmin tokens expire, so sync stops and the dashboard says so instead
  of freezing on the last day that worked.
- Submitting the setup form holds the request open while Garmin answers, and
  gunicorn runs a single worker, so that instance serves nothing else for up
  to `SETUP_LINK_TIMEOUT_SECONDS` (default 45). It only happens during setup,
  and only on that person's instance - but don't run it with more than one
  worker, because the MFA form would then land in a process that knows
  nothing about the login waiting in another.
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
