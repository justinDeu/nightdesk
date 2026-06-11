// display_options.js
//
// The Display options popover for the board and list pages. One popover
// (#nd-display-popover) per page, anchored to the Display button
// (#nd-display-btn) on the toolbar. It hosts the group/order controls and
// the show/hide property checkboxes; the page's own inline script remains
// the source of truth for URL sync + refresh via the exposed setters:
//   board: window.ndBoardSetOrder(order), window.ndBoardSetProps(props)
//   list:  window.ndListSetProps(props)  (group/order selects are wired by
//          list.html itself — they kept their ids when they moved in here).
//
// Exposed API for command_palette.js (Shift+V toggle, Esc close):
//   window.ndDisplayPopover = { open, close, isOpen, toggle }
//
// PROGRESSIVE ENHANCEMENT: without this script the popover simply never
// opens; grouping/ordering stay reachable via URL params.
(function () {
  "use strict";

  function popoverEl() { return document.getElementById("nd-display-popover"); }
  function btnEl() { return document.getElementById("nd-display-btn"); }

  function isOpen() {
    var p = popoverEl();
    return !!p && !p.hidden;
  }

  function open() {
    var p = popoverEl();
    if (!p) return;
    p.hidden = false;
    var btn = btnEl();
    if (btn) btn.setAttribute("aria-expanded", "true");
    // Move focus into the first interactive control so keyboard users can
    // reach the popover immediately and global shortcuts stop firing.
    var first = p.querySelector("select, input, button");
    if (first) first.focus();
  }

  // opts.refocus: restore focus to the toggle button after closing (pass true
  // for keyboard-initiated closes; omit or false for mouse-initiated ones so
  // the click doesn't yank focus away from where the user was heading).
  function close(opts) {
    var p = popoverEl();
    if (!p) return;
    p.hidden = true;
    var btn = btnEl();
    if (btn) btn.setAttribute("aria-expanded", "false");
    if (opts && opts.refocus && btn) btn.focus();
  }

  function toggle() {
    if (isOpen()) close(); else open();
  }

  // Close on outside click (clicks inside the popover or on the button stay).
  document.addEventListener("click", function (e) {
    if (!isOpen()) return;
    var p = popoverEl();
    var btn = btnEl();
    if (!p) return;
    if (p.contains(e.target) || (btn && btn.contains(e.target))) return;
    close();
  });

  // Esc closes the popover and returns focus to the toggle button so the user
  // doesn't lose their place.
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && isOpen()) {
      close({ refocus: true });
    }
  });

  // Props string from the checked boxes of one checkbox group. Returns ""
  // when everything is checked — the default "all shown" keeps the URL clean.
  function computeProps(propName) {
    var p = popoverEl();
    if (!p) return "";
    var boxes = p.querySelectorAll("input[name='" + propName + "']");
    var keys = [];
    var allChecked = true;
    boxes.forEach(function (cb) {
      if (cb.checked) keys.push(cb.value);
      else allChecked = false;
    });
    return allChecked ? "" : keys.join(",");
  }

  function wireButton() {
    var btn = btnEl();
    if (!btn || btn.__ndDisplayWired) return;
    btn.__ndDisplayWired = true;
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      toggle();
    });
  }

  function wireBoardOrder() {
    var sel = document.getElementById("nd-board-order");
    if (!sel || sel.__ndWired) return;
    sel.__ndWired = true;
    sel.addEventListener("change", function () {
      if (typeof window.ndBoardSetOrder === "function") {
        window.ndBoardSetOrder(sel.value || "manual");
      }
    });
  }

  function wirePropCheckboxes(propName, setterName) {
    var p = popoverEl();
    if (!p) return;
    p.querySelectorAll("input[name='" + propName + "']").forEach(function (cb) {
      if (cb.__ndWired) return;
      cb.__ndWired = true;
      cb.addEventListener("change", function () {
        var setter = window[setterName];
        if (typeof setter === "function") setter(computeProps(propName));
      });
    });
  }

  function init() {
    wireButton();
    wireBoardOrder();
    wirePropCheckboxes("card-prop", "ndBoardSetProps");
    wirePropCheckboxes("list-prop", "ndListSetProps");
  }

  window.ndDisplayPopover = { open: open, close: close, isOpen: isOpen, toggle: toggle };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
