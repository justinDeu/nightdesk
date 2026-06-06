// command_palette.js
//
// Keyboard navigation for the nightdesk board: a Ctrl/Cmd+K command palette
// plus a small set of global shortcuts and focused-ticket property actions.
// Pure client JS, no build step.
//
// PROGRESSIVE ENHANCEMENT: every action here is also reachable by mouse
// (header search box, "+ New ticket" button, per-ticket Run-now / Archive /
// Requeue controls, nav links, property picker chips). If this script fails
// to load nothing breaks; the shortcuts simply don't fire.
//
// AUTH NOTE: the JSON API under /api/v1/* is bearer-only and 401s for the
// browser's cookie session, so the palette talks to the cookie-authed UI
// twins instead:
//   - search   GET  /header/search?q=        (HTML partial, parsed below)
//   - run-now  POST /tickets/{id}/run-now
//   - archive  POST /tickets/{id}/archive
//   - requeue  POST /tickets/{id}/requeue
//   - open     navigate to /tickets/{id}
// No new server routes were added for this feature.

(function () {
  "use strict";

  var SEARCH_MIN = 2; // matches /header/search _MIN_QUERY_LEN
  var SEARCH_DEBOUNCE = 180;

  // ---- DOM handles (resolved lazily; the partial is in base.html) --------
  function palette() { return document.getElementById("nd-command-palette"); }
  function cheatsheet() { return document.getElementById("nd-shortcuts-cheatsheet"); }
  function input() { return document.getElementById("nd-cmdk-input"); }
  function list() { return document.getElementById("nd-cmdk-list"); }

  // ---- helpers -----------------------------------------------------------

  function isTypingTarget(el) {
    if (!el) return false;
    if (el.isContentEditable) return true;
    var tag = (el.tagName || "").toLowerCase();
    return tag === "input" || tag === "textarea" || tag === "select";
  }

  // A dialog other than ours is open: don't hijack keys (e.g. the ticket
  // edit/create modal). Our own palette/cheatsheet are handled explicitly.
  function foreignDialogOpen() {
    var dialogs = document.querySelectorAll("dialog[open]");
    for (var i = 0; i < dialogs.length; i++) {
      var d = dialogs[i];
      if (d.id !== "nd-command-palette" && d.id !== "nd-shortcuts-cheatsheet") {
        return true;
      }
    }
    return false;
  }

  // Subsequence fuzzy match with a light score. Returns null on no match.
  // Higher score = better (contiguous / word-start matches rank up).
  function fuzzy(query, text) {
    if (!query) return 0;
    var q = query.toLowerCase();
    var t = text.toLowerCase();
    var qi = 0, score = 0, streak = 0, prev = -1;
    for (var ti = 0; ti < t.length && qi < q.length; ti++) {
      if (t[ti] === q[qi]) {
        streak += 1;
        score += streak; // reward consecutive runs
        if (ti === 0 || /[\s\-_/]/.test(t[ti - 1])) score += 3; // word start
        if (prev === ti - 1) score += 1;
        prev = ti;
        qi += 1;
      } else {
        streak = 0;
      }
    }
    return qi === q.length ? score : null;
  }

  // ---- cursor / focused ticket -------------------------------------------
  // On /tickets/{id} the focused ticket is the page. On the board the cursor
  // tracks a position in the card list; J/K move it, and the sidebar opens
  // when the cursor lands on a card. The cursor delegates to the existing
  // HTMX sidebar swap so property pickers in the sidebar are always wired.

  function cssEscape(s) {
    if (window.CSS && CSS.escape) return CSS.escape(s);
    return String(s).replace(/["\\\]]/g, "\\$&");
  }

  // Get the currently focused ticket context. On the board this is the
  // cursor-selected card; on /tickets/{id} it's the page ticket.
  function currentTicket() {
    // Ticket detail page — the focused ticket IS the page.
    var m = location.pathname.match(/^\/tickets\/([^/]+)\/?$/);
    if (m) {
      return { id: m[1], title: document.title.replace(/ \| nightdesk$/, "") };
    }
    // Board — use the sidebar's selected ticket, or fall back to the
    // cursor-tracked card.
    var sidebar = document.getElementById("sidebar");
    var sel = sidebar && sidebar.getAttribute("data-selected-ticket-id");
    if (!sel) {
      // Check the keyboard cursor
      var cursor = document.querySelector("li[data-ticket-id][data-nd-cursor]");
      if (cursor) sel = cursor.getAttribute("data-ticket-id");
    }
    if (sel) {
      var card = document.querySelector('li[data-ticket-id="' + cssEscape(sel) + '"]');
      var title = card
        ? (card.querySelector(".font-medium, .font-bold") || {}).textContent
        : null;
      return { id: sel, title: (title || "selected ticket").trim() };
    }
    return null;
  }

  // ---- board cursor: J/K navigation between cards ------------------------

  // Collect all board cards in visual order (left-to-right columns, then
  // top-to-bottom within each column).
  function boardCards() {
    var out = [];
    var columns = document.querySelectorAll("section[data-column]");
    columns.forEach(function (col) {
      col.querySelectorAll("li[data-ticket-id]").forEach(function (card) {
        out.push(card);
      });
    });
    return out;
  }

  function cursorIndex() {
    var cards = boardCards();
    for (var i = 0; i < cards.length; i++) {
      if (cards[i].hasAttribute("data-nd-cursor")) return i;
    }
    // Fall back to the sidebar-selected card.
    var sidebar = document.getElementById("sidebar");
    var sel = sidebar && sidebar.getAttribute("data-selected-ticket-id");
    if (sel) {
      for (var j = 0; j < cards.length; j++) {
        if (cards[j].getAttribute("data-ticket-id") === sel) return j;
      }
    }
    return -1;
  }

  function setCursor(idx) {
    var cards = boardCards();
    if (!cards.length) return;
    // Clamp to bounds.
    if (idx < 0) idx = 0;
    if (idx >= cards.length) idx = cards.length - 1;

    // Clear old cursor highlight.
    var prev = document.querySelector("li[data-ticket-id][data-nd-cursor]");
    if (prev) {
      prev.removeAttribute("data-nd-cursor");
      prev.classList.remove("nd-cursor-active");
    }

    var card = cards[idx];
    card.setAttribute("data-nd-cursor", "");
    card.classList.add("nd-cursor-active");
    card.scrollIntoView({ block: "nearest" });

    // Open the sidebar for this card (same as clicking it), so property
    // pickers are wired in the sidebar.
    var tid = card.getAttribute("data-ticket-id");
    var sidebar = document.getElementById("sidebar");
    if (sidebar && typeof window.htmx !== "undefined") {
      window.htmx.ajax("GET", "/board/sidebar?ticket_id=" + encodeURIComponent(tid), {
        target: "#sidebar",
        swap: "outerHTML",
      });
    }
  }

  function moveCursor(delta) {
    // Only works on the board page.
    if (!document.getElementById("board-grid")) return false;
    var idx = cursorIndex();
    if (idx < 0) {
      // No cursor yet — place it on the first card.
      setCursor(0);
      return true;
    }
    setCursor(idx + delta);
    return true;
  }

  // Re-apply cursor highlight after HTMX swaps replace cards.
  function restoreCursor() {
    var sidebar = document.getElementById("sidebar");
    var sel = sidebar && sidebar.getAttribute("data-selected-ticket-id");
    if (!sel) return;
    var cards = boardCards();
    for (var i = 0; i < cards.length; i++) {
      if (cards[i].getAttribute("data-ticket-id") === sel) {
        cards[i].setAttribute("data-nd-cursor", "");
        cards[i].classList.add("nd-cursor-active");
        return;
      }
    }
  }

  // ---- ticket actions (cookie-authed UI endpoints) -----------------------

  function postAction(url) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "HX-Request": "true" },
    });
  }

  function flashStatus(text) {
    // Lightweight transient toast so action commands give feedback without a
    // page reload. Reuses theme variables for consistency.
    var el = document.createElement("div");
    el.textContent = text;
    el.setAttribute("role", "status");
    el.style.cssText =
      "position:fixed;left:50%;bottom:24px;transform:translateX(-50%);" +
      "z-index:120;background:var(--color-bg-elev);color:var(--color-fg);" +
      "border:1px solid var(--color-border);border-radius:.375rem;" +
      "padding:.5rem .875rem;font-size:.8125rem;box-shadow:0 8px 20px rgba(0,0,0,.35);";
    document.body.appendChild(el);
    setTimeout(function () { el.remove(); }, 1800);
  }

  function runTicketAction(verb, id, title) {
    var routes = {
      "run-now": "/tickets/" + encodeURIComponent(id) + "/run-now",
      archive: "/tickets/" + encodeURIComponent(id) + "/archive",
      requeue: "/tickets/" + encodeURIComponent(id) + "/requeue",
    };
    var labels = { "run-now": "Queued", archive: "Archived", requeue: "Requeued" };
    postAction(routes[verb]).then(function (r) {
      if (r.ok || r.status === 204) {
        flashStatus(labels[verb] + ": " + (title || id));
        // Nudge the board to refresh its columns if we're on it.
        var poll = document.getElementById("board-columns-poll");
        if (poll && typeof window.htmx !== "undefined") {
          try { window.htmx.trigger(poll, "poll"); } catch (e) {}
        }
      } else if (r.status === 409) {
        flashStatus("Can't " + verb + " this ticket right now");
      } else if (r.status === 404) {
        flashStatus("Ticket not found");
      } else {
        flashStatus("Action failed (" + r.status + ")");
      }
    }).catch(function () {
      flashStatus("Action failed");
    });
  }

  function openModalEl(modal) {
    if (modal && typeof modal.showModal === "function") {
      try { modal.showModal(); return true; } catch (e) {}
    }
    return false;
  }

  // Opens the create-ticket modal from ANY page. If the page embeds the modal
  // inline (the board), pop it directly. Otherwise lazy-load the partial into
  // #create-modal-host via HTMX (which runs the partial's own init script),
  // then open it. Falls back to navigating to the board if HTMX is missing.
  function openCreateTicket() {
    if (openModalEl(document.getElementById("ticket-create-modal"))) return;
    var host = document.getElementById("create-modal-host");
    if (!host || typeof window.htmx === "undefined") {
      location.href = "/?new=1";
      return;
    }
    window.htmx
      .ajax("GET", "/board/new-ticket-modal", { target: "#create-modal-host", swap: "innerHTML" })
      .then(function () {
        openModalEl(document.getElementById("ticket-create-modal"));
      });
  }
  window.ndOpenCreateTicket = openCreateTicket;

  // ---- focused ticket: property action shortcuts -------------------------
  // These require a focused ticket (from the cursor on the board, or the
  // page context on ticket detail). They reuse the same picker/update paths
  // as mouse actions: ndOpenPropertyPicker for L/P/S, postAction for A/R,
  // the edit modal for E, and a future peek panel for Space.

  function openEditForTicket(ctx) {
    // Board: the sidebar already carries an edit modal for the selected ticket.
    var editModal = document.getElementById("ticket-edit-modal");
    if (editModal) {
      openModalEl(editModal);
      return;
    }
    // Ticket detail page also has the edit modal.
    editModal = document.querySelector('dialog[id="ticket-edit-modal"]');
    if (editModal) {
      openModalEl(editModal);
      return;
    }
    // Fallback: navigate to the ticket page.
    location.href = "/tickets/" + encodeURIComponent(ctx.id);
  }

  function smartRunOrRequeue(ctx) {
    // "R" triggers run-now if the ticket can be run, otherwise requeue.
    // The valid run-now statuses: draft, queued, review, archived.
    // The valid requeue statuses: review, archived.
    // We try run-now first; if the ticket is in running we show feedback.
    // Use the sidebar or card to infer status.
    var card = document.querySelector('li[data-ticket-id="' + cssEscape(ctx.id) + '"]');
    var status = "";
    if (card) {
      // Card is inside a column with data-status.
      var col = card.closest("section[data-column]");
      if (col) status = col.getAttribute("data-column") || "";
    }
    // On ticket detail, look for status chip.
    if (!status) {
      var chip = document.querySelector('[data-property-chip="' + ctx.id + ':status"]');
      if (chip) status = (chip.textContent || "").trim().toLowerCase();
    }

    if (status === "running") {
      flashStatus("Ticket is already running");
      return;
    }
    if (status === "review" || status === "archived") {
      runTicketAction("requeue", ctx.id, ctx.title);
      return;
    }
    // Default: try run-now (works for draft, queued, and any other valid state).
    runTicketAction("run-now", ctx.id, ctx.title);
  }

  function openLabelPicker(ctx) {
    // Labels may not be registered in the property picker yet. Check for the
    // "labels" property in the registry; if absent, flash a message and leave
    // a clear hook for the future implementation.
    var ok = window.ndOpenPropertyPicker &&
      window.ndOpenPropertyPicker(ctx.id, "labels");
    if (!ok) {
      flashStatus("Label picker: select a ticket first, or labels not yet configured");
    }
  }

  function openPeek(ctx) {
    // Lightweight peek panel — hook for future implementation.
    // If a peek panel component exists (window.ndPeekTicket), delegate to it.
    // Otherwise flash a placeholder message.
    if (typeof window.ndPeekTicket === "function") {
      window.ndPeekTicket(ctx.id);
      return;
    }
    flashStatus("Peek: " + (ctx.title || ctx.id));
  }

  // ---- command model -----------------------------------------------------
  // Each command: { label, shortcut, hint, section, run }.
  //   shortcut  — the direct keyboard key(s), shown as a kbd badge so users
  //               discover shortcuts from the palette and learn to bypass it.
  //   hint      — secondary context (e.g. "draft"), shown as muted text.
  //   section   — groups commands visually (header row in the list).
  // Built fresh per open so the current ticket context and status are accurate.

  function focusedTicketStatus(ctx) {
    if (!ctx) return "";
    var card = document.querySelector('li[data-ticket-id="' + cssEscape(ctx.id) + '"]');
    if (card) {
      var col = card.closest("section[data-column]");
      if (col) return (col.getAttribute("data-column") || "").toLowerCase();
    }
    var chip = document.querySelector('[data-property-chip="' + ctx.id + ':status"]');
    if (chip) return (chip.textContent || "").trim().toLowerCase();
    return "";
  }

  // Status-aware sort order for ticket actions. When a ticket is focused we
  // promote the most relevant actions and demote invalid ones. Returns an
  // array of action keys in display order.
  function statusActionOrder(status) {
    // Default order: run → detail → edit → peek → priority → status →
    // labels → archive → requeue
    var order = [
      "run-now", "open-detail", "edit", "peek",
      "priority", "status", "labels", "archive", "requeue",
    ];
    switch (status) {
      case "running":
        // Can't run/requeue/archive while running; show peek & edit first.
        order = ["peek", "edit", "open-detail", "priority", "status", "labels"];
        break;
      case "review":
        // Requeue & archive are the primary actions from review.
        order = ["requeue", "archive", "open-detail", "edit", "peek", "priority", "status", "labels", "run-now"];
        break;
      case "archived":
        // Requeue is the primary action from archived.
        order = ["requeue", "open-detail", "edit", "peek", "priority", "status", "labels", "archive", "run-now"];
        break;
      case "draft":
      case "queued":
        // Run-now is the primary action.
        order = ["run-now", "edit", "open-detail", "peek", "priority", "status", "labels", "archive", "requeue"];
        break;
    }
    return order;
  }

  function baseCommands() {
    var cmds = [];
    var ctx = currentTicket();
    var status = focusedTicketStatus(ctx);

    if (ctx) {
      var short = ctx.title.length > 40 ? ctx.title.slice(0, 39) + "…" : ctx.title;

      // Build ticket-action commands with status-aware ordering.
      var ticketActions = {
        "run-now": {
          label: "Run now: " + short, shortcut: "R", hint: status || "ticket",
          section: "Ticket actions",
          run: function () { runTicketAction("run-now", ctx.id, ctx.title); },
        },
        "open-detail": {
          label: "Open detail: " + short, shortcut: "", hint: "Enter",
          section: "Ticket actions",
          run: function () { location.href = "/tickets/" + encodeURIComponent(ctx.id); },
        },
        "archive": {
          label: "Archive: " + short, shortcut: "A", hint: status || "ticket",
          section: "Ticket actions",
          run: function () { runTicketAction("archive", ctx.id, ctx.title); },
        },
        "requeue": {
          label: "Requeue: " + short, shortcut: "R", hint: status || "ticket",
          section: "Ticket actions",
          run: function () { runTicketAction("requeue", ctx.id, ctx.title); },
        },
        "edit": {
          label: "Edit: " + short, shortcut: "E", hint: "",
          section: "Ticket actions",
          run: function () { openEditForTicket(ctx); },
        },
        "peek": {
          label: "Peek: " + short, shortcut: "Space", hint: "",
          section: "Ticket actions",
          run: function () { openPeek(ctx); },
        },
        "priority": {
          label: "Set priority: " + short, shortcut: "P", hint: "",
          section: "Properties",
          run: function () {
            var ok = window.ndOpenPropertyPicker &&
              window.ndOpenPropertyPicker(ctx.id, "priority");
            if (!ok) flashStatus("Open the ticket to change its priority");
          },
        },
        "status": {
          label: "Set status: " + short, shortcut: "S", hint: "",
          section: "Properties",
          run: function () {
            var ok = window.ndOpenPropertyPicker &&
              window.ndOpenPropertyPicker(ctx.id, "status");
            if (!ok) flashStatus("Open the ticket to change its status");
          },
        },
        "labels": {
          label: "Set labels: " + short, shortcut: "L", hint: "",
          section: "Properties",
          run: function () { openLabelPicker(ctx); },
        },
      };

      var actionOrder = statusActionOrder(status);
      actionOrder.forEach(function (key) {
        var cmd = ticketActions[key];
        if (!cmd) return;
        // For running tickets, skip run-now/requeue/archive (they'd fail).
        if (status === "running" && (key === "run-now" || key === "requeue" || key === "archive")) return;
        cmds.push(cmd);
      });
    }

    // --- View / display commands ---
    // Toggle board/list view, focus search, future saved-views hook.
    var isBoard = !!document.getElementById("board-grid");
    var isRuns = !!document.querySelector("[data-board-view='runs']");
    if (isBoard || isRuns) {
      cmds.push({
        label: isRuns ? "Switch to tickets view" : "Switch to runs view",
        shortcut: "", hint: "view toggle",
        section: "View",
        run: function () {
          var btn = document.querySelector(
            isRuns ? '[data-sb-view="tickets"]' : '[data-sb-view="runs"]'
          );
          if (btn) { btn.click(); return; }
          // Fallback: navigate with query param.
          location.href = isRuns ? "/" : "/?view=runs";
        },
      });
    }
    cmds.push({
      label: "Focus search", shortcut: "/", hint: "",
      section: "View",
      run: function () {
        var search = document.querySelector('#header-search input[name="q"]');
        if (search) { search.focus(); search.select(); }
      },
    });
    // Saved views placeholder — active once the saved-views surface exists.
    if (typeof window.ndSavedViews === "function") {
      cmds.push({
        label: "Jump to saved view…", shortcut: "g v", hint: "",
        section: "View",
        run: function () { window.ndSavedViews(); },
      });
    }

    // --- Navigation & general commands ---
    cmds.push({ label: "New ticket", shortcut: "c", hint: "", section: "Navigation",
      run: openCreateTicket });
    cmds.push({ label: "Go to board", shortcut: "g b", hint: "", section: "Navigation",
      run: function () { location.href = "/"; } });
    cmds.push({ label: "Go to archive", shortcut: "g a", hint: "", section: "Navigation",
      run: function () { location.href = "/archive"; } });
    cmds.push({ label: "Show keyboard shortcuts", shortcut: "?", hint: "", section: "Navigation",
      run: openCheatSheet });

    return cmds;
  }

  // ---- ticket search (parse the /header/search HTML partial) -------------

  function searchTickets(q) {
    if (!q || q.length < SEARCH_MIN) return Promise.resolve([]);
    return fetch("/header/search?q=" + encodeURIComponent(q), {
      credentials: "same-origin",
    })
      .then(function (r) { return r.ok ? r.text() : ""; })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, "text/html");
        var anchors = doc.querySelectorAll('a[href^="/tickets/"]');
        var out = [];
        anchors.forEach(function (a) {
          var href = a.getAttribute("href");
          var idm = href.match(/^\/tickets\/([^/?#]+)/);
          if (!idm) return;
          var id = idm[1];
          var titleEl = a.querySelector(".font-bold, .font-medium");
          var title = (titleEl ? titleEl.textContent : a.textContent).trim();
          var statusEl = a.querySelector(".text-accent");
          var status = statusEl ? statusEl.textContent.trim() : "";
          out.push({ id: id, title: title || id, status: status });
        });
        return out;
      })
      .catch(function () { return []; });
  }

  // ---- rendering ---------------------------------------------------------

  var state = { items: [], active: 0, seq: 0 };

  function render(items) {
    state.items = items;
    // Compute active index excluding section headers (only command items).
    if (state.active >= items.length) state.active = Math.max(0, items.length - 1);
    var ul = list();
    ul.innerHTML = "";
    if (!items.length) {
      var empty = document.createElement("li");
      empty.className = "px-3 py-3 text-sm text-fg-muted";
      empty.textContent = "No matches";
      ul.appendChild(empty);
      return;
    }
    items.forEach(function (it, i) {
      // Section header — not selectable, not an option.
      if (it.section && !it.run) {
        var hdr = document.createElement("li");
        hdr.className = "px-3 pt-3 pb-1 text-[11px] font-semibold uppercase tracking-wide text-fg-muted";
        hdr.setAttribute("role", "presentation");
        hdr.textContent = it.section;
        ul.appendChild(hdr);
        return;
      }
      var li = document.createElement("li");
      li.id = "nd-cmdk-opt-" + i;
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", i === state.active ? "true" : "false");
      li.className =
        "flex items-center gap-3 px-3 py-2 text-sm cursor-pointer " +
        (i === state.active ? "bg-bg-elev-2 text-fg" : "text-fg-muted");
      var label = document.createElement("span");
      label.className = "flex-1 min-w-0 truncate text-fg";
      label.textContent = it.label;
      li.appendChild(label);
      // Show shortcut as a kbd badge (teaches direct shortcuts).
      if (it.shortcut) {
        var kbd = document.createElement("kbd");
        kbd.className =
          "shrink-0 rounded border border-border bg-bg-elev-2 " +
          "px-1.5 py-0.5 font-mono text-[11px] text-fg";
        kbd.textContent = it.shortcut;
        li.appendChild(kbd);
      }
      // Show context hint as muted text (e.g. "draft", "view toggle").
      if (it.hint) {
        var hint = document.createElement("span");
        hint.className = "shrink-0 text-[11px] text-fg-muted";
        hint.textContent = it.hint;
        li.appendChild(hint);
      }
      li.addEventListener("mousemove", function () { setActive(i); });
      li.addEventListener("click", function () { execute(i); });
      ul.appendChild(li);
    });
    syncActiveAttr();
  }

  function syncActiveAttr() {
    var ul = list();
    var children = ul.children;
    for (var i = 0; i < children.length; i++) {
      // Skip section headers — they aren't selectable.
      if (children[i].getAttribute("role") === "presentation") continue;
      var on = i === state.active;
      children[i].setAttribute("aria-selected", on ? "true" : "false");
      children[i].className =
        "flex items-center gap-3 px-3 py-2 text-sm cursor-pointer " +
        (on ? "bg-bg-elev-2 text-fg" : "text-fg-muted");
    }
    var inp = input();
    if (inp && children[state.active]) {
      inp.setAttribute("aria-activedescendant", children[state.active].id);
      children[state.active].scrollIntoView({ block: "nearest" });
    }
  }

  function setActive(i) {
    if (i < 0 || i >= state.items.length) return;
    // Skip over section headers.
    if (state.items[i] && state.items[i].section && !state.items[i].run) return;
    state.active = i;
    syncActiveAttr();
  }

  function move(delta) {
    if (!state.items.length) return;
    var n = state.items.length;
    var next = state.active;
    // Skip over section headers when navigating.
    for (var steps = 0; steps < n; steps++) {
      next = (next + delta + n) % n;
      if (state.items[next] && state.items[next].run) break;
    }
    state.active = next;
    syncActiveAttr();
  }

  function execute(i) {
    var idx = typeof i === "number" ? i : state.active;
    var it = state.items[idx];
    if (!it || !it.run) return;
    closePalette();
    // Defer so the dialog has closed before navigation/fetch side effects.
    setTimeout(function () { it.run(); }, 0);
  }

  // ---- query handling ----------------------------------------------------

  // Build display items from matched commands, inserting section headers.
  // Each item is either { section: "Name" } (a header) or a regular command.
  function buildItems(matched) {
    var items = [];
    var lastSection = "";
    matched.forEach(function (m) {
      if (m.cmd.section && m.cmd.section !== lastSection) {
        lastSection = m.cmd.section;
        items.push({ section: lastSection });
      }
      items.push({
        label: m.cmd.label,
        shortcut: m.cmd.shortcut || "",
        hint: m.cmd.hint || "",
        section: m.cmd.section || "",
        run: m.cmd.run,
      });
    });
    return items;
  }

  function refresh() {
    var q = (input().value || "").trim();
    var cmds = baseCommands();
    var matched;
    if (!q) {
      matched = cmds.map(function (c) { return { cmd: c, score: 0 }; });
    } else {
      matched = [];
      cmds.forEach(function (c) {
        var s = fuzzy(q, c.label);
        if (s !== null) matched.push({ cmd: c, score: s });
      });
      matched.sort(function (a, b) { return b.score - a.score; });
    }
    var items = buildItems(matched);

    // The query language filters the ticket results: status=review, project=x,
    // cost>0.5, free text, etc. all go through /header/search. Guard against
    // out-of-order responses with a sequence token.
    var mySeq = ++state.seq;
    render(items);
    searchTickets(q).then(function (tickets) {
      if (mySeq !== state.seq) return; // stale
      var ticketItems = tickets.map(function (t) {
        return {
          label: t.title,
          shortcut: "",
          hint: t.status ? "ticket · " + t.status : "ticket",
          section: "Search results",
          run: function () { location.href = "/tickets/" + encodeURIComponent(t.id); },
        };
      });
      render(items.concat(ticketItems));
    });
  }

  // ---- open / close ------------------------------------------------------

  function openPalette() {
    var dlg = palette();
    if (!dlg) return;
    if (cheatsheet() && cheatsheet().open) cheatsheet().close();
    if (!dlg.open) {
      try { dlg.showModal(); } catch (e) { return; }
    }
    var inp = input();
    inp.value = "";
    state.active = 0;
    refresh();
    inp.focus();
    inp.select();
  }

  function closePalette() {
    var dlg = palette();
    if (dlg && dlg.open) dlg.close();
  }

  function openCheatSheet() {
    var dlg = cheatsheet();
    if (!dlg) return;
    if (palette() && palette().open) palette().close();
    if (!dlg.open) {
      try { dlg.showModal(); } catch (e) {}
    }
  }

  // ---- wiring ------------------------------------------------------------

  function wirePaletteInput() {
    var inp = input();
    var dlg = palette();
    if (!inp || !dlg || inp.__ndWired) return;
    inp.__ndWired = true;

    var timer = null;
    inp.addEventListener("input", function () {
      clearTimeout(timer);
      timer = setTimeout(refresh, SEARCH_DEBOUNCE);
    });

    inp.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") { e.preventDefault(); move(1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); move(-1); }
      else if (e.key === "Enter") { e.preventDefault(); execute(); }
      // Esc falls through to the dialog's native close.
    });

    // Click on the backdrop closes (native <dialog> doesn't on its own).
    dlg.addEventListener("click", function (e) {
      if (e.target === dlg) dlg.close();
    });
  }

  // ---- global shortcuts --------------------------------------------------

  var lastG = 0; // timestamp of a recent bare 'g' for the g-then-x chords

  function onKeydown(e) {
    // Ctrl/Cmd+K: open palette from anywhere, even inside inputs.
    var key = e.key;
    if ((e.metaKey || e.ctrlKey) && (key === "k" || key === "K")) {
      e.preventDefault();
      if (palette() && palette().open) closePalette();
      else openPalette();
      return;
    }

    // Everything below is a single-key shortcut: ignore modifier combos,
    // typing contexts, and foreign open dialogs.
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (isTypingTarget(e.target)) return;
    if (foreignDialogOpen()) return;

    // If our own palette/cheatsheet is open, let their own handlers / native
    // dialog behavior deal with keys.
    if ((palette() && palette().open) || (cheatsheet() && cheatsheet().open)) return;

    // If a property picker menu is open, don't hijack keys — the picker's
    // own handler manages Arrow/Enter/Escape.
    if (document.querySelector("[data-property-menu]:not(.hidden)")) return;

    var now = Date.now();
    var hadG = now - lastG < 800;

    if (hadG && (key === "b" || key === "B")) {
      e.preventDefault(); lastG = 0; location.href = "/"; return;
    }
    if (hadG && (key === "a" || key === "A")) {
      e.preventDefault(); lastG = 0; location.href = "/archive"; return;
    }
    lastG = 0;

    if (key === "g" || key === "G") { lastG = now; return; }
    if (key === "c" || key === "C") { e.preventDefault(); openCreateTicket(); return; }
    if (key === "/") {
      e.preventDefault();
      var search = document.querySelector('#header-search input[name="q"]');
      if (search) { search.focus(); search.select(); }
      return;
    }
    if (key === "?") { e.preventDefault(); openCheatSheet(); return; }

    // --- Board cursor: J/K navigate cards (vim-style down/up) ------------
    if (key === "j" || key === "J") {
      if (document.getElementById("board-grid")) {
        e.preventDefault();
        moveCursor(1);
      }
      return;
    }
    if (key === "k" || key === "K") {
      if (document.getElementById("board-grid")) {
        e.preventDefault();
        moveCursor(-1);
      }
      return;
    }

    // --- Focused-ticket property actions ----------------------------------
    // These require a selected/focused ticket context.
    var ctx = currentTicket();

    // E — edit the focused ticket
    if (key === "e" || key === "E") {
      if (ctx) { e.preventDefault(); openEditForTicket(ctx); }
      return;
    }
    // P — priority picker
    if (key === "p" || key === "P") {
      if (ctx) {
        e.preventDefault();
        var ok = window.ndOpenPropertyPicker &&
          window.ndOpenPropertyPicker(ctx.id, "priority");
        if (!ok) flashStatus("Open the ticket to change its priority");
      }
      return;
    }
    // S — status picker
    if (key === "s" || key === "S") {
      if (ctx) {
        e.preventDefault();
        var ok2 = window.ndOpenPropertyPicker &&
          window.ndOpenPropertyPicker(ctx.id, "status");
        if (!ok2) flashStatus("Open the ticket to change its status");
      }
      return;
    }
    // L — label picker (hook for future labels property; falls back gracefully)
    if (key === "l" || key === "L") {
      if (ctx) { e.preventDefault(); openLabelPicker(ctx); }
      return;
    }
    // A — archive the focused ticket
    if (key === "a" || key === "A") {
      if (ctx) { e.preventDefault(); runTicketAction("archive", ctx.id, ctx.title); }
      return;
    }
    // R — run/requeue the focused ticket
    if (key === "r" || key === "R") {
      if (ctx) { e.preventDefault(); smartRunOrRequeue(ctx); }
      return;
    }
    // Space — peek (hook for future lightweight preview panel)
    if (key === " ") {
      if (ctx) { e.preventDefault(); openPeek(ctx); }
      return;
    }
  }

  // ---- init --------------------------------------------------------------

  function init() {
    wirePaletteInput();
    document.addEventListener("keydown", onKeydown, true);

    // Esc blurs the header search box. Without this the global onKeydown
    // early-returns inside typing targets, so Esc would never unfocus it.
    var headerSearch = document.querySelector('#header-search input[name="q"]');
    if (headerSearch && !headerSearch.__ndEscWired) {
      headerSearch.__ndEscWired = true;
      headerSearch.addEventListener("keydown", function (e) {
        if (e.key === "Escape") {
          e.preventDefault();
          e.stopPropagation();
          headerSearch.blur();
        }
      });
    }

    // If we landed on the board via the "c" shortcut from another page,
    // auto-open the create modal and clean the URL so a refresh doesn't
    // re-trigger it.
    if (/[?&]new=1\b/.test(location.search)) {
      var modal = document.getElementById("ticket-create-modal");
      if (modal && typeof modal.showModal === "function") {
        try { modal.showModal(); } catch (e) {}
      }
      var clean = location.pathname + location.hash;
      history.replaceState(null, "", clean);
    }

    // Restore cursor highlight after HTMX column swaps on the board.
    document.body.addEventListener("htmx:afterSwap", function () {
      restoreCursor();
    });
    document.body.addEventListener("htmx:oobAfterSwap", function () {
      restoreCursor();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
