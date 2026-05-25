// Work-windows editor + live resolved-schedule view.
//
// Each window is a card: label, day chips (Mon=0..Sun=6 -> day_mask bit 1<<d,
// matching the scheduler), a time range or "All day" (00:00->00:00), and a
// max-parallel footer. Cards serialize to the hidden #windows_json field; the
// detected IANA timezone goes to #schedule_timezone. The resolved view overlays
// all windows per weekday and shows the effective capacity (most permissive,
// matching capacity_for) per time segment.

(function () {
  var DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

  var editor = document.getElementById("windows-editor");
  if (!editor) return;
  var addBtn = document.getElementById("add-window");
  var hidden = document.getElementById("windows_json");
  var tzField = document.getElementById("schedule_timezone");
  var resolved = document.getElementById("resolved-schedule");
  var form = editor.closest("form");

  var browserTz = "UTC";
  try { browserTz = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; } catch (e) {}
  if (tzField) tzField.value = browserTz;
  var tzNameEl = document.getElementById("tz-name");
  if (tzNameEl) tzNameEl.textContent = browserTz;

  function daysToMask(days) {
    return days.reduce(function (m, d) { return m | (1 << d); }, 0);
  }
  function maskToDays(mask) {
    var o = []; for (var d = 0; d < 7; d++) if (mask & (1 << d)) o.push(d); return o;
  }

  var CHIP = "px-2 py-0.5 text-xs border border-border rounded hover:bg-bg-elev-2 " +
    "aria-[pressed=true]:bg-accent aria-[pressed=true]:text-bg aria-[pressed=true]:border-accent";
  var INPUT = "px-2 py-1 bg-bg-elev-2 border border-border rounded text-fg font-mono";

  function cardEl(w) {
    var allDay = w.start === w.end;
    var card = document.createElement("div");
    card.className = "border border-border rounded p-3 flex flex-col gap-2";
    card.dataset.window = "1";

    var head = document.createElement("div");
    head.className = "flex items-center gap-2";
    var label = document.createElement("input");
    label.type = "text"; label.value = w.label || "";
    label.placeholder = "window name";
    label.className = INPUT + " flex-1 font-medium font-sans";
    label.dataset.f = "label";
    var remove = document.createElement("button");
    remove.type = "button"; remove.textContent = "✕";
    remove.className = "px-2 py-1 text-xs text-fg-muted hover:text-danger";
    remove.addEventListener("click", function () { card.remove(); sync(); });
    head.appendChild(label); head.appendChild(remove);
    card.appendChild(head);

    var dayRow = document.createElement("div");
    dayRow.className = "flex flex-wrap gap-1";
    var selected = {}; maskToDays(w.day_mask).forEach(function (d) { selected[d] = true; });
    DAYS.forEach(function (name, d) {
      var chip = document.createElement("button");
      chip.type = "button"; chip.textContent = name; chip.className = CHIP;
      chip.dataset.day = d;
      chip.setAttribute("aria-pressed", selected[d] ? "true" : "false");
      chip.addEventListener("click", function () {
        chip.setAttribute("aria-pressed", chip.getAttribute("aria-pressed") === "true" ? "false" : "true");
        sync();
      });
      dayRow.appendChild(chip);
    });
    card.appendChild(dayRow);

    var timeRow = document.createElement("div");
    timeRow.className = "flex items-center gap-2 text-sm";
    var allDayLabel = document.createElement("label");
    allDayLabel.className = "flex items-center gap-1";
    var allDayBox = document.createElement("input");
    allDayBox.type = "checkbox"; allDayBox.checked = allDay; allDayBox.dataset.f = "allday";
    allDayLabel.appendChild(allDayBox);
    allDayLabel.appendChild(document.createTextNode("All day"));
    var fromI = document.createElement("input");
    fromI.type = "time"; fromI.value = allDay ? "" : w.start; fromI.className = INPUT; fromI.dataset.f = "start";
    var toI = document.createElement("input");
    toI.type = "time"; toI.value = allDay ? "" : w.end; toI.className = INPUT; toI.dataset.f = "end";
    var sep = document.createElement("span"); sep.textContent = "to"; sep.className = "text-fg-muted";
    function applyAllDay() {
      var on = allDayBox.checked;
      fromI.disabled = on; toI.disabled = on;
      fromI.style.opacity = on ? "0.4" : "1"; toI.style.opacity = on ? "0.4" : "1";
    }
    allDayBox.addEventListener("change", function () { applyAllDay(); sync(); });
    timeRow.appendChild(allDayLabel);
    timeRow.appendChild(fromI); timeRow.appendChild(sep); timeRow.appendChild(toI);
    card.appendChild(timeRow);
    applyAllDay();

    var foot = document.createElement("div");
    foot.className = "flex items-center gap-2 text-xs text-fg-muted border-t border-border pt-2";
    foot.appendChild(document.createTextNode("Up to "));
    var cap = document.createElement("input");
    cap.type = "number"; cap.min = "1"; cap.max = "16"; cap.value = w.max_parallel || 1;
    cap.className = INPUT + " w-16"; cap.dataset.f = "max_parallel";
    foot.appendChild(cap);
    foot.appendChild(document.createTextNode(" tickets in parallel"));
    card.appendChild(foot);

    card.querySelectorAll("input").forEach(function (el) {
      el.addEventListener("input", sync);
    });
    return card;
  }

  function readCard(card) {
    var allday = card.querySelector("[data-f=allday]").checked;
    var start = card.querySelector("[data-f=start]").value || "00:00";
    var end = card.querySelector("[data-f=end]").value || "00:00";
    if (allday) { start = "00:00"; end = "00:00"; }
    var days = [];
    card.querySelectorAll("[data-day][aria-pressed=true]").forEach(function (c) {
      days.push(+c.dataset.day);
    });
    return {
      label: card.querySelector("[data-f=label]").value || "",
      day_mask: daysToMask(days),
      start: start,
      end: end,
      max_parallel: Math.max(1, Math.min(+card.querySelector("[data-f=max_parallel]").value || 1, 16))
    };
  }

  function readAll() {
    return Array.prototype.map.call(editor.querySelectorAll("[data-window]"), readCard);
  }

  function toMin(hhmm) {
    var p = (hhmm || "00:00").split(":"); return (+p[0]) * 60 + (+p[1]);
  }
  function fmtMin(m) {
    m = ((m % 1440) + 1440) % 1440;
    var h = Math.floor(m / 60), mm = m % 60;
    var ap = h < 12 ? "AM" : "PM", h12 = h % 12 || 12;
    return h12 + ":" + String(mm).padStart(2, "0") + " " + ap;
  }

  // Intervals (in minutes) a window covers on a given weekday.
  function windowIntervalsForDay(w, day) {
    if (!(w.day_mask & (1 << day))) return [];
    var s = toMin(w.start), e = toMin(w.end);
    if (s === e) return [[0, 1440]];        // all day
    if (s < e) return [[s, e]];
    return [[s, 1440], [0, e]];             // wraps midnight (tail counts on same weekday)
  }

  // Resolve one weekday to merged segments: [{a,b,cap,labels}] covering 0..1440.
  function resolveDay(day, windows) {
    var bounds = { 0: true, 1440: true };
    var contribs = [];
    windows.forEach(function (w) {
      windowIntervalsForDay(w, day).forEach(function (iv) {
        bounds[iv[0]] = true; bounds[iv[1]] = true;
        contribs.push({ a: iv[0], b: iv[1], cap: w.max_parallel, label: w.label || "(unnamed)" });
      });
    });
    var pts = Object.keys(bounds).map(Number).sort(function (x, y) { return x - y; });
    var segs = [];
    for (var i = 0; i < pts.length - 1; i++) {
      var a = pts[i], b = pts[i + 1];
      var cover = contribs.filter(function (c) { return c.a <= a && c.b >= b; });
      var cap = 0, labels = [];
      cover.forEach(function (c) { cap = Math.max(cap, c.cap); if (labels.indexOf(c.label) < 0) labels.push(c.label); });
      segs.push({ a: a, b: b, cap: cap, labels: labels });
    }
    // Merge adjacent segments with the same cap + labels.
    var merged = [];
    segs.forEach(function (s) {
      var last = merged[merged.length - 1];
      if (last && last.cap === s.cap && last.labels.join(",") === s.labels.join(",")) {
        last.b = s.b;
      } else {
        merged.push({ a: s.a, b: s.b, cap: s.cap, labels: s.labels.slice() });
      }
    });
    return merged;
  }

  function renderResolved(windows) {
    if (!resolved) return;
    resolved.innerHTML = "";
    var perDay = [];
    for (var d = 0; d < 7; d++) perDay.push(resolveDay(d, windows));

    // Collapse consecutive identical days into ranges.
    var groups = [];
    for (var d2 = 0; d2 < 7; d2++) {
      var sig = JSON.stringify(perDay[d2]);
      var last = groups[groups.length - 1];
      if (last && last.sig === sig) { last.end = d2; }
      else groups.push({ start: d2, end: d2, sig: sig, segs: perDay[d2] });
    }

    groups.forEach(function (g) {
      var row = document.createElement("div");
      row.className = "flex items-center gap-2 text-xs";
      var name = document.createElement("div");
      name.className = "w-16 shrink-0 text-fg-muted";
      name.textContent = g.start === g.end ? DAYS[g.start] : DAYS[g.start] + "–" + DAYS[g.end];
      var bar = document.createElement("div");
      bar.className = "flex flex-1 h-9 rounded overflow-hidden border border-border";
      g.segs.forEach(function (s, idx) {
        var cell = document.createElement("div");
        cell.style.flexGrow = (s.b - s.a);
        // A 2px background-colored left edge "breaks" the block at every
        // transition so adjacent windows read as distinct, not one smear.
        var divider = idx > 0 ? "border-l-2 border-bg " : "";
        cell.className = "flex flex-col items-center justify-center overflow-hidden " +
          "whitespace-nowrap px-1 leading-tight " + divider +
          (s.cap > 0 ? "bg-accent/30 text-fg" : "bg-bg-elev-2 text-fg-muted");
        // Boundary time = when this segment takes over.
        var t = document.createElement("span");
        t.className = "text-[9px] text-fg-muted font-mono";
        t.textContent = fmtMin(s.a);
        var v = document.createElement("span");
        v.className = "text-[10px]";
        v.textContent = s.cap > 0 ? (s.cap + " · " + s.labels.join(", ")) : "paused";
        cell.appendChild(t); cell.appendChild(v);
        cell.title = fmtMin(s.a) + "–" + fmtMin(s.b) + ": " +
          (s.cap > 0 ? ("capacity " + s.cap + " (" + s.labels.join(", ") + ")") : "paused");
        bar.appendChild(cell);
      });
      row.appendChild(name); row.appendChild(bar);
      resolved.appendChild(row);
    });

    // "Now" line, computed in the browser zone (== configured zone).
    var now = new Date();
    var nowDay = (now.getDay() + 6) % 7;        // JS Sun=0 -> Mon=0
    var nowMin = now.getHours() * 60 + now.getMinutes();
    var active = [], cap = 0;
    windows.forEach(function (w) {
      windowIntervalsForDay(w, nowDay).forEach(function (iv) {
        if (nowMin >= iv[0] && nowMin < iv[1]) {
          cap = Math.max(cap, w.max_parallel);
          if (active.indexOf(w.label || "(unnamed)") < 0) active.push(w.label || "(unnamed)");
        }
      });
    });
    var nowEl = document.createElement("div");
    nowEl.className = "text-xs text-fg-muted mt-1";
    nowEl.textContent = "Now: " + (cap > 0
      ? ("capacity " + cap + " via " + active.join(", "))
      : "paused (no window active — run-now still works)");
    resolved.appendChild(nowEl);
  }

  function sync() {
    var windows = readAll();
    if (hidden) hidden.value = JSON.stringify(windows);
    renderResolved(windows);
  }

  function addCard(w) {
    editor.appendChild(cardEl(w));
  }

  if (addBtn) {
    addBtn.addEventListener("click", function () {
      addCard({ label: "", day_mask: 127, start: "00:00", end: "00:00", max_parallel: 1 });
      sync();
    });
  }
  if (form) form.addEventListener("submit", sync);

  // Initial render from the JSON island.
  var data = [];
  var island = document.getElementById("windows-data");
  if (island) { try { data = JSON.parse(island.textContent || "[]"); } catch (e) { data = []; } }
  data.forEach(addCard);
  sync();
})();
