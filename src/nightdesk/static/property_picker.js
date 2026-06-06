/* property_picker.js — controller for the ONE shared property-picker primitive.
 *
 * Drives every ticket metadata picker (priority, status, project, …) rendered
 * by partials/property_picker.html. There are no per-property scripts: a
 * single delegated set of listeners handles open/close, lazy menu loading,
 * keyboard navigation, type-to-filter search, and commit/close.
 *
 * DOM contract (see the partials):
 *   [data-property-picker="{tid}:{prop}"]   wrapper (position: relative)
 *     [data-property-chip="{tid}:{prop}"]    the chip button (anchor + hx swap target)
 *     [data-property-menu="{tid}:{prop}"]    popover container (lazy-filled)
 *       [data-picker-url]                    GET url for the menu body
 *
 * The menu body (partials/property_picker_menu.html) contains:
 *   [data-property-search]   optional filter input
 *   [data-property-option]   option buttons (HTMX hx-post commits + swaps chip)
 *   [data-property-empty]    "no matches" row toggled during search
 *
 * PROGRESSIVE ENHANCEMENT: the chip stays a real, focusable control; if this
 * script fails to load nothing is destroyed — the metadata is still editable
 * through the ticket edit modal. Every commit is a focused server route.
 */
(function () {
  "use strict";

  // ---- element helpers ---------------------------------------------------

  function menuOf(picker) {
    return picker ? picker.querySelector("[data-property-menu]") : null;
  }
  function chipOf(picker) {
    return picker ? picker.querySelector("[data-property-chip]") : null;
  }
  function openMenus() {
    return document.querySelectorAll("[data-property-menu]:not(.hidden)");
  }
  function visibleOptions(menu) {
    return Array.prototype.filter.call(
      menu.querySelectorAll("[data-property-option]"),
      function (o) { return o.offsetParent !== null && !o.hasAttribute("hidden"); }
    );
  }

  // ---- open / close ------------------------------------------------------

  function closeAll(except) {
    openMenus().forEach(function (menu) {
      if (menu === except) return;
      menu.classList.add("hidden");
      menu.innerHTML = "";
      var picker = menu.closest("[data-property-picker]");
      var chip = chipOf(picker);
      if (chip && chip.setAttribute) chip.setAttribute("aria-expanded", "false");
    });
  }

  function renderError(menu) {
    menu.innerHTML =
      '<div class="px-3 py-2 text-xs text-danger flex items-center gap-2">' +
      "<span>Couldn't load options.</span>" +
      '<button type="button" data-property-retry class="underline">Retry</button>' +
      "</div>";
  }

  function loadMenu(menu, chip) {
    var url = menu.getAttribute("data-picker-url");
    menu.innerHTML =
      '<div class="px-3 py-2 text-xs text-fg-muted">Loading…</div>';
    function onLoaded() { focusMenu(menu); }
    if (window.htmx && typeof window.htmx.ajax === "function") {
      var p = window.htmx.ajax("GET", url, { target: menu, swap: "innerHTML" });
      if (p && typeof p.then === "function") {
        p.then(onLoaded).catch(function () { renderError(menu); });
      } else {
        // Older htmx returns undefined; give it a tick to populate.
        setTimeout(onLoaded, 0);
      }
      return;
    }
    fetch(url, { credentials: "same-origin" })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.text(); })
      .then(function (h) {
        menu.innerHTML = h;
        if (window.htmx) window.htmx.process(menu);
        onLoaded();
      })
      .catch(function () { renderError(menu); });
  }

  function openPicker(chip) {
    var picker = chip.closest("[data-property-picker]");
    var menu = menuOf(picker);
    if (!menu) return;
    closeAll(menu);
    menu.classList.remove("hidden");
    chip.setAttribute("aria-expanded", "true");
    loadMenu(menu, chip);
  }

  function togglePicker(chip) {
    var picker = chip.closest("[data-property-picker]");
    var menu = menuOf(picker);
    if (!menu) return;
    if (menu.classList.contains("hidden")) openPicker(chip);
    else closeAll();
  }

  // ---- keyboard + search active option -----------------------------------

  function clearActive(menu) {
    menu.querySelectorAll("[data-property-option].nd-opt-active").forEach(
      function (o) { o.classList.remove("nd-opt-active"); }
    );
  }
  function setActive(menu, opt) {
    if (!opt) return;
    clearActive(menu);
    opt.classList.add("nd-opt-active");
    opt.scrollIntoView({ block: "nearest" });
  }
  function activeOption(menu) {
    return menu.querySelector("[data-property-option].nd-opt-active");
  }

  function focusMenu(menu) {
    var search = menu.querySelector("[data-property-search]");
    var opts = visibleOptions(menu);
    var current = menu.querySelector('[data-property-option][aria-selected="true"]');
    setActive(menu, current && opts.indexOf(current) >= 0 ? current : opts[0]);
    if (search) {
      try { search.focus(); } catch (e) {}
    }
  }

  function moveActive(menu, delta) {
    var opts = visibleOptions(menu);
    if (!opts.length) return;
    var cur = activeOption(menu);
    var idx = cur ? opts.indexOf(cur) : -1;
    idx = (idx + delta + opts.length) % opts.length;
    setActive(menu, opts[idx]);
  }

  function applyFilter(menu, q) {
    q = (q || "").trim().toLowerCase();
    var opts = menu.querySelectorAll("[data-property-option]");
    var shown = 0;
    opts.forEach(function (o) {
      var text = o.getAttribute("data-search-text") || o.textContent.toLowerCase();
      var match = !q || text.indexOf(q) !== -1;
      if (match) { o.removeAttribute("hidden"); shown += 1; }
      else { o.setAttribute("hidden", "hidden"); }
    });
    var empty = menu.querySelector("[data-property-empty]");
    if (empty) empty.classList.toggle("hidden", shown !== 0);
    setActive(menu, visibleOptions(menu)[0]);
  }

  // ---- commit feedback (called from the option's hx-on::after-request) ----

  window.ndPropertyPickerCommitted = function (btn, ev) {
    var ok = !!(ev && ev.detail && ev.detail.successful);
    var menu = btn.closest("[data-property-menu]");
    if (ok) {
      closeAll();
      // If we're on the board, nudge the column poll so other surfaces
      // (cards) reflect the change promptly.
      var poll = document.getElementById("board-columns-poll");
      if (poll && window.htmx) {
        try { window.htmx.trigger(poll, "poll"); } catch (e) {}
      }
      return;
    }
    if (!menu) return;
    var xhr = ev && ev.detail && ev.detail.xhr;
    var msg = "Update failed";
    if (xhr) {
      if (xhr.status === 409) msg = "Not allowed from this state";
      else if (xhr.status === 404) msg = "Ticket not found";
      else if (xhr.status === 422) msg = "Invalid value";
    }
    var existing = menu.querySelector("[data-property-error]");
    if (existing) existing.remove();
    var err = document.createElement("div");
    err.setAttribute("data-property-error", "");
    err.setAttribute("role", "alert");
    err.className = "px-3 py-1.5 text-[11px] text-danger border-t border-border";
    err.textContent = msg;
    menu.appendChild(err);
  };

  // ---- programmatic entry point (command palette / keyboard shortcuts) ----
  //
  // Opens the picker for a given ticket+property by locating its chip in the
  // current DOM (sidebar on the board, header on the detail page) and toggling
  // it open. Returns true if a chip was found. This is how command-palette
  // actions and keyboard shortcuts reuse the one picker primitive instead of
  // shipping their own metadata UI.
  window.ndOpenPropertyPicker = function (tid, prop) {
    var sel = '[data-property-chip="' + tid + ":" + prop + '"]';
    var chip = document.querySelector(sel);
    if (!chip || chip.tagName !== "BUTTON") return false;
    try { chip.scrollIntoView({ block: "nearest" }); } catch (e) {}
    openPicker(chip);
    return true;
  };

  // ---- global wiring (event delegation; survives HTMX swaps) --------------

  // Capture phase so we can stop a chip click from also opening the card's
  // sidebar (the card listens on click in the bubble phase).
  document.addEventListener(
    "click",
    function (e) {
      var retry = e.target.closest && e.target.closest("[data-property-retry]");
      if (retry) {
        e.preventDefault();
        e.stopPropagation();
        var m = retry.closest("[data-property-menu]");
        var p = m && m.closest("[data-property-picker]");
        if (m && p) loadMenu(m, chipOf(p));
        return;
      }
      var chip = e.target.closest && e.target.closest("[data-property-chip]");
      if (chip && chip.tagName === "BUTTON") {
        e.preventDefault();
        e.stopPropagation();
        togglePicker(chip);
        return;
      }
      // A click anywhere outside an open menu closes it. Clicks on option
      // buttons live inside the menu, so they fall through to HTMX untouched.
      if (!(e.target.closest && e.target.closest("[data-property-menu]"))) {
        closeAll();
      }
    },
    true
  );

  document.addEventListener("input", function (e) {
    var search = e.target.closest && e.target.closest("[data-property-search]");
    if (!search) return;
    var menu = search.closest("[data-property-menu]");
    if (menu) applyFilter(menu, search.value);
  });

  document.addEventListener(
    "keydown",
    function (e) {
      var menu = openMenus()[0];
      if (!menu) return;
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        var picker = menu.closest("[data-property-picker]");
        var chip = chipOf(picker);
        closeAll();
        if (chip && chip.focus) {
          try { chip.focus(); } catch (err) {}
        }
        return;
      }
      if (e.key === "ArrowDown") {
        e.preventDefault();
        moveActive(menu, 1);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        moveActive(menu, -1);
      } else if (e.key === "Enter") {
        var act = activeOption(menu);
        if (act) {
          e.preventDefault();
          act.click();
        }
      }
    },
    true
  );
})();
