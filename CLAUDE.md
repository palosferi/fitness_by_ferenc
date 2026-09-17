# Notes for Claude

Orientation lives in `README.md` (what the scores mean) and `docs/SETUP.md`
(deployment). Tests are `pytest` from the repo root - 50 of them, all passing
as of the last commit. There is no CI.

Two things the repo doesn't tell you:

- **The server copy is not a git clone.** `~/fitness_by_ferenc` on the Fujitsu
  box was scp'd over, so `git pull` fails there. Deploy by copying individual
  files. Converting it to a real clone is safe whenever someone wants to -
  `.gitignore` already covers `.env`, `*.db`, `backups/` and the token store,
  so a checkout would only overwrite tracked source.
- **The reverse proxy is Nginx Proxy Manager**, admin UI on `http://<host>:81`
  (plain HTTP - the `fujitsu` hostname has a cached HSTS policy that forces
  browsers to https and breaks it, so reach it by IP).

---

## Where things stand - 2026-09-17

*Delete this section once the migration below is finished.*

### Done

`b076c04` fixed the monitor tiles being unresponsive when the dashboard was
opened from the iOS widget. Root cause worth remembering: the widget strips
the page's auto-reload before handing the HTML to its WebView, and the regex
doing it cut `setInterval(...)` mid-expression, leaving a syntax error that
killed the *entire* script block - including the tile handlers defined above
it. The page rendered fine (all server-side) and did nothing. The fix removed
the dependency rather than tightening the regex: **the dashboard now uses no
JavaScript at all**, expansion is `:target` CSS, and the reload is a
`<meta http-equiv="refresh">` that the widget strips as a whole tag. Keep it
that way - re-introducing script would re-open the same class of bug. Side
effect: `/#health-detail` and `/#stress-detail` deep-link to an open panel.

`a74b59b` moved the dashboard from `ferencpalos.is-a.dev/fit` to its own
subdomain **`fit.ferencpalos.is-a.dev`**. `URL_PREFIX` must stay unset now;
setting it puts every route a level deep and 404s the widget's `/api/today`.

Server side is fully deployed and verified: new template in place,
`URL_PREFIX` commented out in `.env`, gunicorn serving at the root on `:8420`,
service active. NPM has the proxy host and a Let's Encrypt cert for the new
name (valid to 2026-12-15).

### Blocked here

**NPM returns 502 for `https://fit.ferencpalos.is-a.dev/`** - TLS is fine, the
upstream address is wrong. The app answers 200 from the host on all of
`127.0.0.1`, `172.17.0.1`, `172.18.0.1` and `192.168.0.86`, port 8420.

The likely mistake: `ferencpalos.is-a.dev` serves the *portfolio* container as
its main forward target, with `/fit` as a **Custom Location** underneath. The
working address is the one on that custom location, not the host's main
Forward Hostname/IP. Failing that, `172.17.0.1:8420` (docker bridge gateway)
is the standard way a container reaches a host service.

To narrow it down, from inside the proxy container:

```bash
sudo docker exec nginx-proxy-manager sh -c 'for ip in 172.17.0.1 172.18.0.1 192.168.0.86; do printf "%-14s " $ip; curl -s -o /dev/null -w "%{http_code}\n" --max-time 5 http://$ip:8420/; done'
```

### Then, in order

1. Sign in at `https://fit.ferencpalos.is-a.dev/login`. Session cookies are
   per-domain, so until this is done the new host serves the **demo day** -
   which looks like real data with a badge, and is easy to mistake for a bug.
2. Re-paste `widget/GarminWidget.js` into Scriptable (git doesn't reach it),
   and set the widget's *When Interacting* to `Open URL` with the new address.
3. Verify the monitor tiles expand **through the widget**, not just in Safari.
   That is the path that was broken and the only one that proves the fix.
4. Optional: 301 the old `/fit` to the new root so stale bookmarks don't land
   on the demo page.

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
