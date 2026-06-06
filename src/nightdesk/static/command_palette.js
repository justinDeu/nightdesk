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
  // Each command: { label, hint, run }. Built fresh per open so the current
  // ticket context is accurate.

  function baseCommands() {
    var cmds = [];
    var ctx = currentTicket();
    if (ctx) {
      var short = ctx.title.length > 40 ? ctx.title.slice(0, 39) + "…" : ctx.title;
      cmds.push({
        label: "Run now: " + short, hint: "R",
        run: function () { runTicketAction("run-now", ctx.id, ctx.title); },
      });
      cmds.push({
        label: "Open detail: " + short, hint: "selected ticket",
        run: function () { location.href = "/tickets/" + encodeURIComponent(ctx.id); },
      });
      cmds.push({
        label: "Archive: " + short, hint: "A",
        run: function () { runTicketAction("archive", ctx.id, ctx.title); },
      });
      cmds.push({
        label: "Requeue: " + short, hint: "R",
        run: function () { runTicketAction("requeue", ctx.id, ctx.title); },
      });
      cmds.push({
        label: "Edit: " + short, hint: "E",
        run: function () { openEditForTicket(ctx); },
      });
      cmds.push({
        label: "Peek: " + short, hint: "Space",
        run: function () { openPeek(ctx); },
      });
      // Metadata edits reuse the ONE shared property-picker primitive: the
      // command just opens the relevant chip's popover (on the sidebar or the
      // detail header) rather than shipping its own metadata UI.
      [
        { prop: "labels", key: "L", nice: "Label" },
        { prop: "priority", key: "P", nice: "Priority" },
        { prop: "status", key: "S", nice: "Status" },
      ].forEach(function (p) {
        cmds.push({
          label: "Set " + p.nice + ": " + short,
          hint: p.key,
          run: function () {
            var ok = window.ndOpenPropertyPicker &&
              window.ndOpenPropertyPicker(ctx.id, p.prop);
            if (!ok) flashStatus("Open the ticket to change its " + p.nice.toLowerCase());
          },
        });
      });
    }
    cmds.push({ label: "New ticket", hint: "c", run: openCreateTicket });
    cmds.push({ label: "Go to board", hint: "g b", run: function () { location.href = "/"; } });
    cmds.push({ label: "Go to archive", hint: "g a", run: function () { location.href = "/archive"; } });
    cmds.push({ label: "Show keyboard shortcuts", hint: "?", run: openCheatSheet });
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
      var li = document.createElement("li");
      li.id = "nd-cmdk-opt-" + i;
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", i === state.active ? "true" : "false");
      li.className =
        "flex items-center gap-3 px-3 py-2 text-sm cursor-pointer " +
        (i === state.active ? "bg-bg-elev-2 text-fg" : "text-fg-muted");
      var label = document.createElement("span");
      label.className = "flex-1 min-w-0 truncate " + (i === state.active ? "text-fg" : "text-fg");
      label.textContent = it.label;
      li.appendChild(label);
      if (it.hint) {
        var hint = document.createElement("span");
        hint.className = "shrink-0 text-[11px] text-fg-muted uppercase tracking-wide";
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
    state.active = i;
    syncActiveAttr();
  }

  function move(delta) {
    if (!state.items.length) return;
    var n = state.items.length;
    state.active = (state.active + delta + n) % n;
    syncActiveAttr();
  }

  function execute(i) {
    var idx = typeof i === "number" ? i : state.active;
    var it = state.items[idx];
    if (!it) return;
    closePalette();
    // Defer so the dialog has closed before navigation/fetch side effects.
    setTimeout(function () { it.run(); }, 0);
  }

  // ---- query handling ----------------------------------------------------

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
    var items = matched.map(function (m) {
      return { label: m.cmd.label, hint: m.cmd.hint, run: m.cmd.run };
    });

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
          hint: t.status ? "ticket · " + t.status : "ticket",
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
