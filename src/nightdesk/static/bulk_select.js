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
  var barWired = false;

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

  // Stamp selection state directly onto nodes in an incoming htmx fragment
  // before they are inserted into the DOM.  Called from oobBeforeSwap (and
  // beforeSwap for non-OOB card-container swaps).  htmx 2.0.3 exposes
  // event.detail.fragment on oobBeforeSwap; beforeSwap does not carry a
  // fragment, so that call is a no-op — the afterSwap safety net picks it up.
  //
  // Pre-stamping is essential because the card <li> elements carry
  // `transition-colors` (Tailwind v4, which includes `outline-color`).  If
  // nodes arrive in the DOM without the class, the browser starts a 150 ms
  // CSS outline-fade even though the class is added synchronously in
  // oobAfterSwap.  With the class already present, the browser establishes
  // the selected visual state as the element's initial state — no transition
  // fires.
  function stampFragment(fragment) {
    if (!fragment) return;
    var nodes = [];
    if (fragment.getAttribute && fragment.getAttribute("data-ticket-id")) {
      nodes.push(fragment);
    }
    if (fragment.querySelectorAll) {
      fragment.querySelectorAll("[data-ticket-id]").forEach(function (n) {
        nodes.push(n);
      });
    }
    nodes.forEach(function (node) {
      if (selected.has(node.getAttribute("data-ticket-id"))) {
        node.classList.add("nd-bulk-selected");
        node.setAttribute("data-nd-bulk-selected", "");
      }
    });
  }

  function applyVisual() {
    allCards().forEach(function (card) {
      var on = selected.has(card.getAttribute("data-ticket-id"));
      if (on && !card.classList.contains("nd-bulk-selected")) {
        // Fresh node from a non-OOB swap (the list page's 4 s innerHTML
        // poll) that the pre-stamp couldn't reach: htmx forces a style
        // recalc mid-swap, so adding the class now would animate the
        // card's `transition-colors`.  Suspend transitions, apply, reflow
        // to commit the selected state, then restore.
        card.style.transition = "none";
        card.classList.add("nd-bulk-selected");
        void card.offsetWidth;
        card.style.transition = "";
      } else {
        card.classList.toggle("nd-bulk-selected", on);
      }
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
    archive: archive,
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

  function refreshSidebarNow() {
    var sidebar = document.getElementById("sidebar");
    var tid = sidebar && sidebar.getAttribute("data-selected-ticket-id");
    if (!tid || typeof window.htmx === "undefined") return;
    try {
      window.htmx.ajax("GET", "/board/sidebar?ticket_id=" + encodeURIComponent(tid), {
        target: "#sidebar",
        swap: "outerHTML",
      });
    } catch (e) {}
  }

  function refreshBoardSurfaces() {
    pollColumnsNow();
    refreshSidebarNow();
    // The list page has no board-columns-poll element; it listens for this
    // event instead and calls its own refreshList().
    document.body.dispatchEvent(new CustomEvent("nd:bulk-applied"));
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
    profile:  { url: "/board/tickets/bulk/profile",  field: "profile_id" },
  };

  function closeMenus() {
    var b = bar();
    document.querySelectorAll("[data-nd-bulk-menu]").forEach(function (m) {
      m.hidden = true;
    });
    if (!b) return;
    b.querySelectorAll("[data-nd-bulk-toggle]").forEach(function (t) {
      t.setAttribute("aria-expanded", "false");
    });
  }

  function openMenuName() {
    var m = document.querySelector("[data-nd-bulk-menu]:not([hidden])");
    return m ? m.getAttribute("data-nd-bulk-menu") : null;
  }

  function focusToggleFor(name) {
    if (!name) return;
    var b = bar();
    if (!b) return;
    var btn = b.querySelector('[data-nd-bulk-toggle="' + name + '"]');
    if (btn) { try { btn.focus(); } catch (e) {} }
  }

  function toggleMenu(name) {
    var b = bar();
    if (!b) return;
    var menu = document.querySelector('[data-nd-bulk-menu="' + name + '"]');
    if (!menu) return;
    var willOpen = menu.hidden;
    closeMenus();
    if (willOpen) openMenu(name);
  }

  function visibleOptions(menu) {
    return Array.prototype.filter.call(
      menu.querySelectorAll("[data-nd-bulk-apply], [data-nd-bulk-create-option]"),
      function (opt) { return !opt.hidden && opt.offsetParent !== null; }
    );
  }

  function clearActive(menu) {
    menu.querySelectorAll(".nd-opt-active").forEach(function (opt) {
      opt.classList.remove("nd-opt-active");
    });
    var search = menu.querySelector("[data-nd-bulk-label-search]");
    if (search) {
      search.removeAttribute("aria-activedescendant");
    } else {
      // Non-label menus move real DOM focus to options; remove any stale attribute.
      menu.removeAttribute("aria-activedescendant");
    }
  }

  function setActive(menu, opt) {
    if (!opt) return;
    clearActive(menu);
    opt.classList.add("nd-opt-active");
    if (!opt.id) {
      var menuName = menu.getAttribute("data-nd-bulk-menu") || "x";
      var allOpts = menu.querySelectorAll("[data-nd-bulk-apply], [data-nd-bulk-create-option]");
      var idx = Array.prototype.indexOf.call(allOpts, opt);
      opt.id = "nd-bulk-opt-" + menuName + "-" + (idx >= 0 ? idx : Date.now());
    }
    // Labels menu: focus stays on the search input (role="combobox"), so
    // aria-activedescendant belongs on that input, not the menu container.
    // Other menus: moveActive moves real DOM focus to the option button,
    // so aria-activedescendant is not needed.
    var search = menu.querySelector("[data-nd-bulk-label-search]");
    if (search) {
      search.setAttribute("aria-activedescendant", opt.id);
    }
    try { opt.scrollIntoView({ block: "nearest" }); } catch (e) {}
  }

  function activeOption(menu) {
    return menu.querySelector(".nd-opt-active");
  }

  function moveActive(menu, delta) {
    var opts = visibleOptions(menu);
    if (!opts.length) return;
    var cur = activeOption(menu);
    var idx = cur ? opts.indexOf(cur) : -1;
    idx = (idx + delta + opts.length) % opts.length;
    setActive(menu, opts[idx]);
    if (!menu.querySelector("[data-nd-bulk-label-search]")) {
      try { opts[idx].focus(); } catch (e) {}
    }
  }

  function focusMenu(menu) {
    var search = menu.querySelector("[data-nd-bulk-label-search]");
    updateLabelMarkers(menu);
    applyLabelFilter(menu);
    setActive(menu, visibleOptions(menu)[0]);
    if (search) {
      try { search.focus(); search.select(); } catch (e) {}
      return;
    }
    var active = activeOption(menu);
    if (active) active.focus();
  }

  function openMenu(name) {
    var b = bar();
    if (!b || b.hidden) return false;
    var menu = document.querySelector('[data-nd-bulk-menu="' + name + '"]');
    var btn = b.querySelector('[data-nd-bulk-toggle="' + name + '"]');
    if (!menu) return false;
    if (name === "labels" && menu.parentElement !== document.body) {
      var orphan = document.querySelector('body > [data-nd-bulk-menu="labels"]');
      if (orphan && orphan !== menu) orphan.remove();
      document.body.appendChild(menu);
    }
    closeMenus();
    menu.hidden = false;
    if (btn) btn.setAttribute("aria-expanded", "true");
    focusMenu(menu);
    return true;
  }

  function describe(prop) {
    return { priority: "priority", status: "status", project: "project",
             labels: "labels", profile: "profile" }[prop] || prop;
  }

  // Build a " · N skipped — reason1; reason2" suffix from the skipped array.
  // Reasons are deduped and capped at 3. Uses only string values so it is safe
  // to feed into textContent (no innerHTML injection risk).
  function skipReasonsSuffix(skippedItems) {
    var items = skippedItems || [];
    if (!items.length) return "";
    var reasons = [];
    var seen = {};
    items.forEach(function (s) {
      var r = (s && s.reason) ? String(s.reason) : "";
      if (r && !seen[r]) {
        seen[r] = true;
        if (reasons.length < 3) reasons.push(r);
      }
    });
    var text = " · " + items.length + " skipped";
    if (reasons.length) text += " — " + reasons.join("; ");
    return text;
  }

  function addLabelOption(label) {
    var b = bar();
    if (!b || !label || !label.id) return;
    var menu = document.querySelector('[data-nd-bulk-menu="labels"]');
    if (!menu || menu.querySelector('[data-nd-bulk-apply="labels"][data-value="' + cssEscape(label.id) + '"]')) return;
    menu.querySelectorAll("span.nd-bulk-opt").forEach(function (empty) {
      if ((empty.textContent || "").trim() === "No labels defined") empty.remove();
    });
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "nd-bulk-opt w-full";
    btn.setAttribute("role", "option");
    btn.setAttribute("data-nd-bulk-apply", "labels");
    btn.setAttribute("data-value", label.id);
    btn.setAttribute("data-nd-bulk-label-option", "");
    btn.setAttribute("data-search-text", (label.name || "").toLowerCase());
    var marker = document.createElement("span");
    marker.className = "inline-flex w-5 justify-center text-accent";
    marker.setAttribute("data-nd-bulk-label-marker", "");
    var dot = document.createElement("span");
    dot.className = "nd-bulk-dot";
    dot.style.background = label.color || "var(--color-fg-muted)";
    var name = document.createElement("span");
    name.textContent = label.name || "Label";
    btn.appendChild(marker);
    btn.appendChild(dot);
    btn.appendChild(name);
    var results = menu.querySelector("[data-nd-bulk-label-results]") || menu;
    results.appendChild(btn);
  }

  function labelSelectionCount(labelId) {
    var count = 0;
    selected.forEach(function (tid) {
      var card = document.querySelector('li[data-ticket-id="' + cssEscape(tid) + '"]');
      if (card && card.querySelector('[data-label-id="' + cssEscape(labelId) + '"]')) count += 1;
    });
    return count;
  }

  function updateLabelMarkers(menu) {
    if (!menu) return;
    var total = selected.size;
    menu.querySelectorAll("[data-nd-bulk-label-option][data-value]").forEach(function (opt) {
      var count = labelSelectionCount(opt.getAttribute("data-value"));
      var marker = opt.querySelector("[data-nd-bulk-label-marker]");
      if (marker) marker.textContent = count === 0 ? "" : (count === total ? "✓" : "◐");
      opt.setAttribute("data-nd-bulk-label-state", count === 0 ? "none" : (count === total ? "all" : "some"));
      opt.setAttribute("aria-selected", count > 0 ? "true" : "false");
    });
  }

  function fuzzyMatch(text, query) {
    text = (text || "").toLowerCase();
    query = (query || "").toLowerCase();
    if (!query) return true;
    var pos = 0;
    for (var i = 0; i < query.length; i += 1) {
      pos = text.indexOf(query[i], pos);
      if (pos === -1) return false;
      pos += 1;
    }
    return true;
  }

  function applyLabelFilter(menu) {
    if (!menu) return;
    var search = menu.querySelector("[data-nd-bulk-label-search]");
    if (!search) return;
    var q = (search.value || "").trim();
    var shown = 0;
    var exact = false;
    menu.querySelectorAll("[data-nd-bulk-label-option]").forEach(function (opt) {
      var text = opt.getAttribute("data-search-text") || opt.textContent || "";
      var match = fuzzyMatch(text, q);
      opt.hidden = !match;
      if (match) shown += 1;
      if (text.toLowerCase() === q.toLowerCase()) exact = true;
    });
    var empty = menu.querySelector("[data-nd-bulk-label-empty]");
    if (empty) {
      var showEmpty = shown === 0 && !q;
      empty.hidden = !showEmpty;
      empty.classList.toggle("hidden", !showEmpty);
    }
    var create = menu.querySelector("[data-nd-bulk-create-option]");
    if (create) {
      var name = create.querySelector("[data-nd-bulk-create-name]");
      if (name) name.textContent = q ? '"' + q + '"' : "";
      var showCreate = !!q && !exact;
      create.hidden = !showCreate;
      create.classList.toggle("hidden", !showCreate);
    }
  }

  function apply(prop, value, action) {
    var cfg = ROUTES[prop];
    if (!cfg) return;
    var picked = ids();
    if (!picked.length) return;
    var params = { ticket_ids: picked.join(",") };
    params[cfg.field] = value;
    if (prop === "labels") params.label_action = action || "add";
    var applyMenuName = openMenuName();
    closeMenus();
    postForm(cfg.url, params).then(function (r) {
      if (!r.ok) {
        toast("Couldn't update " + describe(prop) + " (" + r.status + ")");
        return;
      }
      return r.json().then(function (body) {
        var n = (body.updated || []).length;
        var text = "Updated " + describe(prop) + " on " + n + " ticket" +
                   (n === 1 ? "" : "s");
        text += skipReasonsSuffix(body.skipped);
        if (prop === "labels") addLabelOption(body.label);
        refreshBoardSurfaces();
        updateLabelMarkers(bar() && bar().querySelector('[data-nd-bulk-menu="labels"]'));
        toast(text, body.undo || null);
        var b = bar();
        if (b && !b.hidden) focusToggleFor(applyMenuName);
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
          var text = "Archived " + n + " ticket" + (n === 1 ? "" : "s");
          text += skipReasonsSuffix(body.skipped);
          // Archived tickets leave the visible columns; drop them from the set.
          (body.updated || []).forEach(function (u) { selected.delete(u.id); });
          sync();
          refreshBoardSurfaces();
          toast(text, body.undo || null);
        });
      }, function () { toast("Couldn't archive"); });
  }

  function createLabel(form) {
    var input = form.querySelector('input[name="label_name"]');
    var name = input && input.value.trim();
    if (!name) {
      if (input) input.focus();
      return;
    }
    var picked = ids();
    if (!picked.length) return;
    var params = {
      ticket_ids: picked.join(","),
      label_name: name,
      label_color: (form.querySelector('input[name="label_color"]') || {}).value || "",
    };
    var createMenuName = openMenuName();
    postForm("/board/tickets/bulk/labels", params).then(function (r) {
      if (!r.ok) { toast("Couldn't create label (" + r.status + ")"); return; }
      return r.json().then(function (body) {
        var n = (body.updated || []).length;
        var skipped = (body.skipped || []).length;
        var text = "Applied label to " + n + " ticket" + (n === 1 ? "" : "s");
        if (skipped) text += " · " + skipped + " skipped";
        addLabelOption(body.label);
        if (input) input.value = "";
        closeMenus();
        refreshBoardSurfaces();
        toast(text, body.undo || null);
        var b = bar();
        if (b && !b.hidden) focusToggleFor(createMenuName);
      });
    }, function () { toast("Couldn't create label"); });
  }

  function commitActive(menu) {
    var active = activeOption(menu) || visibleOptions(menu)[0];
    if (!active) return;
    if (active.matches("[data-nd-bulk-create-option]")) {
      var form = menu.querySelector("[data-nd-bulk-create-label]");
      if (form) createLabel(form);
      return;
    }
    if (active.matches("[data-nd-bulk-apply]")) {
      var prop = active.getAttribute("data-nd-bulk-apply");
      var action = prop === "labels" && active.getAttribute("data-nd-bulk-label-state") !== "none"
        ? "remove"
        : "add";
      apply(prop, active.getAttribute("data-value"), action);
      return;
    }
  }

  function wireBar() {
    if (barWired) return;
    var b = bar();
    if (!b) return;
    barWired = true;
    document.addEventListener("click", function (e) {
      var toggle = e.target.closest("[data-nd-bulk-toggle]");
      if (toggle) { e.preventDefault(); toggleMenu(toggle.getAttribute("data-nd-bulk-toggle")); return; }
      var opt = e.target.closest("[data-nd-bulk-apply]");
      if (opt) {
        e.preventDefault();
        var prop = opt.getAttribute("data-nd-bulk-apply");
        var action = prop === "labels" && opt.getAttribute("data-nd-bulk-label-state") !== "none"
          ? "remove"
          : "add";
        apply(prop, opt.getAttribute("data-value"), action);
        return;
      }
      var createOpt = e.target.closest("[data-nd-bulk-create-option]");
      if (createOpt) {
        e.preventDefault();
        createLabel(createOpt.closest("[data-nd-bulk-menu]").querySelector("[data-nd-bulk-create-label]"));
        return;
      }
      if (e.target.closest("[data-nd-bulk-archive]")) { e.preventDefault(); archive(); return; }
      if (e.target.closest("[data-nd-bulk-clear]")) { e.preventDefault(); clear(); return; }
    });
    document.addEventListener("submit", function (e) {
      var form = e.target.closest("[data-nd-bulk-create-label]");
      if (!form) return;
      e.preventDefault();
      createLabel(form);
    });
    document.addEventListener("input", function (e) {
      var search = e.target.closest("[data-nd-bulk-label-search]");
      if (!search) return;
      var menu = search.closest("[data-nd-bulk-menu]");
      applyLabelFilter(menu);
      setActive(menu, visibleOptions(menu)[0]);
    });
    document.addEventListener("keydown", function (e) {
      // Close the bulk menu on Esc regardless of where focus is. Without this
      // guard, Esc only fired when focus was already inside the menu element;
      // clicking a card then pressing Esc left the menu stranded open.
      if (e.key === "Escape" || e.key === "Esc") {
        var openBulkMenu = document.querySelector("[data-nd-bulk-menu]:not([hidden])");
        if (openBulkMenu) {
          e.preventDefault();
          var escapedMenuName = openBulkMenu.getAttribute("data-nd-bulk-menu");
          closeMenus();
          focusToggleFor(escapedMenuName);
          return;
        }
      }
      var menu = e.target.closest("[data-nd-bulk-menu]");
      if (!menu || menu.hidden) return;
      if (e.key === "ArrowDown") {
        e.preventDefault();
        moveActive(menu, 1);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        moveActive(menu, -1);
        return;
      }
      if (e.key === "Enter") {
        e.preventDefault();
        commitActive(menu);
      }
    });
  }

  // Close menus when clicking outside the bar.
  document.addEventListener("click", function (e) {
    var b = bar();
    if (!b || b.hidden) return;
    if (!e.target.closest || !e.target.closest("#nd-bulk-bar, [data-nd-bulk-menu]")) closeMenus();
  });

  // ---- init --------------------------------------------------------------

  function init() {
    wireBar();
    sync();
    // Pre-stamp selection state onto incoming fragments BEFORE they hit the DOM.
    // oobBeforeSwap (htmx 2.0.3) exposes event.detail.fragment for OOB swaps —
    // the board's 3 s column poll uses these.  beforeSwap does not carry a
    // fragment for non-OOB swaps (list's 4 s innerHTML poll), so that listener
    // is a no-op; the afterSwap safety net below covers that path.
    document.body.addEventListener("htmx:oobBeforeSwap", function (evt) {
      stampFragment(evt.detail && evt.detail.fragment);
    });
    document.body.addEventListener("htmx:beforeSwap", function (evt) {
      stampFragment(evt.detail && evt.detail.fragment);
    });
    // Safety-net: re-apply after every swap so the bar stays wired and any
    // path not covered by the pre-stamp (e.g. list innerHTML swaps) is fixed.
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
