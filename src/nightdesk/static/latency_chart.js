// "Median turn latency by model" multi-line chart on /analytics.
//
// Server-side: analytics.daily_latency_by_model_series emits one entry per
// day {date, by_model: {model: median_seconds}}; latency_model_legend carries
// the {model, color} pairs (colors shared with every other analytics chart).
// This script renders an SVG line chart (one polyline per model, split at days
// with no sample for that model) and a per-day hover tooltip — the same UX as
// token_chart.js. The SVG stretches to the card width (preserveAspectRatio
// "none") and polylines use non-scaling-stroke so their weight stays constant.
(function () {
  "use strict";

  var W = 640, H = 192;
  var PAD_L = 10, PAD_R = 10, PAD_T = 12, PAD_B = 14;
  var plotW = W - PAD_L - PAD_R;
  var plotH = H - PAD_T - PAD_B;

  function parseIsland(id) {
    var el = document.getElementById(id);
    if (!el) return null;
    try {
      return JSON.parse(el.textContent || "null");
    } catch (e) {
      return null;
    }
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  // Mirror the Jinja fmt_dur macro: none -> "—", >=3600 -> Xh, >=60 -> Xm, else Xs.
  function fmtDur(secs) {
    if (secs === null || secs === undefined || isNaN(secs)) return "—";
    if (secs >= 3600) return (secs / 3600).toFixed(1) + "h";
    if (secs >= 60) return (secs / 60).toFixed(1) + "m";
    return Math.round(secs) + "s";
  }

  function el(name, attrs) {
    var n = document.createElementNS("http://www.w3.org/2000/svg", name);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        n.setAttribute(k, attrs[k]);
      });
    }
    return n;
  }

  function init() {
    var host = document.getElementById("latency-trend-svg");
    var card = document.getElementById("latency-trend-chart");
    var tip = document.getElementById("latency-chart-tip");
    if (!host || !card || !tip) return;

    var series = parseIsland("latency-series-data") || [];
    var legend = parseIsland("latency-legend-data") || [];
    var n = series.length;
    if (n === 0 || legend.length === 0) return;

    var maxV = 0;
    series.forEach(function (d) {
      var bm = (d && d.by_model) || {};
      Object.keys(bm).forEach(function (m) {
        var v = bm[m];
        if (typeof v === "number" && v > maxV) maxV = v;
      });
    });
    if (maxV <= 0) return;

    var x = function (i) {
      if (n <= 1) return PAD_L + plotW / 2;
      return PAD_L + (i / (n - 1)) * plotW;
    };
    var y = function (v) {
      return PAD_T + (1 - v / maxV) * plotH;
    };

    var svg = el("svg", {
      viewBox: "0 0 " + W + " " + H,
      preserveAspectRatio: "none",
      style: "width:100%;height:100%;display:block;",
    });

    // Baseline + mid gridlines (purely visual; non-scaling keeps them crisp).
    [0, 0.5, 1].forEach(function (f) {
      svg.appendChild(el("line", {
        x1: PAD_L, x2: W - PAD_R,
        y1: PAD_T + f * plotH, y2: PAD_T + f * plotH,
        stroke: "currentColor", "stroke-opacity": f === 1 ? "0.18" : "0.08",
        "vector-effect": "non-scaling-stroke",
      }));
    });

    // One polyline per model, split into contiguous runs across sample-less days.
    legend.forEach(function (m) {
      var segment = [];
      series.forEach(function (d, i) {
        var v = d && d.by_model ? d.by_model[m.model] : undefined;
        if (typeof v === "number") {
          segment.push(x(i) + "," + y(v));
        } else if (segment.length > 1) {
          svg.appendChild(el("polyline", {
            points: segment.join(" "), fill: "none", stroke: m.color,
            "stroke-width": "1.5", "stroke-linejoin": "round",
            "stroke-linecap": "round", "vector-effect": "non-scaling-stroke",
          }));
          segment = [];
        } else {
          segment = [];
        }
      });
      if (segment.length > 1) {
        svg.appendChild(el("polyline", {
          points: segment.join(" "), fill: "none", stroke: m.color,
          "stroke-width": "1.5", "stroke-linejoin": "round",
          "stroke-linecap": "round", "vector-effect": "non-scaling-stroke",
        }));
      }
    });

    host.appendChild(svg);

    // Per-day hover columns (invisible hit areas) feeding the tooltip.
    var colW = n > 1 ? plotW / n : plotW;
    function rowsFor(day) {
      var bm = (day && day.by_model) || {};
      var html = "";
      legend.forEach(function (m) {
        var v = bm[m.model];
        if (typeof v !== "number") return;
        html +=
          '<div class="flex items-center justify-between gap-3">' +
          '<span class="inline-flex items-center gap-1.5">' +
          '<span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:' +
          m.color + '"></span>' + escapeHtml(m.model) + "</span>" +
          '<span class="font-mono">' + fmtDur(v) + "</span></div>";
      });
      return html || '<div class="text-fg-muted">no samples</div>';
    }

    var layer = el("g");
    series.forEach(function (d, i) {
      var rect = el("rect", {
        x: x(i) - colW / 2, y: PAD_T, width: colW, height: plotH,
        fill: "transparent",
      });
      rect.addEventListener("mouseenter", function (e) {
        tip.innerHTML =
          '<div class="font-medium mb-1">' + escapeHtml(d.date) + "</div>" +
          rowsFor(d);
        tip.classList.remove("hidden");
        position(e);
      });
      rect.addEventListener("mousemove", position);
      rect.addEventListener("mouseleave", function () {
        tip.classList.add("hidden");
      });
      layer.appendChild(rect);
    });
    svg.appendChild(layer);

    function position(e) {
      var rect = card.getBoundingClientRect();
      var px = e.clientX - rect.left + 12;
      var py = e.clientY - rect.top + 12;
      var tw = tip.offsetWidth;
      var th = tip.offsetHeight;
      if (px + tw > rect.width) px = Math.max(0, e.clientX - rect.left - tw - 12);
      if (py + th > rect.height) py = Math.max(0, e.clientY - rect.top - th - 12);
      tip.style.left = px + "px";
      tip.style.top = py + "px";
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
