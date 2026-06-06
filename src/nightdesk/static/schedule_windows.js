// Work-windows editor + live resolved-schedule view.
//
// Each window is a card: label, day chips (Mon=0..Sun=6 -> day_mask bit 1<<d,
// matching the scheduler), a time range or "All day" (00:00->00:00), and a
// max-parallel footer. Cards serialize to the hidden #windows_json field in
// DOM order, and that order IS the precedence: cards are drag-reorderable and
// the server stores their order as `position`. The detected IANA timezone goes
// to #schedule_timezone. The resolved view overlays all windows per weekday and
// shows the effective capacity per time segment, where an overlap resolves to
// the highest card in the list (first in order — matching capacity_for).

(function () {
  var DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

  var editor = document.getElementById("windows-editor");
  if (!editor) return;
  var addBtn = document.getElementById("add-window");
  var hidden = document.getElementById("windows_json");
  var tzField = document.getElementById("schedule_timezone");
  var baselineSelect = document.getElementById("resolved-schedule-baseline");
  var resolvedAxis = document.getElementById("resolved-schedule-axis");
  var resolved = document.getElementById("resolved-schedule");
  var tooltip = document.getElementById("resolved-schedule-tooltip");
  var form = editor.closest("form");
  var windowSeq = 0;

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

  function nextWindowId() {
    windowSeq += 1;
    return "w" + windowSeq;
  }

  function cardEl(w) {
    var allDay = w.start === w.end;
    var card = document.createElement("div");
    card.className = "border border-border rounded p-3 flex flex-col gap-2";
    card.dataset.window = "1";
    card.dataset.windowId = w._displayId || nextWindowId();

    var head = document.createElement("div");
    head.className = "flex items-center gap-2";
    var grip = document.createElement("button");
    grip.type = "button";
    grip.textContent = "⠿";
    grip.dataset.windowHandle = "1";
    grip.setAttribute("aria-label", "Drag to reorder window (sets overlap precedence)");
    grip.title = "Drag to reorder — higher windows win when windows overlap";
    grip.className = "cursor-grab select-none px-1 text-fg-muted hover:text-fg leading-none";
    head.appendChild(grip);
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
      _displayId: card.dataset.windowId,
      label: (card.querySelector("[data-f=label]").value || "").trim(),
      day_mask: daysToMask(days),
      start: start,
      end: end,
      max_parallel: Math.max(1, Math.min(+card.querySelector("[data-f=max_parallel]").value || 1, 16))
    };
  }

  function readAll() {
    return Array.prototype.map.call(editor.querySelectorAll("[data-window]"), readCard);
  }

  function toPersistedWindows(windows) {
    return windows.map(function (w) {
      return {
        label: w.label,
        day_mask: w.day_mask,
        start: w.start,
        end: w.end,
        max_parallel: w.max_parallel
      };
    });
  }

  function formatDaySummary(mask) {
    var days = maskToDays(mask);
    if (!days.length) return "No days";
    if (days.length === 7) return "Mon–Sun";
    var ranges = [];
    var start = days[0];
    var prev = days[0];
    for (var i = 1; i <= days.length; i++) {
      var cur = days[i];
      if (cur === prev + 1) {
        prev = cur;
        continue;
      }
      ranges.push(start === prev ? DAYS[start] : DAYS[start] + "–" + DAYS[prev]);
      start = cur;
      prev = cur;
    }
    return ranges.join(", ");
  }

  function describeWindow(w) {
    var timeText = w.start === w.end ? "all day" : (fmtMin(toMin(w.start)) + "–" + fmtMin(toMin(w.end)));
    return (w.label || "(unnamed)") + " · " + formatDaySummary(w.day_mask) + " · " + timeText + " · cap " + w.max_parallel;
  }

  function workerLabel(count) {
    return count === 1 ? "1 worker" : (count + " workers");
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

  function pickWinner(cover) {
    // Precedence follows the editor order: the window highest in the list wins
    // when several overlap. `contribs` is built by walking `windows` in DOM
    // order, and `cover` (a filter of it) preserves that order, so cover[0] is
    // the earliest/highest-precedence window covering this segment. Mirrors the
    // server's capacity_for (first matching window by position wins).
    return cover.length ? cover[0] : null;
  }

  // Resolve one weekday to merged winner-only segments: [{a,b,winner}] covering 0..1440.
  function resolveDay(day, windows) {
    var bounds = { 0: true, 1440: true };
    var contribs = [];
    windows.forEach(function (w) {
      windowIntervalsForDay(w, day).forEach(function (iv) {
        bounds[iv[0]] = true; bounds[iv[1]] = true;
        contribs.push({ a: iv[0], b: iv[1], window: w, label: w.label || "(unnamed)" });
      });
    });
    var pts = Object.keys(bounds).map(Number).sort(function (x, y) { return x - y; });
    var segs = [];
    for (var i = 0; i < pts.length - 1; i++) {
      var a = pts[i], b = pts[i + 1];
      var cover = contribs.filter(function (c) { return c.a <= a && c.b >= b; });
      var winner = pickWinner(cover);
      segs.push({ a: a, b: b, winner: winner });
    }
    // Merge adjacent segments only when the winner itself stays the same.
    var merged = [];
    segs.forEach(function (s) {
      var last = merged[merged.length - 1];
      var sameWinner = last && last.winner && s.winner && last.winner.window === s.winner.window;
      if (sameWinner) {
        last.b = s.b;
      } else if (!last && !s.winner) {
        merged.push({ a: s.a, b: s.b, winner: null });
      } else if (last && !last.winner && !s.winner) {
        last.b = s.b;
      } else {
        merged.push({ a: s.a, b: s.b, winner: s.winner });
      }
    });
    return merged;
  }

  function pct(minute) {
    return (minute / 1440) * 100;
  }

  function setStyle(el, styles) {
    Object.keys(styles).forEach(function (key) { el.style[key] = styles[key]; });
  }

  function hideTooltip() {
    if (!tooltip) return;
    tooltip.setAttribute("hidden", "hidden");
  }

  function showTooltip(anchorRect, title, body, clientX, clientY) {
    if (!tooltip) return;
    var titleEl = tooltip.querySelector(".nd-tooltip-title");
    var bodyEl = tooltip.querySelector(".nd-tooltip-body");
    if (titleEl) titleEl.textContent = title;
    if (bodyEl) bodyEl.textContent = body;
    tooltip.removeAttribute("hidden");

    var width = tooltip.offsetWidth || 260;
    var height = tooltip.offsetHeight || 64;
    var x = typeof clientX === "number" ? clientX : (anchorRect.left + anchorRect.right) / 2;
    var y = typeof clientY === "number" ? clientY : anchorRect.top;
    var left = Math.min(window.innerWidth - width - 12, Math.max(12, x - width / 2));
    var top = y - height - 12;
    if (top < 12) top = Math.min(window.innerHeight - height - 12, anchorRect.bottom + 12);
    tooltip.style.left = left + "px";
    tooltip.style.top = top + "px";
  }

  function bindTooltip(target, title, body) {
    if (!tooltip) return;
    function onMove(ev) {
      showTooltip(target.getBoundingClientRect(), title, body, ev.clientX, ev.clientY);
    }
    target.addEventListener("mouseenter", onMove);
    target.addEventListener("mousemove", onMove);
    target.addEventListener("mouseleave", hideTooltip);
    target.addEventListener("focus", function () {
      showTooltip(target.getBoundingClientRect(), title, body);
    });
    target.addEventListener("blur", hideTooltip);
  }

  function syncBaselineOptions(windows) {
    if (!baselineSelect) return;
    var previous = baselineSelect.value;
    baselineSelect.innerHTML = "";

    var empty = document.createElement("option");
    empty.value = "";
    empty.textContent = "Winner-only";
    baselineSelect.appendChild(empty);

    windows.forEach(function (w) {
      var opt = document.createElement("option");
      opt.value = w._displayId;
      opt.textContent = describeWindow(w);
      baselineSelect.appendChild(opt);
    });

    if (previous && windows.some(function (w) { return w._displayId === previous; })) {
      baselineSelect.value = previous;
    }
  }

  function currentBaseline(windows) {
    if (!baselineSelect || !baselineSelect.value) return null;
    return windows.find(function (w) { return w._displayId === baselineSelect.value; }) || null;
  }

  function renderAxis() {
    if (!resolvedAxis) return;
    resolvedAxis.innerHTML = "";

    var shell = document.createElement("div");
    setStyle(shell, {
      display: "grid",
      gridTemplateColumns: "4rem minmax(0, 1fr)",
      gap: "0.5rem",
      alignItems: "end"
    });

    var spacer = document.createElement("div");
    shell.appendChild(spacer);

    var axis = document.createElement("div");
    setStyle(axis, {
      position: "relative",
      height: "1.25rem",
      borderBottom: "1px solid var(--color-border)"
    });

    for (var hour = 0; hour <= 24; hour += 4) {
      var tick = document.createElement("div");
      setStyle(tick, {
        position: "absolute",
        left: hour === 24 ? "100%" : pct(hour * 60) + "%",
        top: "0",
        bottom: "-1px",
        width: "0",
        borderLeft: "1px solid color-mix(in srgb, var(--color-border) 82%, transparent)",
        pointerEvents: "none"
      });
      var label = document.createElement("span");
      label.textContent = fmtMin(hour * 60);
      setStyle(label, {
        position: "absolute",
        left: "0",
        bottom: "0.3rem",
        transform: hour === 24 ? "translateX(-100%)" : "translateX(0.35rem)",
        fontSize: "10px",
        lineHeight: "1",
        fontFamily: "var(--font-mono)",
        color: "var(--color-fg-muted)",
        whiteSpace: "nowrap"
      });
      tick.appendChild(label);
      axis.appendChild(tick);
    }

    shell.appendChild(axis);
    resolvedAxis.appendChild(shell);
  }

  function applyTrackGrid(track) {
    setStyle(track, {
      backgroundImage: "repeating-linear-gradient(to right, transparent 0, transparent calc(16.666% - 1px), color-mix(in srgb, var(--color-border) 82%, transparent) calc(16.666% - 1px), color-mix(in srgb, var(--color-border) 82%, transparent) 16.666%)",
      backgroundSize: "100% 100%"
    });
  }

  function updateBlockLabels(scope) {
    scope.querySelectorAll("[data-block-label]").forEach(function (label) {
      var block = label.parentElement;
      if (!block) return;
      label.style.display = block.getBoundingClientRect().width < 82 ? "none" : "block";
    });
  }

  function blockGeometry(start, end) {
    var insetLeft = start > 0 ? 1 : 0;
    var insetRight = end < 1440 ? 1 : 0;
    return {
      left: "calc(" + pct(start) + "% + " + insetLeft + "px)",
      width: "calc(" + pct(end - start) + "% - " + (insetLeft + insetRight) + "px)"
    };
  }

  function renderBand(track, start, end, styles, attrs) {
    var el = document.createElement("div");
    var geom = blockGeometry(start, end);
    setStyle(el, {
      position: "absolute",
      left: geom.left,
      width: geom.width,
      boxSizing: "border-box"
    });
    setStyle(el, styles);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) { el.setAttribute(key, attrs[key]); });
    }
    track.appendChild(el);
    return el;
  }

  function renderResolved(windows) {
    if (!resolved) return;
    renderAxis();
    resolved.innerHTML = "";
    var baseline = currentBaseline(windows);
    for (var d = 0; d < 7; d++) {
      var segs = resolveDay(d, windows);
      var baselineIntervals = baseline ? windowIntervalsForDay(baseline, d) : [];
      var row = document.createElement("div");
      setStyle(row, {
        display: "grid",
        gridTemplateColumns: "4rem minmax(0, 1fr)",
        gap: "0.5rem",
        alignItems: "center"
      });

      var name = document.createElement("div");
      name.textContent = DAYS[d];
      setStyle(name, {
        color: "var(--color-fg-muted)",
        fontSize: "12px",
        fontWeight: "600",
        textTransform: "uppercase",
        letterSpacing: "0.04em"
      });

      var track = document.createElement("div");
      setStyle(track, {
        position: "relative",
        height: "2.75rem",
        border: "1px solid var(--color-border)",
        borderRadius: "0.5rem",
        backgroundColor: "var(--color-bg-elev-2)",
        overflow: "hidden"
      });
      applyTrackGrid(track);

      baselineIntervals.forEach(function (iv) {
        renderBand(track, iv[0], iv[1], {
          top: "0.8rem",
          height: "1.1rem",
          borderRadius: "999px",
          background: "color-mix(in srgb, var(--color-fg-muted) 14%, transparent)",
          border: "1px solid color-mix(in srgb, var(--color-fg-muted) 20%, transparent)"
        });
      });

      segs.forEach(function (s) {
        if (!s.winner) return;
        if (baseline && s.winner.window === baseline) return;
        var block = document.createElement("div");
        var label = document.createElement("span");
        var winner = s.winner.window;
        var title = s.winner.label;
        block.dataset.block = "1";
        block.tabIndex = 0;
        var geom = blockGeometry(s.a, s.b);
        setStyle(block, {
          position: "absolute",
          left: geom.left,
          width: geom.width,
          top: "0.45rem",
          bottom: "0.45rem",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          padding: "0 0.45rem",
          borderRadius: "0.45rem",
          border: "1px solid color-mix(in srgb, var(--color-accent-strong) 76%, transparent)",
          background: "linear-gradient(180deg, color-mix(in srgb, var(--color-accent) 34%, transparent), color-mix(in srgb, var(--color-accent-strong) 22%, transparent))",
          color: "var(--color-fg)",
          overflow: "hidden",
          whiteSpace: "nowrap",
          textOverflow: "ellipsis",
          boxSizing: "border-box",
          boxShadow: "inset 0 1px 0 color-mix(in srgb, white 14%, transparent), 0 0 0 1px color-mix(in srgb, var(--color-bg) 16%, transparent)"
        });
        label.textContent = title;
        label.dataset.blockLabel = "1";
        setStyle(label, {
          fontSize: "11px",
          lineHeight: "1.1",
          fontWeight: "600",
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
          pointerEvents: "none"
        });
        block.appendChild(label);
        block.setAttribute("aria-label", fmtMin(s.a) + " to " + fmtMin(s.b) + ", " + title + ", capacity " + winner.max_parallel);
        bindTooltip(
          block,
          title || "(unnamed)",
          DAYS[d] + " · " + fmtMin(s.a) + "–" + fmtMin(s.b) + " · " + workerLabel(winner.max_parallel)
        );
        track.appendChild(block);
      });

      row.appendChild(name);
      row.appendChild(track);
      resolved.appendChild(row);
    }

    updateBlockLabels(resolved);

    // "Now" line, computed in the browser zone (== configured zone).
    var now = new Date();
    var nowDay = (now.getDay() + 6) % 7;        // JS Sun=0 -> Mon=0
    var nowMin = now.getHours() * 60 + now.getMinutes();
    var active = resolveDay(nowDay, windows).find(function (s) {
      return s.winner && nowMin >= s.a && nowMin < s.b;
    });
    var nowEl = document.createElement("div");
    nowEl.className = "text-xs text-fg-muted mt-2";
    nowEl.textContent = "Now: " + (active && active.winner
      ? ("capacity " + active.winner.window.max_parallel + " via " + active.winner.label)
      : "paused (no window active — run-now still works)");
    resolved.appendChild(nowEl);
  }

  function sync() {
    var windows = readAll();
    hideTooltip();
    syncBaselineOptions(windows);
    if (hidden) hidden.value = JSON.stringify(toPersistedWindows(windows));
    renderResolved(windows);
  }

  function addCard(w) {
    editor.appendChild(cardEl(w));
  }

  // Drag-to-reorder. The DOM order of cards IS the precedence order: sync()
  // serializes #windows_json in document order and the server stores that as
  // `position`, so dragging a card above another makes its cap win on overlap.
  if (typeof Sortable !== "undefined") {
    new Sortable(editor, {
      animation: 120,
      handle: "[data-window-handle]",
      draggable: "[data-window]",
      ghostClass: "opacity-50",
      onEnd: sync,
    });
  }

  if (addBtn) {
    addBtn.addEventListener("click", function () {
      addCard({ label: "", day_mask: 127, start: "00:00", end: "00:00", max_parallel: 1 });
      sync();
    });
  }
  if (form) form.addEventListener("submit", sync);
  if (baselineSelect) baselineSelect.addEventListener("change", sync);
  window.addEventListener("resize", sync);

  // Initial render from the JSON island.
  var data = [];
  var island = document.getElementById("windows-data");
  if (island) { try { data = JSON.parse(island.textContent || "[]"); } catch (e) { data = []; } }
  data.forEach(addCard);
  sync();
})();
