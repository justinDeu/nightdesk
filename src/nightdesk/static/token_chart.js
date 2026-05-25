// Hover tooltip for the "Daily tokens by model" stacked bar chart on
// /analytics. Each bar carries data-day-index; on hover we look up that day's
// per-model breakdown and render total + per-model rows into a floating tip
// positioned within the chart card (clamped so it never spills off the edge).
(function () {
  "use strict";

  function parseIsland(id) {
    var el = document.getElementById(id);
    if (!el) return null;
    try {
      return JSON.parse(el.textContent || "null");
    } catch (e) {
      return null;
    }
  }

  function fmtTokens(n) {
    n = n || 0;
    if (n >= 1000000) return (n / 1000000).toFixed(2) + "M";
    if (n >= 1000) return (n / 1000).toFixed(1) + "k";
    return String(n);
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  function init() {
    var chart = document.getElementById("daily-token-chart");
    var bars = document.getElementById("daily-bars");
    var tip = document.getElementById("token-chart-tip");
    if (!chart || !bars || !tip) return;

    var days = parseIsland("daily-usage-data") || [];
    var legend = parseIsland("model-legend-data") || [];

    function rowsFor(day) {
      var byModel = (day && day.by_model) || {};
      var html = "";
      for (var i = 0; i < legend.length; i++) {
        var m = legend[i];
        var tk = byModel[m.model] || 0;
        if (!tk) continue;
        html +=
          '<div class="flex items-center justify-between gap-3">' +
          '<span class="inline-flex items-center gap-1.5">' +
          '<span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:' +
          m.color +
          '"></span>' +
          escapeHtml(m.model) +
          "</span>" +
          '<span class="font-mono">' +
          fmtTokens(tk) +
          "</span></div>";
      }
      if (!html) html = '<div class="text-fg-muted">no tokens</div>';
      return html;
    }

    function render(day) {
      return (
        '<div class="font-medium mb-1">' +
        escapeHtml(day.date) +
        "</div>" +
        '<div class="flex items-center justify-between gap-3 mb-1">' +
        '<span class="text-fg-muted">Total</span>' +
        '<span class="font-mono">' +
        fmtTokens(day.total_tokens) +
        "</span></div>" +
        rowsFor(day)
      );
    }

    function position(evt) {
      var rect = chart.getBoundingClientRect();
      var x = evt.clientX - rect.left + 12;
      var y = evt.clientY - rect.top + 12;
      var tw = tip.offsetWidth;
      var th = tip.offsetHeight;
      if (x + tw > rect.width) x = Math.max(0, evt.clientX - rect.left - tw - 12);
      if (y + th > rect.height) y = Math.max(0, evt.clientY - rect.top - th - 12);
      tip.style.left = x + "px";
      tip.style.top = y + "px";
    }

    var cols = bars.querySelectorAll("[data-day-index]");
    cols.forEach(function (col) {
      var idx = parseInt(col.getAttribute("data-day-index"), 10);
      col.addEventListener("mouseenter", function (e) {
        var day = days[idx];
        if (!day) return;
        tip.innerHTML = render(day);
        tip.classList.remove("hidden");
        position(e);
      });
      col.addEventListener("mousemove", position);
      col.addEventListener("mouseleave", function () {
        tip.classList.add("hidden");
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
