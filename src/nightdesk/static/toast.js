/* Minimal in-page toast primitive.
 *
 * Exposes window.ndToast(text, opts):
 *   opts.type — 'error' | 'info' | 'success' (default 'info'). Errors use
 *               role="alert", a danger border, and linger longer.
 *   opts.ms   — override the auto-dismiss duration (ms).
 *
 * This is the simple-message variant. bulk_select.js carries its own richer
 * toast with an Undo button (its toast() is private to that module by design,
 * coupled to its undo payload shape); the two share the same look by using the
 * same design-system tokens (var(--color-*)) and the same anchored stack, so a
 * plain toast and an undo toast never look like they came from different apps.
 *
 * Implemented from scratch rather than pulling in a library: a fixed centered
 * stack at the bottom of the viewport, one <div> per toast, auto-dismiss with a
 * click-to-dismiss escape hatch. Resilient to HTMX OOB swaps because it lives
 * outside the polled regions.
 */
(function () {
  if (typeof window === "undefined") return;
  if (window.ndToast) return; // idempotent guard

  var STACK_ID = "nd-toast-stack";

  function stack() {
    var el = document.getElementById(STACK_ID);
    if (el) return el;
    el = document.createElement("div");
    el.id = STACK_ID;
    el.setAttribute("aria-live", "polite");
    el.style.cssText =
      "position:fixed;left:50%;bottom:74px;transform:translateX(-50%);" +
      "z-index:130;display:flex;flex-direction:column;align-items:center;gap:.5rem;" +
      "pointer-events:none;";
    document.body.appendChild(el);
    return el;
  }

  function toast(text, opts) {
    opts = opts || {};
    var type = opts.type || "info";
    var ms = opts.ms || (type === "error" ? 4500 : type === "success" ? 2400 : 3000);
    var accent =
      type === "error" ? "var(--color-danger)" :
      type === "success" ? "var(--color-accent)" :
      "var(--color-info, var(--color-accent))";

    var el = document.createElement("div");
    el.setAttribute("role", type === "error" ? "alert" : "status");
    el.style.cssText =
      "pointer-events:auto;background:var(--color-bg-elev);color:var(--color-fg);" +
      "border:1px solid " + accent + ";border-radius:.375rem;padding:.5rem .875rem;" +
      "font-size:.8125rem;box-shadow:0 8px 20px rgba(0,0,0,.35);" +
      "max-width:min(28rem,90vw);opacity:0;transform:translateY(6px);" +
      "transition:opacity .12s ease, transform .12s ease;cursor:pointer;";
    var msg = document.createElement("span");
    msg.textContent = text;
    el.appendChild(msg);

    var host = stack();
    host.appendChild(el);
    // Defer the transition so the initial opacity:0 paints first.
    (window.requestAnimationFrame || function (cb) { setTimeout(cb, 16); })(function () {
      el.style.opacity = "1";
      el.style.transform = "translateY(0)";
    });

    var timer;
    function dismiss() {
      clearTimeout(timer);
      el.style.opacity = "0";
      setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 150);
    }
    el.addEventListener("click", dismiss);
    timer = setTimeout(dismiss, ms);
  }

  window.ndToast = toast;
})();
