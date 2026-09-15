// Scriptable widget: your daily fitness rings, pulled from the dashboard.
//
// Setup
// 1. Install the free "Scriptable" app from the App Store.
// 2. Create a new script, paste this whole file in.
// 3. Run it once inside Scriptable. It'll prompt for your dashboard username
//    and password and store them in the iOS keychain - they are NOT saved in
//    this script, so it stays safe to share or screenshot. The username must
//    match DASHBOARD_USER in the server's .env exactly.
// 4. Long-press the Home Screen -> add a Scriptable widget (Medium) -> set
//    its "Script" to this one.
// 5. Edit the widget (long-press -> Edit Widget):
//      When Interacting : Open URL
//      URL              : https://ferencpalos.is-a.dev/fit/
//    Put the URL in that field - this script deliberately does NOT set
//    widget.url. Setting both makes the tap fire twice: Scriptable opens for
//    the script's URL and the browser opens for the field's, leaving two apps
//    to close. One mechanism only.
//
//    "Run Script" is the other option, but avoid it: it routes the tap
//    through Scriptable, and iOS gives no way for a script to close the app
//    afterwards, so you land in its script list on the way back.
//
//    If the keychain ever loses your credentials, open this script manually
//    in Scriptable to be prompted again.
//
// The tile refreshes on iOS's own schedule (roughly every 10-15 min; iOS
// budgets widget refreshes for battery and ignores any request to go
// faster). Tapping always fetches fresh.

const SERVER_URL = "https://ferencpalos.is-a.dev/fit";
const KEYCHAIN_USER = "fitness_by_ferenc_user";
const KEYCHAIN_PASS = "fitness_by_ferenc_password";

// ---------------------------------------------------------------- auth

// Both are prompted for and kept in the keychain - the username must match
// DASHBOARD_USER on the server exactly, so it is asked for rather than
// assumed.
async function getCredentials() {
  if (Keychain.contains(KEYCHAIN_USER) && Keychain.contains(KEYCHAIN_PASS)) {
    return { user: Keychain.get(KEYCHAIN_USER), password: Keychain.get(KEYCHAIN_PASS) };
  }
  // Widgets can't show prompts, so only ask when running inside the app.
  if (!config.runsInWidget) {
    const alert = new Alert();
    alert.title = "Dashboard sign in";
    alert.message = "Stored in the iOS keychain, not in this script.";
    alert.addTextField("Username", Keychain.contains(KEYCHAIN_USER) ? Keychain.get(KEYCHAIN_USER) : "");
    alert.addSecureTextField("Password", "");
    alert.addAction("Save");
    alert.addCancelAction("Cancel");
    if ((await alert.present()) === 0) {
      const user = alert.textFieldValue(0);
      const password = alert.textFieldValue(1);
      if (user && password) {
        Keychain.set(KEYCHAIN_USER, user);
        Keychain.set(KEYCHAIN_PASS, password);
        return { user, password };
      }
    }
  }
  return null;
}

function authHeaders(creds) {
  if (!creds) return {};
  const token = Data.fromString(`${creds.user}:${creds.password}`).toBase64String();
  return { Authorization: `Basic ${token}` };
}

async function fetchToday(creds) {
  const req = new Request(`${SERVER_URL}/api/today`);
  req.headers = authHeaders(creds);
  req.timeoutInterval = 8;
  try {
    return await req.loadJSON();
  } catch (e) {
    return null;
  }
}

// ---------------------------------------------------------------- drawing

function colorFor(value, thresholds = [34, 67]) {
  if (value === null || value === undefined) return new Color("#555555");
  if (value < thresholds[0]) return new Color("#e5484d");
  if (value < thresholds[1]) return new Color("#f5a623");
  return new Color("#30a46c");
}

function drawRingImage(size, pct, color, displayText) {
  const ctx = new DrawContext();
  ctx.size = new Size(size, size);
  ctx.opaque = false;
  ctx.respectScreenScale = true;

  const center = size / 2;
  const lineWidth = size * 0.13;
  const radius = center - lineWidth / 2;

  function drawArc(startDeg, endDeg, strokeColor) {
    const path = new Path();
    const steps = 80;
    const startRad = ((startDeg - 90) * Math.PI) / 180;
    path.move(new Point(center + radius * Math.cos(startRad), center + radius * Math.sin(startRad)));
    for (let i = 1; i <= steps; i++) {
      const deg = startDeg + ((endDeg - startDeg) * i) / steps;
      const rad = ((deg - 90) * Math.PI) / 180;
      path.addLine(new Point(center + radius * Math.cos(rad), center + radius * Math.sin(rad)));
    }
    ctx.addPath(path);
    ctx.setStrokeColor(strokeColor);
    ctx.setLineWidth(lineWidth);
    ctx.strokePath();
  }

  drawArc(0, 360, new Color("#26272c"));
  const clamped = Math.max(0, Math.min(100, pct || 0));
  if (clamped > 0) drawArc(0, 360 * (clamped / 100), color);

  ctx.setTextColor(Color.white());
  ctx.setFont(Font.boldSystemFont(size * 0.24));
  ctx.setTextAlignedCenter();
  ctx.drawTextInRect(displayText, new Rect(0, center - size * 0.14, size, size * 0.3));

  return ctx.getImage();
}

function addRingColumn(stack, label, pct, displayText, color, size = 70) {
  const col = stack.addStack();
  col.layoutVertically();
  col.centerAlignContent();
  const img = col.addImage(drawRingImage(size, pct, color, displayText));
  img.imageSize = new Size(size, size);
  col.addSpacer(3);
  const lbl = col.addText(label);
  lbl.font = Font.systemFont(10);
  lbl.textColor = Color.gray();
  lbl.centerAlignText();
}

// ---------------------------------------------------------------- widget

async function createWidget(creds) {
  const data = await fetchToday(creds);
  const widget = new ListWidget();
  widget.backgroundColor = new Color("#0a0a0b");
  widget.setPadding(12, 12, 12, 12);

  const stale = data && data.stale;
  const demo = data && data.demo;

  let heading = "Today";
  let headingColor = Color.gray();
  if (!data) {
    heading = "No data";
    headingColor = new Color("#e5484d");
  } else if (demo) {
    heading = creds ? "Wrong credentials - tap" : "Tap to sign in";
    headingColor = new Color("#5b8def");
  } else if (stale) {
    heading = "Today (stale)";
    headingColor = new Color("#f5a623");
  }

  const title = widget.addText(heading);
  title.font = Font.mediumSystemFont(12);
  title.textColor = headingColor;
  widget.addSpacer(6);

  const row = widget.addStack();
  row.layoutHorizontally();
  row.centerAlignContent();

  if (data) {
    const sleep = data.sleep_score;
    const recovery = data.readiness_score;
    const strain = data.strain_score;

    // Same three as the dashboard, same order.
    addRingColumn(row, "Sleep", sleep, sleep != null ? `${Math.round(sleep)}` : "--", colorFor(sleep));
    row.addSpacer();
    addRingColumn(row, "Recovery", recovery, recovery != null ? `${Math.round(recovery)}` : "--", colorFor(recovery));
    row.addSpacer();
    addRingColumn(row, "Strain", strain, strain != null ? `${Math.round(strain)}` : "--", new Color("#5b8def"));

    widget.addSpacer(8);
    const rec = widget.addText(data.recommendation_detail || "");
    rec.font = Font.systemFont(11);
    rec.textColor = Color.white();
    rec.lineLimit = 2;

    const footerBits = [];
    if (data.steps) footerBits.push(`${Math.round(data.steps / 1000)}k steps`);
    const needMin = data.sleep_need_minutes || (data.sleep_recommendation_hours || 0) * 60;
    if (needMin) {
      const h = Math.floor(Math.round(needMin) / 60);
      const m = Math.round(needMin) % 60;
      footerBits.push(`sleep target ${m ? `${h}h${String(m).padStart(2, "0")}m` : `${h}h`}`);
    }
    if (footerBits.length) {
      widget.addSpacer(4);
      const footer = widget.addText(footerBits.join(" · "));
      footer.font = Font.systemFont(10);
      footer.textColor = Color.gray();
    }
  } else {
    row.addSpacer();
    const err = row.addText("Can't reach server");
    err.font = Font.systemFont(11);
    err.textColor = new Color("#e5484d");
    row.addSpacer();
  }

  widget.refreshAfterDate = new Date(Date.now() + 10 * 60 * 1000);
  return widget;
}

// ---------------------------------------------------------------- in-app

async function presentDashboard(creds) {
  const webView = new WebView();

  // Fetch the page ourselves so the auth header goes with it, then hand the
  // HTML to the WebView. loadURL() alone would arrive unauthenticated and
  // show the public demo page instead of real numbers. The page is fully
  // self-contained (inline CSS and SVG), so nothing else needs fetching.
  const req = new Request(`${SERVER_URL}/`);
  req.headers = authHeaders(creds);
  req.timeoutInterval = 10;

  try {
    let html = await req.loadString();
    // The page reloads itself every 5 minutes; that request wouldn't carry
    // the auth header and would silently drop back to demo data.
    html = html.replace(/setInterval\([^)]*\)[^;]*;/g, "");
    await webView.loadHTML(html, SERVER_URL);
  } catch (e) {
    await webView.loadURL(SERVER_URL);
  }

  await webView.present(true);
}

// ---------------------------------------------------------------- entry

const creds = await getCredentials();

if (config.runsInWidget) {
  Script.setWidget(await createWidget(creds));
} else {
  await presentDashboard(creds);
}
Script.complete();
