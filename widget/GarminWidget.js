// Scriptable widget: daily fitness rings, pulled from your home-server dashboard.
// 1. Install the free "Scriptable" app from the App Store.
// 2. Create a new script, paste this whole file in.
// 3. Replace SERVER_URL below with your Tailscale hostname/IP + port.
// 4. Run once in-app to test, then add a Scriptable widget to your Home
//    Screen and set its "Script" to this one (Medium size recommended).
// 5. Edit the widget (long-press -> Edit Widget) and set "Tap" / "When
//    Interacting" to "Run Script" (not "Open URL") - that's what makes
//    tapping open a fullscreen in-app view instead of kicking out to Safari.
//    Note: the passive home-screen tile itself still only refreshes on
//    iOS's own schedule (it ignores taps for that) - iOS budgets widget
//    refreshes for battery reasons and there's no way around that from
//    Scriptable. Tapping gets you a guaranteed-fresh view on demand instead.

const SERVER_URL = "http://100.102.24.16:8420"; // fujitsu, via Tailscale

async function fetchToday() {
  const req = new Request(`${SERVER_URL}/api/today`);
  req.timeoutInterval = 8;
  try {
    return await req.loadJSON();
  } catch (e) {
    return null;
  }
}

function colorFor(value, thresholds = [40, 70]) {
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

  drawArc(0, 360, new Color("#2a2a2a"));
  const clamped = Math.max(0, Math.min(100, pct || 0));
  if (clamped > 0) {
    drawArc(0, 360 * (clamped / 100), color);
  }

  ctx.setTextColor(Color.white());
  ctx.setFont(Font.boldSystemFont(size * 0.24));
  ctx.setTextAlignedCenter();
  ctx.drawTextInRect(displayText, new Rect(0, center - size * 0.14, size, size * 0.3));

  return ctx.getImage();
}

function addRingColumn(stack, label, pct, displayText, color, size = 62) {
  const col = stack.addStack();
  col.layoutVertically();
  col.centerAlignContent();
  const img = drawRingImage(size, pct, color, displayText);
  const iw = col.addImage(img);
  iw.imageSize = new Size(size, size);
  col.addSpacer(3);
  const lbl = col.addText(label);
  lbl.font = Font.systemFont(10);
  lbl.textColor = Color.gray();
  lbl.centerAlignText();
}

async function createWidget() {
  const data = await fetchToday();
  const widget = new ListWidget();
  widget.backgroundColor = new Color("#111111");
  widget.setPadding(12, 12, 12, 12);
  widget.url = SERVER_URL;

  const stale = data && data.stale;
  const title = widget.addText(data ? (stale ? "Today (stale)" : "Today") : "No data");
  title.font = Font.mediumSystemFont(12);
  title.textColor = stale ? new Color("#f5a623") : Color.gray();
  widget.addSpacer(6);

  const row = widget.addStack();
  row.layoutHorizontally();
  row.centerAlignContent();

  if (data) {
    const strain = data.strain_score;
    const target = data.target_strain;
    const energy = data.body_battery;
    const sleep = data.sleep_score;

    addRingColumn(row, "Strain", strain, strain != null ? `${Math.round(strain)}` : "--", colorFor(strain));
    row.addSpacer();
    addRingColumn(row, "Target", target, target != null ? `${Math.round(target)}` : "--", new Color("#5b8def"));
    row.addSpacer();
    addRingColumn(row, "Energy", energy, energy != null ? `${Math.round(energy)}` : "--", colorFor(energy));
    row.addSpacer();
    addRingColumn(row, "Sleep", sleep, sleep != null ? `${Math.round(sleep)}` : "--", colorFor(sleep));

    widget.addSpacer(8);
    const rec = widget.addText(data.recommendation_detail || "");
    rec.font = Font.systemFont(11);
    rec.textColor = Color.white();
    rec.lineLimit = 2;

    const steps = data.steps;
    const sleepHours = data.sleep_recommendation_hours;
    const footerBits = [];
    if (steps) footerBits.push(`${Math.round(steps / 1000)}k steps`);
    if (sleepHours) footerBits.push(`sleep target ${sleepHours}h`);
    if (footerBits.length) {
      widget.addSpacer(4);
      const footer = widget.addText(footerBits.join(" · "));
      footer.font = Font.systemFont(10);
      footer.textColor = Color.gray();
    }
  } else {
    row.addSpacer();
    const err = row.addText("Can't reach server\n(check Tailscale)");
    err.font = Font.systemFont(11);
    err.textColor = Color.red();
    row.addSpacer();
  }

  widget.refreshAfterDate = new Date(Date.now() + 10 * 60 * 1000);
  return widget;
}

if (config.runsInWidget) {
  const widget = await createWidget();
  Script.setWidget(widget);
} else {
  // Tapped directly, or the widget's interaction is set to "Run Script":
  // show a fullscreen in-app view (Scriptable's own WebView, no Safari
  // chrome) instead of previewing the tile or jumping out to the browser.
  const webView = new WebView();
  await webView.loadURL(SERVER_URL);
  await webView.present(true);
}
Script.complete();
