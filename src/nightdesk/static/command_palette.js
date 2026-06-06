// command_palette.js
//
// Keyboard navigation for the nightdesk board: a Ctrl/Cmd+K command palette
// plus a small set of global shortcuts. Pure client JS, no build step.
//
// PROGRESSIVE ENHANCEMENT: every action here is also reachable by mouse
// (header search box, "+ New ticket" button, per-ticket Run-now / Archive /
// Requeue controls, nav links). If this script fails to load nothing breaks;
// the shortcuts simply don't fire.
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

  // ---- current ticket context -------------------------------------------
  // On /tickets/{id} the focused ticket is the page. On the board it's the
  // card whose edit-sidebar is open (data-selected-ticket-id).

  function currentTicket() {
    var m = location.pathname.match(/^\/tickets\/([^/]+)\/?$/);
    if (m) {
      return { id: m[1], title: document.title.replace(/ \| nightdesk$/, "") };
    }
    var sidebar = document.getElementById("sidebar");
    var sel = sidebar && sidebar.getAttribute("data-selected-ticket-id");
    if (sel) {
      var card = document.querySelector('li[data-ticket-id="' + cssEscape(sel) + '"]');
      var title = card
        ? (card.querySelector(".font-medium, .font-bold") || {}).textContent
        : null;
      return { id: sel, title: (title || "selected ticket").trim() };
    }
    return null;
  }

  function cssEscape(s) {
    if (window.CSS && CSS.escape) return CSS.escape(s);
    return String(s).replace(/["\\\]]/g, "\\$&");
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

  // ---- command model -----------------------------------------------------
  // Each command: { label, hint, run }. Built fresh per open so the current
  // ticket context is accurate.

  function baseCommands() {
    var cmds = [];
    var ctx = currentTicket();
    if (ctx) {
      var short = ctx.title.length > 40 ? ctx.title.slice(0, 39) + "…" : ctx.title;
      cmds.push({
        label: "Run now: " + short, hint: "selected ticket",
        run: function () { runTicketAction("run-now", ctx.id, ctx.title); },
      });
      cmds.push({
        label: "Open detail: " + short, hint: "selected ticket",
        run: function () { location.href = "/tickets/" + encodeURIComponent(ctx.id); },
      });
      cmds.push({
        label: "Archive: " + short, hint: "selected ticket",
        run: function () { runTicketAction("archive", ctx.id, ctx.title); },
      });
      cmds.push({
        label: "Requeue: " + short, hint: "selected ticket",
        run: function () { runTicketAction("requeue", ctx.id, ctx.title); },
      });
      // Metadata edits reuse the ONE shared property-picker primitive: the
      // command just opens the relevant chip's popover (on the sidebar or the
      // detail header) rather than shipping its own metadata UI.
      ["priority", "status", "project"].forEach(function (prop) {
        var nice = prop.charAt(0).toUpperCase() + prop.slice(1);
        cmds.push({
          label: "Set " + prop + ": " + short,
          hint: "property",
          run: function () {
            var ok = window.ndOpenPropertyPicker &&
              window.ndOpenPropertyPicker(ctx.id, prop);
            if (!ok) flashStatus("Open the ticket to change its " + prop);
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
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
