# Notes for Claude

Orientation lives in `README.md` (what the scores mean) and `docs/SETUP.md`
(deployment). Tests are `pytest` from the repo root - 50 of them, all passing
as of the last commit. There is no CI.

## Things the repo doesn't tell you

- **The server copy is not a git clone.** `~/fitness_by_ferenc` on the Fujitsu
  box was scp'd over, so `git pull` fails there. Deploy by copying individual
  files. Converting it to a real clone is safe whenever someone wants to -
  `.gitignore` already covers `.env`, `*.db`, `backups/` and the token store,
  so a checkout would only overwrite tracked source.

- **The dashboard uses no JavaScript, deliberately.** Expansion is `:target`
  CSS; the auto-reload is a `<meta http-equiv="refresh">`. The reason: the iOS
  widget strips the reload before handing the HTML to its WebView, and when
  that reload was `setInterval(...)` the stripping regex cut it mid-expression,
  leaving a syntax error that killed the *entire* script block - including the
  tile handlers defined above it. The page rendered fine (it is all
  server-side) and did nothing. `b076c04` removed the dependency rather than
  tightening the regex. Re-introducing script re-opens that whole class of bug.
  Side effect worth knowing: `/#health-detail` and `/#stress-detail` deep-link
  to an open panel.

- **Serving topology.** The dashboard is `https://fit.ferencpalos.is-a.dev`,
  served by gunicorn under systemd on the **host** at `:8420` - not in a
  container. `URL_PREFIX` must stay unset; setting it puts every route a level
  deep and 404s the widget's `/api/today`.

- **The reverse proxy is Nginx Proxy Manager**, admin UI on `http://<host>:81`
  (plain HTTP - the `fujitsu` hostname has a cached HSTS policy that forces
  browsers to https and breaks it, so reach it by IP). Its proxy host for the
  dashboard forwards to `192.168.0.86:8420`, the host's LAN IP. Three traps,
  each of which has already cost time:
  - NPM is itself containerised, so `127.0.0.1` never works as an upstream.
    The host's LAN IP works from any docker network; the `172.x` bridge
    gateways only work from the bridge NPM happens to sit on.
  - Its green **Online** badge does not health-check the upstream - it only
    reports that the nginx config loaded. A host 502ing continuously still
    reads Online.
  - `ferencpalos.is-a.dev` forwards wholesale to `portfolio:80` - stock
    `nginx:alpine` serving `/var/www/ferencpalos`, which proxies nothing - and
    there are no Custom Locations in NPM at all. Hand-written path rules live
    in `/data/nginx/custom/server_proxy.conf`, on the host at
    `/home/palos/server/data/npm/nginx/custom/server_proxy.conf`. NPM includes
    that file in *every* proxy host's server block and never rewrites it, so
    each rule carries an `if ($host != ...) { return 404; }` guard to keep it
    off the other domains. **`/adventures` -> `bucketlist:3000` lives in that
    file and is live - do not delete it.** Edit the file directly, then
    `docker exec nginx-proxy-manager nginx -t` before `nginx -s reload`.

- **The widget is out of git's reach.** `widget/GarminWidget.js` has to be
  pasted into Scriptable by hand. Its tap target is the widget's own
  *When Interacting -> Open URL* field; the script deliberately does not set
  `widget.url` (`905d851`), because setting both makes one tap open two apps.

- **Sessions are per-domain and per-device.** An unauthenticated browser gets
  the **demo day**, which looks like real data with a small badge. The widget's
  `/api/today` fetch authenticates separately out of the iOS keychain, so the
  tile can read real while the tapped-through page reads demo - that split
  means a missing sign-in, not a bug.

---

## Next project: a second user (his father)

Instinct 2 watch, Samsung phone. Nothing here is started.

**Instinct 2 capability was checked against Garmin's docs, not assumed:** it
has both HRV Status and Training Readiness (the latter arrived in firmware
13.10), so update the watch first. But HRV Status needs ~3 weeks of consistent
overnight wear before a baseline exists, and that cold start is the real
problem: with no HRV, `compute_readiness()` (scoring.py) substitutes a flat
`hrv_component = 60`, readiness parks at 60-70, and `recommend_training()`
emits "Moderate run, 45 min" every single day for three weeks. Body Battery is
available from day one and on the same 0-100 scale - the day's peak is roughly
the wake value - so it's the natural stand-in.

Other adjustments that setup needs:

- `recommend_training()` is run-centric; three of its four bands prescribe
  running and the top one prescribes intervals. A non-runner in his sixties
  needs a modality knob, and that top band deserves a cap regardless.
- `USER_MAX_HR` defaults to `220 - age` (config.py), which drifts badly past
  40 and is the denominator of every TRIMP calculation. Prefer Tanaka
  (`208 - 0.7 x age`) or a measured value.
- Android has no Scriptable equivalent. A PWA manifest plus "Add to Home
  screen" is the cheapest good answer; a daily ntfy push probably beats a
  widget for someone who won't go looking for one.
- Samsung's battery optimisation kills Garmin Connect's background sync -
  it has to go in "Never sleeping apps" or the whole dashboard reads stale
  through no fault of the server.

**Run him as a second instance, in his own checkout** - the app is
single-user throughout (one `GARMIN_EMAIL`, one DB, one port, one dashboard
password), and `URL_PREFIX` exists to let a second instance share the domain.
There is a genuine footgun in sharing one directory: `config.py` calls
`load_dotenv()`, which does **not** override real environment variables but
**does** fill gaps - so a second `.env` missing `FITNESS_DB_PATH` would
silently write his father's data into Ferenc's database. A separate checkout
removes the whole class of problem.
