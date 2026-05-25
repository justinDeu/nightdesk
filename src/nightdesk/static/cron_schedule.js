// Cron schedule builder: chips pick the cadence, the user fills in the times,
// and a 5-field cron string is kept as ground truth in the raw "schedule" input.
// A live human description + server-computed next-fire preview render alongside.
//
// Round-trip invariants (buildCron(parseCron(x)) === x):
//   */15 * * * *     | 30 */2 * * *   | 0 9 * * *
//   0 9 * * 1,2,3,4,5 | 0 9 15 * *
//
// Day-index trap: the UI uses Mon=0..Sun=6 (same as the scheduler's day_mask).
// Cron's day-of-week field uses Sun=0..Sat=6, so we convert only here:
// to cron (d+1)%7, from cron (cd+6)%7.

(function () {
  function hhmm(h, m) {
    return String(+h).padStart(2, "0") + ":" + String(+m).padStart(2, "0");
  }

  function buildCron(state) {
    var t = (state.time || "00:00").split(":");
    var h = +t[0] || 0, m = +t[1] || 0;
    switch (state.cadence) {
      case "minutes":
        return "*/" + (state.minutes || 1) + " * * * *";
      case "hourly":
        return (state.atMinute || 0) + " */" + (state.hours || 1) + " * * *";
      case "daily":
        return m + " " + h + " * * *";
      case "weekly":
        var dows = (state.days || []).slice().map(function (d) { return (d + 1) % 7; })
          .sort(function (a, b) { return a - b; }).join(",");
        return m + " " + h + " * * " + (dows || "*");
      case "monthly":
        return m + " " + h + " " + (state.dom || 1) + " * *";
      default:
        return state.raw || "";
    }
  }

  function parseCron(expr) {
    var f = (expr || "").trim().split(/\s+/);
    if (f.length !== 5) return null;
    var min = f[0], hr = f[1], dom = f[2], mon = f[3], dow = f[4];
    var num = function (s) { return /^\d+$/.test(s); };
    if (mon !== "*") return null;
    if (/^\*\/(\d+)$/.test(min) && hr === "*" && dom === "*" && dow === "*")
      return { cadence: "minutes", minutes: +min.split("/")[1] };
    if (num(min) && /^\*\/(\d+)$/.test(hr) && dom === "*" && dow === "*")
      return { cadence: "hourly", atMinute: +min, hours: +hr.split("/")[1] };
    if (num(min) && num(hr) && dom === "*" && dow === "*")
      return { cadence: "daily", time: hhmm(hr, min) };
    if (num(min) && num(hr) && dom === "*" && /^[0-6](,[0-6])*$/.test(dow))
      return {
        cadence: "weekly", time: hhmm(hr, min),
        days: dow.split(",").map(function (d) { return (+d + 6) % 7; })
      };
    if (num(min) && num(hr) && num(dom) && dow === "*")
      return { cadence: "monthly", time: hhmm(hr, min), dom: +dom };
    return null;
  }

  var DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

  function fmt12(t) {
    var p = (t || "00:00").split(":");
    var h = +p[0], m = +p[1];
    var ap = h < 12 ? "AM" : "PM";
    var h12 = h % 12; if (h12 === 0) h12 = 12;
    return h12 + ":" + String(m).padStart(2, "0") + " " + ap;
  }

  function describe(state) {
    switch (state.cadence) {
      case "minutes": return "Every " + (state.minutes || 1) + " minute(s)";
      case "hourly":
        return "Every " + (state.hours || 1) + " hour(s) at minute " + (state.atMinute || 0);
      case "daily": return "Daily at " + fmt12(state.time);
      case "weekly":
        var days = (state.days || []).slice().sort(function (a, b) { return a - b; })
          .map(function (d) { return DAY_NAMES[d]; }).join(", ");
        return "Weekly on " + (days || "—") + " at " + fmt12(state.time);
      case "monthly":
        return "Monthly on day " + (state.dom || 1) + " at " + fmt12(state.time);
      default: return "Custom schedule";
    }
  }

  function initBuilder(root) {
    if (root.dataset.cronInit === "1") return;  // idempotent across re-scans
    root.dataset.cronInit = "1";
    var raw = root.querySelector("[data-cron-raw]");
    var descEl = root.querySelector("[data-cron-description]");
    var previewEl = root.querySelector("[data-cron-preview]");
    var advanced = root.querySelector("[data-cron-advanced]");
    var form = root.closest("form");
    if (!raw) return;

    // Prefill an empty timezone input with the browser zone so the preview and
    // the eventual cron job both default sensibly.
    var tzInput = form && form.querySelector('[name="timezone"]');
    if (tzInput && !tzInput.value) {
      try { tzInput.value = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (e) {}
    }

    var current = "minutes";
    var previewTimer = null;

    function tzValue() {
      var el = form && form.querySelector('[name="timezone"]');
      var v = el && el.value ? el.value.trim() : "";
      return v || "UTC";
    }

    function setCadence(cadence) {
      current = cadence;
      root.querySelectorAll("[data-cadence]").forEach(function (chip) {
        chip.setAttribute("aria-pressed", chip.dataset.cadence === cadence ? "true" : "false");
      });
      root.querySelectorAll("[data-fields]").forEach(function (block) {
        block.hidden = block.dataset.fields !== cadence;
      });
      if (advanced) advanced.open = cadence === "advanced";
    }

    function activeBlock() {
      return root.querySelector('[data-fields="' + current + '"]');
    }

    function readState() {
      var st = { cadence: current };
      var block = activeBlock();
      if (current === "minutes") {
        st.minutes = +(block.querySelector("[data-f=minutes]").value) || 1;
      } else if (current === "hourly") {
        st.hours = +(block.querySelector("[data-f=hours]").value) || 1;
        st.atMinute = +(block.querySelector("[data-f=atMinute]").value) || 0;
      } else if (current === "daily") {
        st.time = block.querySelector("[data-f=time]").value || "00:00";
      } else if (current === "weekly") {
        st.time = block.querySelector("[data-f=time]").value || "00:00";
        st.days = [];
        block.querySelectorAll("[data-day][aria-pressed=true]").forEach(function (c) {
          st.days.push(+c.dataset.day);
        });
      } else if (current === "monthly") {
        st.time = block.querySelector("[data-f=time]").value || "00:00";
        st.dom = +(block.querySelector("[data-f=dom]").value) || 1;
      } else {
        st.raw = raw.value;
      }
      return st;
    }

    function fillFields(state) {
      var block = root.querySelector('[data-fields="' + state.cadence + '"]');
      if (!block) return;
      if (state.cadence === "minutes") {
        block.querySelector("[data-f=minutes]").value = state.minutes;
      } else if (state.cadence === "hourly") {
        block.querySelector("[data-f=hours]").value = state.hours;
        block.querySelector("[data-f=atMinute]").value = state.atMinute;
      } else if (state.cadence === "daily") {
        block.querySelector("[data-f=time]").value = state.time;
      } else if (state.cadence === "weekly") {
        block.querySelector("[data-f=time]").value = state.time;
        var set = {}; (state.days || []).forEach(function (d) { set[d] = true; });
        block.querySelectorAll("[data-day]").forEach(function (c) {
          c.setAttribute("aria-pressed", set[+c.dataset.day] ? "true" : "false");
        });
      } else if (state.cadence === "monthly") {
        block.querySelector("[data-f=time]").value = state.time;
        block.querySelector("[data-f=dom]").value = state.dom;
      }
    }

    function refreshPreview() {
      if (!previewEl) return;
      if (previewTimer) clearTimeout(previewTimer);
      previewTimer = setTimeout(function () {
        var body = "schedule=" + encodeURIComponent(raw.value) +
          "&timezone=" + encodeURIComponent(tzValue());
        fetch("/cron/preview", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: body
        }).then(function (r) { return r.text(); })
          .then(function (html) { previewEl.innerHTML = html; })
          .catch(function () { });
      }, 350);
    }

    function recompute() {
      if (current === "advanced") {
        if (descEl) descEl.textContent = "Custom schedule";
      } else {
        var st = readState();
        raw.value = buildCron(st);
        if (descEl) descEl.textContent = describe(st);
      }
      refreshPreview();
    }

    // Chip clicks.
    root.querySelectorAll("[data-cadence]").forEach(function (chip) {
      chip.addEventListener("click", function () {
        setCadence(chip.dataset.cadence);
        recompute();
      });
    });
    // Day toggles.
    root.querySelectorAll("[data-day]").forEach(function (c) {
      c.addEventListener("click", function () {
        c.setAttribute("aria-pressed", c.getAttribute("aria-pressed") === "true" ? "false" : "true");
        recompute();
      });
    });
    // Field edits.
    root.querySelectorAll("[data-f]").forEach(function (el) {
      el.addEventListener("input", recompute);
    });
    // Direct edits to the raw cron in Advanced.
    raw.addEventListener("input", function () {
      if (current === "advanced") {
        if (descEl) descEl.textContent = "Custom schedule";
        refreshPreview();
      }
    });

    // Initial state: parse the existing cron back into a chip, else Advanced.
    var parsed = parseCron(raw.value);
    if (parsed) {
      setCadence(parsed.cadence);
      fillFields(parsed);
      recompute();
    } else if (raw.value.trim()) {
      setCadence("advanced");
      if (descEl) descEl.textContent = "Custom schedule";
      refreshPreview();
    } else {
      setCadence("minutes");
      recompute();
    }
  }

  function init() {
    document.querySelectorAll("[data-cron-builder]").forEach(initBuilder);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
  // The edit/new form is swapped into the two-pane layout via HTMX, which does
  // not fire DOMContentLoaded — re-scan after each swap.
  document.body.addEventListener("htmx:afterSwap", init);

  // Expose for tests/debugging.
  window.cronBuilder = { buildCron: buildCron, parseCron: parseCron, describe: describe };
})();
