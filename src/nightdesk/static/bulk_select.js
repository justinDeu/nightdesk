// bulk_select.js
//
// Selection model + bulk action bar for board/list management.
//
//   X            toggle selection of the focused (cursor) card
//   Shift+J / K  extend the selection down / up while moving the cursor
//   Esc          clear the selection
//
// The keyboard keys themselves are dispatched by command_palette.js (the single
// keyboard owner) which calls into the window.ndBulkSelect API exposed here, and
// reuses its window.ndBoardCursor helpers so cursor logic is never duplicated.
//
// Selection state is an in-memory Set of ticket ids, so it is preserved across
// HTMX board swaps BY TICKET ID: the visual highlight is re-applied from the Set
// on every htmx:afterSwap / htmx:oobAfterSwap (the same hooks the cursor uses).
//
// The action bar drives the cookie-auth bulk routes:
//   POST /board/tickets/bulk/priority   {ticket_ids, priority}
//   POST /board/tickets/bulk/status     {ticket_ids, status}
//   POST /board/tickets/bulk/project    {ticket_ids, project_id}
//   POST /board/tickets/bulk/labels     {ticket_ids, label_ids}
//   POST /board/tickets/bulk/archive    {ticket_ids}
// High-impact actions return an `undo` descriptor; the result toast offers a
// one-click revert that re-POSTs it.
//
// PROGRESSIVE ENHANCEMENT: every bulk change is also reachable per-ticket
// through the sidebar / property pickers. If this script fails to load nothing
// breaks — the selection shortcuts and bar simply never appear.

(function () {
  "use strict";

  var selected = new Set();

  // ---- helpers -----------------------------------------------------------

  function onBoard() {
    return !!document.getElementById("board-grid");
  }

  function bar() { return document.getElementById("nd-bulk-bar"); }

  function cssEscape(s) {
    if (window.CSS && CSS.escape) return CSS.escape(s);
    return String(s).replace(/["\\\]]/g, "\\$&");
  }

  function allCards() {
    return document.querySelectorAll("li[data-ticket-id]");
  }

  // The focused card: the keyboard cursor, else the sidebar-selected ticket.
  function focusedId() {
    var cur = document.querySelector("li[data-ticket-id][data-nd-cursor]");
    if (cur) return cur.getAttribute("data-ticket-id");
    var sidebar = document.getElementById("sidebar");
    var sel = sidebar && sidebar.getAttribute("data-selected-ticket-id");
    return sel || null;
  }

  function ids() { return Array.from(selected); }

  // ---- visual ------------------------------------------------------------

  function applyVisual() {
    allCards().forEach(function (card) {
      var on = selected.has(card.getAttribute("data-ticket-id"));
      card.classList.toggle("nd-bulk-selected", on);
      if (on) card.setAttribute("data-nd-bulk-selected", "");
      else card.removeAttribute("data-nd-bulk-selected");
    });
  }

  function renderBar() {
    var b = bar();
    if (!b) return;
    var countEl = b.querySelector("[data-nd-bulk-count]");
    if (countEl) countEl.textContent = String(selected.size);
    if (selected.size > 0) {
      b.hidden = false;
    } else {
      b.hidden = true;
      closeMenus();
    }
  }

  function sync() { applyVisual(); renderBar(); }

  // ---- selection API (consumed by command_palette.js) --------------------

  function selectId(id) { if (id) selected.add(id); }

  function toggleFocused() {
    if (!onBoard()) return;
    var id = focusedId();
    if (!id) {
      // No focus yet — drop the cursor on the first card and select it.
      var api = window.ndBoardCursor;
      if (api && api.cards().length) {
        api.set(0);
        id = api.currentId();
      }
    }
    if (!id) return;
    if (selected.has(id)) selected.delete(id);
    else selected.add(id);
    sync();
  }

  // Extend the selection by moving the cursor one card in `delta` direction,
  // selecting both the card we leave and the card we land on (contiguous grow).
  function extend(delta) {
    if (!onBoard()) return;
    var api = window.ndBoardCursor;
    if (!api) return;
    var idx = api.index();
    if (idx < 0) {
      api.set(0);
      selectId(api.currentId());
      sync();
      return;
    }
    selectId(api.currentId());
    api.set(idx + delta);
    selectId(api.currentId());
    sync();
  }

  function clear() {
    if (selected.size === 0) return;
    selected.clear();
    sync();
  }

  window.ndBulkSelect = {
    toggleFocused: toggleFocused,
    extend: extend,
    clear: clear,
    openMenu: openMenu,
    has: function (id) { return selected.has(id); },
    count: function () { return selected.size; },
    ids: ids,
  };

  // ---- toast (with optional Undo) ----------------------------------------

  function postForm(url, params) {
    var fd = new FormData();
    Object.keys(params).forEach(function (k) { fd.append(k, params[k]); });
    return fetch(url, {
      method: "POST", body: fd, credentials: "same-origin",
      headers: { "HX-Request": "true" },
    });
  }

  function postJson(url, body) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "HX-Request": "true" },
      body: JSON.stringify(body),
    });
  }

  function pollColumnsNow() {
    var t = document.getElementById("board-columns-poll");
    if (t && typeof window.htmx !== "undefined") {
      try { window.htmx.trigger(t, "poll"); } catch (e) {}
    }
  }

  function toast(text, undo) {
    var el = document.createElement("div");
    el.setAttribute("role", "status");
    el.style.cssText =
      "position:fixed;left:50%;bottom:74px;transform:translateX(-50%);" +
      "z-index:130;background:var(--color-bg-elev);color:var(--color-fg);" +
      "border:1px solid var(--color-border);border-radius:.375rem;" +
      "padding:.5rem .875rem;font-size:.8125rem;display:flex;align-items:center;" +
      "gap:.75rem;box-shadow:0 8px 20px rgba(0,0,0,.35);";
    var msg = document.createElement("span");
    msg.textContent = text;
    el.appendChild(msg);
    var timer;
    function dismiss() { clearTimeout(timer); el.remove(); }
    if (undo) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = "Undo";
      btn.style.cssText =
        "cursor:pointer;border:1px solid var(--color-border);" +
        "background:var(--color-bg-elev-2);color:var(--color-accent);" +
        "border-radius:.25rem;padding:.2rem .55rem;font-size:.8125rem;";
      btn.addEventListener("click", function () {
        dismiss();
        var p = undo.json ? postJson(undo.route, undo.payload)
                          : postForm(undo.route, undo.payload);
        p.then(function () { pollColumnsNow(); toast("Reverted"); },
               function () { toast("Undo failed"); });
      });
      el.appendChild(btn);
    }
    document.body.appendChild(el);
    timer = setTimeout(dismiss, undo ? 6000 : 2200);
  }

  // ---- bulk action bar ---------------------------------------------------

  var ROUTES = {
    priority: { url: "/board/tickets/bulk/priority", field: "priority" },
    status:   { url: "/board/tickets/bulk/status",   field: "status" },
    project:  { url: "/board/tickets/bulk/project",  field: "project_id" },
    labels:   { url: "/board/tickets/bulk/labels",   field: "label_ids" },
  };

  function closeMenus() {
    var b = bar();
    if (!b) return;
    b.querySelectorAll("[data-nd-bulk-menu]").forEach(function (m) {
      m.hidden = true;
    });
    b.querySelectorAll("[data-nd-bulk-toggle]").forEach(function (t) {
      t.setAttribute("aria-expanded", "false");
    });
  }

  function toggleMenu(name) {
    var b = bar();
    if (!b) return;
    var menu = b.querySelector('[data-nd-bulk-menu="' + name + '"]');
    var btn = b.querySelector('[data-nd-bulk-toggle="' + name + '"]');
    if (!menu) return;
    var willOpen = menu.hidden;
    closeMenus();
    menu.hidden = !willOpen;
    if (btn) btn.setAttribute("aria-expanded", willOpen ? "true" : "false");
  }

  function openMenu(name) {
    var b = bar();
    if (!b || b.hidden) return false;
    var menu = b.querySelector('[data-nd-bulk-menu="' + name + '"]');
    var btn = b.querySelector('[data-nd-bulk-toggle="' + name + '"]');
    if (!menu) return false;
    closeMenus();
    menu.hidden = false;
    if (btn) btn.setAttribute("aria-expanded", "true");
    var first = menu.querySelector("button:not([disabled])");
    if (first) first.focus();
    else if (btn) btn.focus();
    return true;
  }

  function describe(prop) {
    return { priority: "priority", status: "status", project: "project",
             labels: "labels" }[prop] || prop;
  }

  function apply(prop, value) {
    var cfg = ROUTES[prop];
    if (!cfg) return;
    var picked = ids();
    if (!picked.length) return;
    var params = { ticket_ids: picked.join(",") };
    params[cfg.field] = value;
    closeMenus();
    postForm(cfg.url, params).then(function (r) {
      if (!r.ok) {
        toast("Couldn't update " + describe(prop) + " (" + r.status + ")");
        return;
      }
      return r.json().then(function (body) {
        var n = (body.updated || []).length;
        var skipped = (body.skipped || []).length;
        var text = "Updated " + describe(prop) + " on " + n + " ticket" +
                   (n === 1 ? "" : "s");
        if (skipped) text += " · " + skipped + " skipped";
        pollColumnsNow();
        toast(text, body.undo || null);
      });
    }, function () {
      toast("Couldn't update " + describe(prop));
    });
  }

  function archive() {
    var picked = ids();
    if (!picked.length) return;
    postForm("/board/tickets/bulk/archive", { ticket_ids: picked.join(",") })
      .then(function (r) {
        if (!r.ok) { toast("Couldn't archive (" + r.status + ")"); return; }
        return r.json().then(function (body) {
          var n = (body.updated || []).length;
          var skipped = (body.skipped || []).length;
          var text = "Archived " + n + " ticket" + (n === 1 ? "" : "s");
          if (skipped) text += " · " + skipped + " skipped (only review tickets archive)";
          // Archived tickets leave the visible columns; drop them from the set.
          (body.updated || []).forEach(function (u) { selected.delete(u.id); });
          sync();
          pollColumnsNow();
          toast(text, body.undo || null);
        });
      }, function () { toast("Couldn't archive"); });
  }

  function wireBar() {
    var b = bar();
    if (!b || b.__ndWired) return;
    b.__ndWired = true;
    b.addEventListener("click", function (e) {
      var toggle = e.target.closest("[data-nd-bulk-toggle]");
      if (toggle) { e.preventDefault(); toggleMenu(toggle.getAttribute("data-nd-bulk-toggle")); return; }
      var opt = e.target.closest("[data-nd-bulk-apply]");
      if (opt) {
        e.preventDefault();
        apply(opt.getAttribute("data-nd-bulk-apply"), opt.getAttribute("data-value"));
        return;
      }
      if (e.target.closest("[data-nd-bulk-archive]")) { e.preventDefault(); archive(); return; }
      if (e.target.closest("[data-nd-bulk-clear]")) { e.preventDefault(); clear(); return; }
    });
  }

  // Close menus when clicking outside the bar.
  document.addEventListener("click", function (e) {
    var b = bar();
    if (!b || b.hidden) return;
    if (!e.target.closest || !e.target.closest("#nd-bulk-bar")) closeMenus();
  });

  // ---- init --------------------------------------------------------------

  function init() {
    wireBar();
    sync();
    // Re-apply the selection highlight after HTMX swaps replace cards (the 3s
    // column poll OOB-swaps every <li>, dropping the class). The Set is keyed
    // by ticket id so selection survives by id.
    document.body.addEventListener("htmx:afterSwap", function () {
      wireBar(); applyVisual();
    });
    document.body.addEventListener("htmx:oobAfterSwap", function () {
      wireBar(); applyVisual();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
