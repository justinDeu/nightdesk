// saved_views.js — saved-view cache, command-palette hook, and save-view form.
//
// Exposes:
//   window.ndSavedViews()          → [{id, name, surface, url}] (sync, from cache)
//   window.ndOpenSaveViewDialog(surface)  → open the save-view <dialog>
//
// The cache is warmed by a single fetch to /api/v1/views on page load.
// command_palette.js reads ndSavedViews() to add "View: <name>" commands.
(function () {
  "use strict";

  // ---- cache ---------------------------------------------------------------

  var _cache = null;

  function _loadViews() {
    fetch("/api/v1/views", { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : []; })
      .then(function (data) { _cache = Array.isArray(data) ? data : []; })
      .catch(function () { _cache = []; });
  }

  // Warm on page load.
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", _loadViews);
  } else {
    _loadViews();
  }

  // Returns current cache synchronously; may be null before first fetch.
  window.ndSavedViews = function () { return _cache || []; };

  // ---- nav popover refresh -------------------------------------------------

  function _refreshNavPopover() {
    var popover = document.getElementById("nd-views-nav-popover");
    if (!popover) return;
    if (typeof window.htmx !== "undefined") {
      try { window.htmx.ajax("GET", "/views/menu", { target: "#nd-views-nav-popover", swap: "innerHTML" }); } catch (e) {}
    }
    // Also invalidate the in-memory cache so the palette reflects changes.
    _loadViews();
  }

  // ---- save-view dialog ----------------------------------------------------

  window.ndOpenSaveViewDialog = function (surface) {
    var dlg = document.getElementById("nd-save-view-dialog");
    if (!dlg) return;
    // Store the surface on the hidden input.
    var surfaceInput = document.getElementById("nd-save-view-surface");
    if (surfaceInput) surfaceInput.value = surface || "";
    // Clear previous state.
    var nameInput = document.getElementById("nd-save-view-name");
    var errEl = document.getElementById("nd-save-view-error");
    if (nameInput) nameInput.value = "";
    if (errEl) { errEl.textContent = ""; errEl.classList.add("hidden"); }
    try { dlg.showModal(); } catch (e) {}
    if (nameInput) nameInput.focus();
  };

  // Wire the form once the DOM is ready.
  function _wireSaveForm() {
    var form = document.getElementById("nd-save-view-form");
    if (!form || form.__ndWired) return;
    form.__ndWired = true;

    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var dlg = document.getElementById("nd-save-view-dialog");
      var nameInput = document.getElementById("nd-save-view-name");
      var surfaceInput = document.getElementById("nd-save-view-surface");
      var errEl = document.getElementById("nd-save-view-error");

      var name = (nameInput ? nameInput.value : "").trim();
      var surface = surfaceInput ? surfaceInput.value : "";

      if (!name) {
        if (errEl) { errEl.textContent = "A name is required."; errEl.classList.remove("hidden"); }
        if (nameInput) nameInput.focus();
        return;
      }

      // Collect current URL params for the surface. Both surfaces accept the
      // full display set (q/group/order/props) — keep this aligned with
      // SURFACE_ALLOWED_PARAMS in domain/saved_views.py.
      var sp = new URLSearchParams(location.search);
      var params = {};
      ["q", "group", "order", "props"].forEach(function (k) {
        var v = sp.get(k);
        if (v) params[k] = v;
      });

      var submitBtn = form.querySelector('[type="submit"]');
      if (submitBtn) { submitBtn.disabled = true; submitBtn.textContent = "Saving…"; }

      fetch("/views", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: name, surface: surface, params: params }),
      })
        .then(function (r) {
          if (r.ok) {
            if (dlg) dlg.close();
            _refreshNavPopover();
            window.ndFlashStatus && window.ndFlashStatus("View “" + name + "” saved");
          } else {
            r.json().then(function (body) {
              var msg = (body && body.detail) || ("Error " + r.status);
              if (errEl) { errEl.textContent = msg; errEl.classList.remove("hidden"); }
            }).catch(function () {
              if (errEl) { errEl.textContent = "Error " + r.status; errEl.classList.remove("hidden"); }
            });
          }
        })
        .catch(function () {
          if (errEl) { errEl.textContent = "Network error — could not save view."; errEl.classList.remove("hidden"); }
        })
        .finally(function () {
          if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = "Save view"; }
        });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", _wireSaveForm);
  } else {
    _wireSaveForm();
  }
})();
