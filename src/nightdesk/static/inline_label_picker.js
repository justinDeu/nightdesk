/* inline_label_picker.js — controller for the inline label popover in list rows.
 *
 * Drives the toggle-based label picker that opens when clicking the label
 * area in a list row. Follows the same open/close/keyboard patterns as
 * property_picker.js but for multi-select labels. Each label toggle commits
 * immediately via HTMX, swapping the full anchor (chips + popover) so the
 * popover stays open for further toggles.
 *
 * DOM contract:
 *   [data-label-picker="{tid}"]              wrapper (position: relative)
 *     label chips                              visible labels
 *     [data-inline-label-menu="{tid}"]        popover container (lazy-filled)
 *       [data-picker-url]                     GET url for the popover body
 *
 * The popover body (partials/inline_label_picker.html) contains:
 *   [data-inline-label-search]  filter input
 *   [data-inline-label-option]  toggle buttons (HTMX hx-post commits + swaps anchor)
 *   [data-inline-label-empty]   "no matches" row toggled during search
 */
(function () {
  "use strict";

  // ---- element helpers ---------------------------------------------------

  function menuOf(anchor) {
    return anchor ? anchor.querySelector("[data-inline-label-menu]") : null;
  }
  function openMenus() {
    return document.querySelectorAll("[data-inline-label-menu]:not(.hidden)");
  }
  function visibleOptions(menu) {
    return Array.prototype.filter.call(
      menu.querySelectorAll("[data-inline-label-option]"),
      function (o) { return o.offsetParent !== null && !o.hasAttribute("hidden"); }
    );
  }

  // ---- open / close ------------------------------------------------------

  function closeAll(except) {
    var closed = 0;
    openMenus().forEach(function (menu) {
      if (menu === except) return;
      menu.classList.add("hidden");
      closed += 1;
      // Only clear if it was lazy-loaded (has a picker-url for next open)
      if (menu.getAttribute("data-picker-url")) {
        menu.innerHTML = "";
      }
    });
    // Nudge the columns poll when a menu actually closed. The board guard
    // suppresses polls while a picker is open, so this is the first chance
    // for other surfaces to pick up any toggles made during the session.
    if (closed > 0) {
      var poll = document.getElementById("board-columns-poll");
      if (poll && window.htmx) {
        try { window.htmx.trigger(poll, "poll"); } catch (e) {}
      }
    }
  }

  function renderError(menu) {
    menu.innerHTML =
      '<div class="px-3 py-2 text-xs text-danger flex items-center gap-2">' +
      "<span>Couldn't load labels.</span>" +
      '<button type="button" data-label-retry class="underline">Retry</button>' +
      "</div>";
  }

  function loadMenu(menu) {
    var url = menu.getAttribute("data-picker-url");
    if (!url) return;
    menu.innerHTML =
      '<div class="px-3 py-2 text-xs text-fg-muted">Loading…</div>';
    function onLoaded() { focusMenu(menu); }
    if (window.htmx && typeof window.htmx.ajax === "function") {
      var p = window.htmx.ajax("GET", url, { target: menu, swap: "innerHTML" });
      if (p && typeof p.then === "function") {
        p.then(onLoaded).catch(function () { renderError(menu); });
      } else {
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

  function openPicker(anchor) {
    var menu = menuOf(anchor);
    if (!menu) return;
    // Unhide BEFORE closeAll: its poll nudge must see an open menu so the
    // board's pickerOpen() guard suppresses the poll (else the response
    // could swap the card under the picker that's just opening).
    menu.classList.remove("hidden");
    closeAll(menu);
    // Lazy-load if empty (initial open)
    if (!menu.textContent.trim()) {
      loadMenu(menu);
    } else {
      focusMenu(menu);
    }
  }

  function togglePicker(anchor) {
    var menu = menuOf(anchor);
    if (!menu) return;
    if (menu.classList.contains("hidden")) openPicker(anchor);
    else closeAll();
  }

  // ---- keyboard + search active option -----------------------------------

  function clearActive(menu) {
    menu.querySelectorAll("[data-inline-label-option].nd-opt-active").forEach(
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
    return menu.querySelector("[data-inline-label-option].nd-opt-active");
  }

  function focusMenu(menu) {
    var search = menu.querySelector("[data-inline-label-search]");
    var opts = visibleOptions(menu);
    var current = menu.querySelector('[data-inline-label-option][aria-selected="true"]');
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
    var opts = menu.querySelectorAll("[data-inline-label-option]");
    var shown = 0;
    opts.forEach(function (o) {
      var text = o.getAttribute("data-search-text") || o.textContent.toLowerCase();
      var match = !q || text.indexOf(q) !== -1;
      if (match) { o.removeAttribute("hidden"); shown += 1; }
      else { o.setAttribute("hidden", "hidden"); }
    });
    var empty = menu.querySelector("[data-inline-label-empty]");
    if (empty) empty.classList.toggle("hidden", shown !== 0);
    setActive(menu, visibleOptions(menu)[0]);
  }

  // ---- programmatic entry point (command palette / keyboard shortcuts) ----
  //
  // Opens the inline label picker for a given ticket by locating its anchor
  // in the current DOM (board card or list row).  Returns true if an anchor
  // was found.  This lets the command palette and keyboard shortcuts reuse
  // the one picker primitive instead of shipping a separate UI.
  window.ndOpenLabelPicker = function (ticketId) {
    var anchor = document.querySelector('[data-label-picker="' + ticketId + '"]');
    if (!anchor) return false;
    try { anchor.scrollIntoView({ block: "nearest" }); } catch (e) {}
    openPicker(anchor);
    return true;
  };

  // ---- commit feedback (called from toggle buttons' hx-on::after-request) --

  window.ndInlineLabelCommitted = function (btn, ev) {
    // On success the outerHTML swap destroys the button BEFORE htmx
    // dispatches after-request (it re-targets the event at the swap
    // container), so this handler only ever runs for FAILED requests —
    // no swap happened and the button survived. Success-side work
    // (refocus + poll nudge) lives in the delegated htmx:afterSettle
    // listener and closeAll() below.
    var ok = !!(ev && ev.detail && ev.detail.successful);
    if (ok) return;
    // On failure, show an inline error. The popover stays open.
    var menu = btn.closest("[data-inline-label-menu]");
    if (!menu) return;
    var xhr = ev && ev.detail && ev.detail.xhr;
    var msg = "Update failed";
    if (xhr) {
      if (xhr.status === 404) msg = "Ticket or label not found";
    }
    var existing = menu.querySelector("[data-inline-label-error]");
    if (existing) existing.remove();
    var err = document.createElement("div");
    err.setAttribute("data-inline-label-error", "");
    err.setAttribute("role", "alert");
    err.className = "px-3 py-1.5 text-[11px] text-danger border-t border-border";
    err.textContent = msg;
    menu.appendChild(err);
  };

  // ---- global wiring (event delegation; survives HTMX swaps) --------------

  // Capture phase so we can stop a label-area click from also opening the
  // card's sidebar (the row listens on click in the bubble phase).
  document.addEventListener(
    "click",
    function (e) {
      // Retry button in error state
      var retry = e.target.closest && e.target.closest("[data-label-retry]");
      if (retry) {
        e.preventDefault();
        e.stopPropagation();
        var m = retry.closest("[data-inline-label-menu]");
        var a = m && m.closest("[data-label-picker]");
        if (m && a) loadMenu(m);
        return;
      }
      // Click on label anchor (chips or placeholder)
      var anchor = e.target.closest && e.target.closest("[data-label-picker]");
      if (anchor) {
        // Don't toggle if the click was inside the popover menu (on an option)
        var menu = e.target.closest && e.target.closest("[data-inline-label-menu]");
        if (menu) return; // let the option button's HTMX handle it
        e.preventDefault();
        e.stopPropagation();
        togglePicker(anchor);
        return;
      }
      // Click outside closes any open label menu
      if (!(e.target.closest && e.target.closest("[data-inline-label-menu]"))) {
        closeAll();
      }
    },
    true
  );

  document.addEventListener("input", function (e) {
    var search = e.target.closest && e.target.closest("[data-inline-label-search]");
    if (!search) return;
    var menu = search.closest("[data-inline-label-menu]");
    if (menu) applyFilter(menu, search.value);
  });

  // After a successful label toggle, htmx outerHTML-swaps the whole anchor
  // and dispatches afterSettle at the swapped-in NEW anchor. Refocus its
  // search input here — the button's own after-request hook never fires on
  // success (the swap destroys the button first), so this delegated
  // listener is the only reliable success-side hook.
  document.body.addEventListener("htmx:afterSettle", function (e) {
    var anchor = e.target;
    if (!anchor || !anchor.matches || !anchor.matches("[data-label-picker]")) return;
    var menu = menuOf(anchor);
    if (!menu || menu.classList.contains("hidden")) return;
    focusMenu(menu);
    // Nudge the columns poll so other surfaces reflect the change. While
    // the popover is open the board's poll guard suppresses it; closeAll()
    // fires another nudge on close to catch up.
    var poll = document.getElementById("board-columns-poll");
    if (poll && window.htmx) {
      try { window.htmx.trigger(poll, "poll"); } catch (err) {}
    }
  });

  document.addEventListener(
    "keydown",
    function (e) {
      var menu = openMenus()[0];
      if (!menu) return;
      if (e.key === "Escape") {
        e.preventDefault();
        // stopImmediatePropagation prevents sibling capture listeners (e.g.
        // command palette) from also acting on this Escape, independent of
        // load order.
        e.stopImmediatePropagation();
        closeAll();
        // Return focus to the anchor element itself (it has tabindex="-1").
        var anchor = menu.closest("[data-label-picker]");
        if (anchor) try { anchor.focus(); } catch (err) {}
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
