// command_palette.js
//
// Keyboard navigation for the nightdesk board and inbox: a Ctrl/Cmd+K command
// palette plus a small set of global shortcuts and focused-ticket property
// actions. Pure client JS, no build step.
//
// PROGRESSIVE ENHANCEMENT: every action here is also reachable by mouse
// (header search box, "+ New ticket" button, per-ticket Run-now / Archive /
// Requeue controls, nav links, property picker chips, inbox promote/decline
// buttons). If this script fails to load nothing breaks; the shortcuts simply
// don't fire.
//
// INBOX TRIAGE SHORTCUTS (active on /inbox):
//   J/K       — next/previous inbox item
//   Enter     — open focused item (ticket detail)
//   A         — accept / promote to queued (422 feedback if ticket incomplete)
//   D         — decline (archive)
//   E         — flesh out / promote (opens modal)
//   L         — edit labels (plain L — no column nav on inbox)
//   P         — set project
//   Shift+P   — set priority
//   S         — unbound (snooze not yet implemented)
//
// AUTH NOTE: the JSON API under /api/v1/* is bearer-only and 401s for the
// browser's cookie session, so the palette talks to the cookie-authed UI
// twins instead:
//   - search   GET  /header/search?q=        (HTML partial, parsed below)
//   - run-now  POST /tickets/{id}/run-now
//   - archive  POST /tickets/{id}/archive
//   - requeue  POST /tickets/{id}/requeue
//   - promote  POST /inbox/tickets/{id}/promote  body: target=<target> (form-urlencoded)
//   - decline  POST /inbox/tickets/{id}/decline
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

  var desiredBoardSidebarTicketId = null;
  var pendingBoardFocus = null;

  function clearBoardCursor() {
    document.querySelectorAll("li[data-ticket-id][data-nd-cursor]").forEach(function (card) {
      card.removeAttribute("data-nd-cursor");
      card.classList.remove("nd-cursor-active");
    });
  }

  function paintBoardSelectedCard(ticketId) {
    var accent = getComputedStyle(document.documentElement).getPropertyValue("--color-accent").trim() || "#34d399";
    document.querySelectorAll("li[data-ticket-id]").forEach(function (card) {
      var match = ticketId && card.getAttribute("data-ticket-id") === ticketId;
      if (match) {
        card.dataset.selected = "1";
        card.style.borderColor = accent;
        card.style.boxShadow = "0 0 0 1px " + accent + ", 0 4px 12px rgba(52, 211, 153, .2)";
        card.style.background = "color-mix(in oklab, " + accent + " 8%, var(--color-bg-elev-2))";
      } else {
        delete card.dataset.selected;
        card.style.borderColor = "";
        card.style.boxShadow = "";
        card.style.background = "";
      }
    });
  }

  function markBoardCursor(card) {
    if (!card) return;
    clearBoardCursor();
    card.setAttribute("data-nd-cursor", "");
    card.classList.add("nd-cursor-active");
    card.scrollIntoView({ block: "nearest" });
  }

  function rememberBoardFocusAfterMutation(ticketId, opts) {
    opts = opts || {};
    if (!document.getElementById("board-grid")) return;
    var cards = boardCards();
    var index = cursorIndex();
    if (index < 0 && ticketId) {
      for (var i = 0; i < cards.length; i++) {
        if (cards[i].getAttribute("data-ticket-id") === ticketId) {
          index = i;
          break;
        }
      }
    }
    pendingBoardFocus = {
      id: ticketId || null,
      index: Math.max(0, index),
      follow: !!opts.follow,
      followUntil: opts.follow ? Date.now() + 8000 : 0,
    };
  }

  function restorePendingBoardFocus() {
    if (!pendingBoardFocus) return false;
    var pending = pendingBoardFocus;
    pendingBoardFocus = null;
    var cards = boardCards();
    if (!cards.length) {
      clearBoardCursor();
      paintBoardSelectedCard(null);
      return true;
    }

    var target = null;
    if (pending.follow && pending.id) {
      for (var i = 0; i < cards.length; i++) {
        if (cards[i].getAttribute("data-ticket-id") === pending.id) {
          target = cards[i];
          break;
        }
      }
    }
    if (!target && pending.follow && Date.now() < pending.followUntil) {
      pendingBoardFocus = pending;
      window.setTimeout(restorePendingBoardFocus, 250);
      return true;
    }
    if (!target) {
      var fallbackCards = cards;
      if (!pending.follow && pending.id) {
        fallbackCards = cards.filter(function (card) {
          return card.getAttribute("data-ticket-id") !== pending.id;
        });
      }
      target = fallbackCards[Math.min(pending.index, fallbackCards.length - 1)];
    }
    var targetId = target && target.getAttribute("data-ticket-id");
    desiredBoardSidebarTicketId = targetId;
    markBoardCursor(target);
    paintBoardSelectedCard(targetId);
    var sidebar = document.getElementById("sidebar");
    if (targetId && sidebar && sidebar.getAttribute("data-selected-ticket-id") !== targetId && typeof window.htmx !== "undefined") {
      window.htmx.ajax("GET", "/board/sidebar?ticket_id=" + encodeURIComponent(targetId), {
        target: "#sidebar",
        swap: "outerHTML",
      });
    }
    return true;
  }

  function sidebarResponseTicketId(html) {
    var m = String(html || "").match(/data-selected-ticket-id=["']([^"']+)["']/);
    return m ? m[1] : null;
  }

  document.body.addEventListener("htmx:beforeSwap", function (evt) {
    var detail = evt.detail || {};
    var target = detail.target;
    if (!target || target.id !== "sidebar" || !desiredBoardSidebarTicketId) return;
    var responseId = sidebarResponseTicketId(detail.xhr && detail.xhr.responseText);
    if (responseId && responseId !== desiredBoardSidebarTicketId) {
      detail.shouldSwap = false;
      evt.preventDefault();
    }
  });

  document.body.addEventListener("htmx:beforeRequest", function (evt) {
    var detail = evt.detail || {};
    var elt = detail.elt;
    if (!elt || !document.getElementById("board-grid")) return;
    var archiveBtn = elt.closest && elt.closest("[data-archive-btn]");
    var deleteBtn = elt.closest && elt.closest("[hx-delete]");
    if (!archiveBtn && !deleteBtn) return;
    var sidebar = document.getElementById("sidebar");
    var tid = sidebar && sidebar.getAttribute("data-selected-ticket-id");
    rememberBoardFocusAfterMutation(tid, { follow: false });
  });

  document.addEventListener("click", function (evt) {
    var card = evt.target && evt.target.closest && evt.target.closest("li[data-ticket-id]");
    if (!card || !document.getElementById("board-grid")) return;
    desiredBoardSidebarTicketId = card.getAttribute("data-ticket-id");
  });

  // Get the currently focused ticket context. On the board this is the
  // cursor-selected card; on /inbox it's the inbox cursor; on /tickets/{id}
  // it's the page ticket.
  function currentTicket() {
    // Ticket detail page — the focused ticket IS the page.
    var m = location.pathname.match(/^\/tickets\/([^/]+)\/?$/);
    if (m) {
      return { id: m[1], title: document.title.replace(/ \| nightdesk$/, "") };
    }
    // Inbox — use the inbox cursor.
    if (isInboxPage()) {
      var ic = inboxCurrentTicket();
      if (ic) return ic;
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

    var card = cards[idx];
    markBoardCursor(card);

    // Open the sidebar for this card (same as clicking it), so property
    // pickers are wired in the sidebar.
    var tid = card.getAttribute("data-ticket-id");
    desiredBoardSidebarTicketId = tid;
    paintBoardSelectedCard(tid);
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

  // ---- board cursor: H/L navigation between columns ----------------------
  // J/K walk the flat, column-ordered card list (so they already cross column
  // boundaries top-to-bottom). H/L jump horizontally to the adjacent column,
  // landing on the card at the same row (clamped), skipping empty columns.
  // Built on the same boardCards()/setCursor() infra as J/K so it inherits
  // the sidebar wiring and post-poll cursor restoration for free.
  function boardColumns() {
    return Array.prototype.slice.call(
      document.querySelectorAll("section[data-column]")
    );
  }

  function moveColumn(delta) {
    if (!document.getElementById("board-grid")) return false;
    var cols = boardColumns();
    if (!cols.length) return false;
    var cur = document.querySelector("li[data-ticket-id][data-nd-cursor]");
    if (!cur) {
      // No cursor yet — drop onto the first available card.
      setCursor(0);
      return true;
    }
    // Locate the current column and the cursor's row within it.
    var ci = -1, row = 0;
    for (var i = 0; i < cols.length; i++) {
      if (cols[i].contains(cur)) {
        ci = i;
        var cards = cols[i].querySelectorAll("li[data-ticket-id]");
        for (var r = 0; r < cards.length; r++) {
          if (cards[r] === cur) { row = r; break; }
        }
        break;
      }
    }
    if (ci < 0) { setCursor(0); return true; }
    // Step to the nearest non-empty column in the requested direction.
    var flat = boardCards();
    for (var target = ci + delta; target >= 0 && target < cols.length; target += delta) {
      var colCards = cols[target].querySelectorAll("li[data-ticket-id]");
      if (!colCards.length) continue;
      var pick = colCards[Math.min(row, colCards.length - 1)];
      for (var f = 0; f < flat.length; f++) {
        if (flat[f] === pick) { setCursor(f); return true; }
      }
      return true;
    }
    return true; // no column that direction — keep the cursor where it is
  }

  // Public cursor API so the bulk-selection module can reuse the cursor logic
  // (X selects the focused card; Shift+J/K extend the selection) without
  // duplicating boardCards()/setCursor(). Returns the focused ticket id.
  window.ndBoardCursor = {
    cards: boardCards,
    index: cursorIndex,
    set: setCursor,
    move: moveCursor,
    currentId: function () {
      var c = document.querySelector("li[data-ticket-id][data-nd-cursor]");
      return c ? c.getAttribute("data-ticket-id") : null;
    },
  };

  // Re-apply cursor highlight after HTMX swaps replace cards.
  function restoreCursor() {
    if (restorePendingBoardFocus()) return;
    // Prefer the in-flight desired id; sidebar attribute is stale while the
    // sidebar AJAX from setCursor is still in flight.
    var sel = desiredBoardSidebarTicketId;
    if (!sel) {
      var sidebar = document.getElementById("sidebar");
      sel = sidebar && sidebar.getAttribute("data-selected-ticket-id");
    }
    if (!sel) return;
    clearBoardCursor();
    var cards = boardCards();
    for (var i = 0; i < cards.length; i++) {
      if (cards[i].getAttribute("data-ticket-id") === sel) {
        cards[i].setAttribute("data-nd-cursor", "");
        cards[i].classList.add("nd-cursor-active");
        return;
      }
    }
  }

  // ---- inbox page detection -----------------------------------------------
  // The inbox page (/inbox) uses the same cursor pattern as the board but
  // with different shortcuts (A=promote, D=decline, Enter=open, L=labels).

  function isInboxPage() {
    return !!document.getElementById("inbox-list");
  }

  // ---- inbox cursor: J/K navigation between items -------------------------
  // Inbox items live in #inbox-list ul > li[data-ticket-id]. J/K walks the
  // list top-to-bottom. The cursor highlights the row and enables triage
  // shortcuts (A/D/E/L/P/Enter) on the focused item.

  function inboxCards() {
    var host = document.getElementById("inbox-list");
    if (!host) return [];
    return Array.prototype.slice.call(
      host.querySelectorAll("li[data-ticket-id]")
    );
  }

  function inboxCursorIndex() {
    var cards = inboxCards();
    for (var i = 0; i < cards.length; i++) {
      if (cards[i].hasAttribute("data-nd-cursor")) return i;
    }
    return -1;
  }

  function inboxSetCursor(idx) {
    var cards = inboxCards();
    if (!cards.length) return;
    if (idx < 0) idx = 0;
    if (idx >= cards.length) idx = cards.length - 1;

    // Clear old cursor highlight.
    var prev = document.querySelector("#inbox-list li[data-ticket-id][data-nd-cursor]");
    if (prev) {
      prev.removeAttribute("data-nd-cursor");
      prev.classList.remove("nd-cursor-active");
    }

    var card = cards[idx];
    card.setAttribute("data-nd-cursor", "");
    card.classList.add("nd-cursor-active");
    card.scrollIntoView({ block: "nearest" });
  }

  function inboxMoveCursor(delta) {
    if (!isInboxPage()) return false;
    var idx = inboxCursorIndex();
    if (idx < 0) {
      inboxSetCursor(0);
      return true;
    }
    inboxSetCursor(idx + delta);
    return true;
  }

  function inboxCurrentTicket() {
    var card = document.querySelector("#inbox-list li[data-ticket-id][data-nd-cursor]");
    if (!card) return null;
    var id = card.getAttribute("data-ticket-id");
    var titleEl = card.querySelector(".font-medium, .font-bold");
    var title = (titleEl ? titleEl.textContent : "").trim();
    return { id: id, title: title || id };
  }

  // Re-apply inbox cursor highlight after HTMX swaps.
  function restoreInboxCursor() {
    if (!isInboxPage()) return;
    var prev = document.querySelector("#inbox-list li[data-ticket-id][data-nd-cursor]");
    if (prev) return; // cursor survived the swap
    // No cursor — nothing to restore. The user must press J/K to start.
  }

  // ---- inbox triage actions ------------------------------------------------
  // Post promote/decline via fetch (reusing cookie-authed routes), then
  // refresh the inbox list via HTMX swap.

  // inboxPostAction(url, feedback[, body])
  // Captures the cursor index *before* POSTing so the cursor can be
  // re-landed at the same position (or the new last item) after the list
  // is refreshed. Without the pre-capture the index is always -1 after the
  // swap and serial triage (A, A, D, A…) keeps jumping back to item 0.
  // body, when provided, is sent as application/x-www-form-urlencoded.
  function inboxPostAction(url, feedback, body) {
    // Capture index before any DOM mutation.
    var savedIdx = inboxCursorIndex();
    var fetchHeaders = { "HX-Request": "true" };
    var fetchOpts = { method: "POST", credentials: "same-origin", headers: fetchHeaders };
    if (body) {
      fetchHeaders["Content-Type"] = "application/x-www-form-urlencoded";
      fetchOpts.body = body;
    }
    return fetch(url, fetchOpts).then(function (r) {
      if (r.ok || r.status === 204) {
        flashStatus(feedback);
        // Refresh the inbox list in place.
        var listEl = document.getElementById("inbox-list");
        if (listEl && typeof window.htmx !== "undefined") {
          var qs = location.search || "";
          window.htmx.ajax("GET", "/inbox/list" + qs, {
            target: "#inbox-list",
            swap: "outerHTML",
          }).then(function () {
            // Re-land the cursor at the saved index (clamped to the new
            // list length), so triage can continue without the cursor
            // jumping back to item 0 after each accept/decline.
            var cards = inboxCards();
            if (cards.length) {
              inboxSetCursor(savedIdx >= 0 ? Math.min(savedIdx, cards.length - 1) : 0);
            }
          });
        }
      } else if (r.status === 422) {
        // Incomplete ticket — can't promote.
        r.text().then(function (msg) {
          flashStatus("Can't promote: " + (msg || "incomplete"));
        });
      } else if (r.status === 409) {
        flashStatus("Action not allowed for this item");
      } else if (r.status === 404) {
        flashStatus("Item not found");
      } else {
        flashStatus("Action failed (" + r.status + ")");
      }
    }).catch(function () {
      flashStatus("Action failed");
    });
  }

  function inboxPromote(ctx, target) {
    var qs = location.search || "";
    // Send target as a form-urlencoded body so the route's form.get("target")
    // reads it. Without this body the route defaults to "draft" regardless of
    // what the caller passes, so "A" silently promoted to draft instead of queued.
    inboxPostAction(
      "/inbox/tickets/" + encodeURIComponent(ctx.id) + "/promote" + qs,
      "Promoted: " + (ctx.title || ctx.id),
      "target=" + encodeURIComponent(target)
    );
  }

  function inboxDecline(ctx) {
    var qs = location.search || "";
    inboxPostAction(
      "/inbox/tickets/" + encodeURIComponent(ctx.id) + "/decline" + qs,
      "Declined — moved to Archive: " + (ctx.title || ctx.id)
    );
  }

  function inboxOpenDetail(ctx) {
    location.href = "/tickets/" + encodeURIComponent(ctx.id);
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
  // Expose so HTMX hx-on handlers on buttons (e.g. Decline) can fire the
  // same toast without duplicating the implementation.
  window.ndFlashStatus = flashStatus;

  function runTicketAction(verb, id, title) {
    var routes = {
      "run-now": "/tickets/" + encodeURIComponent(id) + "/run-now",
      archive: "/tickets/" + encodeURIComponent(id) + "/archive",
      requeue: "/tickets/" + encodeURIComponent(id) + "/requeue",
    };
    var labels = { "run-now": "Queued", archive: "Archived", requeue: "Requeued" };
    rememberBoardFocusAfterMutation(id, { follow: verb !== "archive" });
    postAction(routes[verb]).then(function (r) {
      if (r.ok || r.status === 204) {
        flashStatus(labels[verb] + ": " + (title || id));
        if (verb === "archive") restorePendingBoardFocus();
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
  function openCreateTicket(opts) {
    opts = opts || {};
    if (openModalEl(document.getElementById("ticket-create-modal"))) return;
    var host = document.getElementById("create-modal-host");
    if (!host || typeof window.htmx === "undefined") {
      location.href = opts.project ? "/?project=" + encodeURIComponent(opts.project) + "&new=1" : "/?new=1";
      return;
    }
    var url = "/board/new-ticket-modal";
    if (opts.project) url += "?project=" + encodeURIComponent(opts.project);
    window.htmx
      .ajax("GET", url, { target: "#create-modal-host", swap: "innerHTML" })
      .then(function () {
        openModalEl(document.getElementById("ticket-create-modal"));
      });
  }
  window.ndOpenCreateTicket = openCreateTicket;

  // Inbox promote: lazy-load the promote modal (the shared edit modal in
  // "promote" mode, pre-filled for one inbox item) into #promote-modal-host
  // and open it. Falls back to the ticket detail page if HTMX is unavailable.
  function openPromoteTicket(tid, project) {
    if (!tid) return;
    var host = document.getElementById("promote-modal-host");
    if (!host || typeof window.htmx === "undefined") {
      location.href = "/tickets/" + encodeURIComponent(tid);
      return;
    }
    var url = "/inbox/tickets/" + encodeURIComponent(tid) + "/promote-modal";
    if (project) url += "?project=" + encodeURIComponent(project);
    window.htmx
      .ajax("GET", url, { target: "#promote-modal-host", swap: "innerHTML" })
      .then(function () {
        openModalEl(document.getElementById("inbox-promote-modal"));
      });
  }
  window.ndOpenPromoteTicket = openPromoteTicket;

  // After-request handler for the promote modal form. On success the inbox list
  // has already been swapped in, so just close the dialog. On a rejected
  // promotion (e.g. queueing with required fields still missing) keep the modal
  // open and surface the reason in the error banner, preserving the edits.
  window.ndPromoteAfterRequest = function (event, modalId) {
    var dlg = document.getElementById(modalId);
    var ok = !!(event && event.detail && event.detail.successful);
    if (ok) {
      if (dlg) dlg.close();
      return;
    }
    var box = dlg && dlg.querySelector("[data-promote-error]");
    if (!box) return;
    var msg = "Promotion failed.";
    var xhr = event && event.detail && event.detail.xhr;
    if (xhr && xhr.responseText) {
      try {
        var body = JSON.parse(xhr.responseText);
        if (body && body.detail) msg = "Cannot promote: " + body.detail;
      } catch (e) {
        if (xhr.responseText.length < 300) msg = xhr.responseText;
      }
    }
    box.textContent = msg;
    box.classList.remove("hidden");
  };

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
    var bulk = window.ndBulkSelect;
    if (bulk && bulk.count && bulk.count() > 0) {
      var opened = bulk.openMenu && bulk.openMenu("labels");
      if (!opened) flashStatus("Bulk labels: select tickets first");
      return;
    }
    if (!ctx) {
      flashStatus("Label picker: select a ticket first");
      return;
    }
    // Delegate to the inline label picker exposed by inline_label_picker.js.
    var ok = window.ndOpenLabelPicker && window.ndOpenLabelPicker(ctx.id);
    if (!ok) {
      flashStatus("Label picker: select a ticket first");
    }
  }

  function bulkCount() {
    var bulk = window.ndBulkSelect;
    return bulk && bulk.count ? bulk.count() : 0;
  }

  function openBulkMenu(name) {
    var bulk = window.ndBulkSelect;
    return !!(bulk && bulk.count && bulk.count() > 0 &&
      bulk.openMenu && bulk.openMenu(name));
  }

  function openPeek(ctx) {
    // Optional pre-hook: if a custom peek panel is registered, delegate to it.
    if (typeof window.ndPeekTicket === "function") {
      window.ndPeekTicket(ctx.id);
      return;
    }
    // On the board: load the sidebar for the focused ticket (same as clicking the card).
    var sidebar = document.getElementById("sidebar");
    if (sidebar && typeof window.htmx !== "undefined") {
      desiredBoardSidebarTicketId = ctx.id;
      paintBoardSelectedCard(ctx.id);
      window.htmx.ajax("GET", "/board/sidebar?ticket_id=" + encodeURIComponent(ctx.id), {
        target: "#sidebar",
        swap: "outerHTML",
      });
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
    // Default order: run → detail → edit → peek → project → priority → status →
    // labels → archive → requeue
    var order = [
      "run-now", "open-detail", "edit", "peek",
      "project", "priority", "status", "labels", "archive", "requeue",
    ];
    switch (status) {
      case "running":
        // Can't run/requeue/archive while running; show peek & edit first.
        order = ["peek", "edit", "open-detail", "project", "priority", "status", "labels"];
        break;
      case "review":
        // Requeue & archive are the primary actions from review.
        order = ["requeue", "archive", "open-detail", "edit", "peek", "project", "priority", "status", "labels", "run-now"];
        break;
      case "archived":
        // Requeue is the primary action from archived.
        order = ["requeue", "open-detail", "edit", "peek", "project", "priority", "status", "labels", "archive", "run-now"];
        break;
      case "draft":
        // Run-now is primary; inbox lets you demote back for triage.
        order = ["run-now", "inbox", "edit", "open-detail", "peek", "project", "priority", "status", "labels", "archive", "requeue"];
        break;
      case "queued":
        // Run-now is the primary action.
        order = ["run-now", "edit", "open-detail", "peek", "project", "priority", "status", "labels", "archive", "requeue"];
        break;
    }
    return order;
  }

  function baseCommands() {
    var cmds = [];
    var ctx = currentTicket();
    var status = focusedTicketStatus(ctx);
    var onInbox = isInboxPage();

    if (ctx) {
      var short = ctx.title.length > 40 ? ctx.title.slice(0, 39) + "…" : ctx.title;

      // Inbox triage commands — surface promote/decline/open/labels/priority.
      if (onInbox) {
        cmds.push({
          label: "Accept & queue: " + short, shortcut: "A", hint: "",
          section: "Inbox triage",
          run: function () { inboxPromote(ctx, "queued"); },
        });
        cmds.push({
          label: "Decline: " + short, shortcut: "D", hint: "",
          section: "Inbox triage",
          run: function () { inboxDecline(ctx); },
        });
        cmds.push({
          label: "Open detail: " + short, shortcut: "Enter", hint: "",
          section: "Inbox triage",
          run: function () { inboxOpenDetail(ctx); },
        });
        cmds.push({
          label: "Flesh out / promote…: " + short, shortcut: "E", hint: "",
          section: "Inbox triage",
          run: function () {
            var proj = (new URLSearchParams(location.search)).get("project") || "";
            window.ndOpenPromoteTicket && window.ndOpenPromoteTicket(ctx.id, proj);
          },
        });
        cmds.push({
          label: "Set project: " + short, shortcut: "P", hint: "",
          section: "Inbox triage",
          run: function () {
            var ok = window.ndOpenPropertyPicker &&
              window.ndOpenPropertyPicker(ctx.id, "project");
            if (!ok) flashStatus("No project picker found for this item");
          },
        });
        cmds.push({
          label: "Set priority: " + short, shortcut: "Shift P", hint: "",
          section: "Inbox triage",
          run: function () {
            var ok = window.ndOpenPropertyPicker &&
              window.ndOpenPropertyPicker(ctx.id, "priority");
            if (!ok) flashStatus("No priority picker found for this item");
          },
        });
        cmds.push({
          label: "Set labels: " + short, shortcut: "L", hint: "",
          section: "Inbox triage",
          run: function () { openLabelPicker(ctx); },
        });
      } else {
        // Board / ticket-detail commands.
        var ticketActions = {
          "run-now": {
            label: "Run now: " + short, shortcut: "R", hint: status || "ticket",
            section: "Ticket actions",
            run: function () { runTicketAction("run-now", ctx.id, ctx.title); },
          },
          "open-detail": {
            label: "Open detail: " + short, shortcut: "Enter", hint: "",
            section: "Ticket actions",
            run: function () { location.href = "/tickets/" + encodeURIComponent(ctx.id); },
          },
          "inbox": {
            label: "Send to Inbox: " + short, shortcut: "I", hint: "draft only",
            section: "Ticket actions",
            run: function () {
              if (status !== "draft") {
                flashStatus("Only draft tickets can be sent to Inbox");
                return;
              }
              fetch("/board/tickets/" + encodeURIComponent(ctx.id) + "/property/status", {
                method: "POST",
                credentials: "same-origin",
                headers: {
                  "Content-Type": "application/x-www-form-urlencoded",
                  "HX-Request": "true",
                },
                body: "value=inbox",
              }).then(function (r) {
                if (r.ok) {
                  flashStatus("Sent to Inbox: " + (ctx.title || ctx.id));
                } else {
                  flashStatus("Failed to send to Inbox (" + r.status + ")");
                }
              }).catch(function () {
                flashStatus("Failed to send to Inbox");
              });
            },
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
            label: "Set priority: " + short, shortcut: "Shift P", hint: "",
            section: "Properties",
            run: function () {
              var ok = window.ndOpenPropertyPicker &&
                window.ndOpenPropertyPicker(ctx.id, "priority");
              if (!ok) flashStatus("Open the ticket to change its priority");
            },
          },
          "project": {
            label: "Set project: " + short, shortcut: "P", hint: "",
            section: "Properties",
            run: function () {
              var ok = window.ndOpenPropertyPicker &&
                window.ndOpenPropertyPicker(ctx.id, "project");
              if (!ok) flashStatus("Open the ticket to change its project");
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
            label: "Set labels: " + short, shortcut: "Shift L", hint: "",
            section: "Properties",
            run: function () { openLabelPicker(ctx); },
          },
        };

        var actionOrder = statusActionOrder(status);
        actionOrder.forEach(function (key) {
          var cmd = ticketActions[key];
          if (!cmd) return;
          if (status === "running" && (key === "run-now" || key === "requeue" || key === "archive")) return;
          cmds.push(cmd);
        });
      }
    }

    var bulk = window.ndBulkSelect;
    var bulkCount = bulk && bulk.count ? bulk.count() : 0;
    if (bulkCount > 0) {
      [
        ["priority", "Bulk: set priority"],
        ["status", "Bulk: set status"],
        ["project", "Bulk: set project"],
        ["labels", "Bulk: add label"],
      ].forEach(function (entry) {
        cmds.push({
          label: entry[1] + " (" + bulkCount + " selected)",
          shortcut: "Ctrl/Cmd K",
          hint: "selected tickets",
          section: "Bulk actions",
          run: function () {
            var ok = bulk.openMenu && bulk.openMenu(entry[0]);
            if (!ok) flashStatus("Select tickets first");
          },
        });
      });
    }

    // --- View / display commands ---
    // Toggle board/list view, focus search, future saved-views hook.
    var isBoard = !!document.getElementById("board-grid");
    var activeViewBtn = document.querySelector('[data-sb-view][aria-pressed="true"]');
    var currentBoardView = activeViewBtn ? activeViewBtn.getAttribute("data-sb-view") : "tickets";
    var isRuns = currentBoardView === "runs";
    var isListPage = location.pathname === "/list";
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
      label: isListPage ? "Switch to board view" : "Switch to list view",
      shortcut: "Ctrl/Cmd B", hint: "toggle",
      section: "View",
      run: function () {
        var sp2 = new URLSearchParams(location.search);
        var tp = [];
        ["q", "group", "order", "props"].forEach(function (k) {
          var v = sp2.get(k) || "";
          if (v) tp.push(k + "=" + encodeURIComponent(v));
        });
        var tqs = tp.length ? "?" + tp.join("&") : "";
        location.href = isListPage ? ("/" + tqs) : ("/list" + tqs);
      },
    });
    if (document.getElementById("nd-display-btn")) {
      cmds.push({
        label: "Display options",
        shortcut: "Shift V",
        hint: "group · order · properties",
        section: "View",
        run: function () {
          if (window.ndDisplayPopover) window.ndDisplayPopover.open();
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
    // Saved views — ndSavedViews() returns a [{name,surface,url}] array.
    // "Jump to saved view…" always appears so g v stays discoverable.
    if (typeof window.ndSavedViews === "function") {
      var savedViews = window.ndSavedViews();
      if (savedViews && savedViews.length) {
        cmds.push({
          label: "Jump to saved view…", shortcut: "g v", hint: "",
          section: "View",
          run: function () { openPaletteFiltered("View:"); },
        });
        savedViews.forEach(function (sv) {
          cmds.push({
            label: "View: " + sv.name,
            shortcut: "",
            hint: sv.surface,
            section: "Saved views",
            run: (function (url) { return function () { location.href = url; }; })(sv.url),
          });
        });
      } else {
        cmds.push({
          label: "Jump to saved view…", shortcut: "g v", hint: "no views saved",
          section: "View",
          run: function () { openPaletteFiltered("View:"); },
        });
      }
    }

    // --- Navigation & general commands ---
    cmds.push({ label: "New ticket", shortcut: "c", hint: "", section: "Navigation",
      run: openCreateTicket });
    cmds.push({ label: "Go to Work", shortcut: "", hint: "Board", section: "Navigation",
      run: function () { location.href = "/"; } });
    cmds.push({ label: "Go to Insights", shortcut: "g i", hint: "Analytics", section: "Navigation",
      run: function () { location.href = "/analytics"; } });
    cmds.push({ label: "Go to Setup", shortcut: "g s", hint: "Settings", section: "Navigation",
      run: function () { location.href = "/settings"; } });
    cmds.push({ label: "Go to Projects", shortcut: "", hint: "Setup", section: "Navigation",
      run: function () { location.href = "/projects"; } });
    cmds.push({ label: "Go to Toolsets", shortcut: "", hint: "Setup", section: "Navigation",
      run: function () { location.href = "/toolsets"; } });
    cmds.push({ label: "Show keyboard shortcuts", shortcut: "?", hint: "", section: "Navigation",
      run: openCheatSheet });
    cmds.push({ label: "Go to Work: board", shortcut: "g b", hint: "view", section: "Work views",
      run: function () { location.href = "/"; } });
    cmds.push({ label: "Go to Work: list", shortcut: "g l", hint: "view", section: "Work views",
      run: function () { location.href = "/list"; } });
    cmds.push({ label: "Go to Work: inbox", shortcut: "", hint: "view", section: "Work views",
      run: function () { location.href = "/inbox"; } });
    cmds.push({ label: "Go to Work: archive", shortcut: "g a", hint: "view", section: "Work views",
      run: function () { location.href = "/archive"; } });
    cmds.push({ label: "Go to Work: scheduled", shortcut: "", hint: "Cron", section: "Work views",
      run: function () { location.href = "/cron"; } });

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

  function openPaletteFiltered(filter) {
    // Open the palette with a pre-set filter string so the user lands directly
    // on a filtered command set (e.g. "View:" to browse saved views).
    var dlg = palette();
    if (!dlg) return;
    if (cheatsheet() && cheatsheet().open) cheatsheet().close();
    if (!dlg.open) {
      try { dlg.showModal(); } catch (e) { return; }
    }
    var inp = input();
    inp.value = filter || "";
    state.active = 0;
    refresh();
    inp.focus();
    var len = inp.value.length;
    inp.setSelectionRange(len, len);
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
      else if (!foreignDialogOpen()) openPalette();
      return;
    }

    // Ctrl/Cmd+B: toggle between board (/) and list (/list), carrying the
    // query and the display params (?group=, ?order=, ?props=).
    if ((e.metaKey || e.ctrlKey) && (key === "b" || key === "B")) {
      e.preventDefault();
      var sp = new URLSearchParams(location.search);
      var parts = [];
      ["q", "group", "order", "props"].forEach(function (k) {
        var v = sp.get(k) || "";
        if (v) parts.push(k + "=" + encodeURIComponent(v));
      });
      var bqs = parts.length ? "?" + parts.join("&") : "";
      location.href = (location.pathname === "/list") ? ("/" + bqs) : ("/list" + bqs);
      return;
    }

    // Everything below is a single-key shortcut: ignore modifier combos,
    // typing contexts, and foreign open dialogs.
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (isTypingTarget(e.target)) return;
    if (foreignDialogOpen()) return;

    var now = Date.now();
    var hadG = now - lastG < 800;
    lastG = 0;

    // If our own palette/cheatsheet is open, let their own handlers / native
    // dialog behavior deal with keys.
    if ((palette() && palette().open) || (cheatsheet() && cheatsheet().open)) return;

    // If a property picker menu is open, don't hijack keys — the picker's
    // own handler manages Arrow/Enter/Escape.
    if (document.querySelector("[data-property-menu]:not(.hidden)")) return;
    if (document.querySelector("[data-nd-bulk-menu]:not([hidden])")) return;
    if (document.querySelector("[data-inline-label-menu]:not(.hidden)")) return;

    // Shift+V: toggle the Display options popover (board + list toolbars).
    if (e.shiftKey && (key === "v" || key === "V")) {
      if (window.ndDisplayPopover && document.getElementById("nd-display-btn")) {
        e.preventDefault();
        window.ndDisplayPopover.toggle();
      }
      return;
    }

    // Esc priority: display popover close → selection clear → board cursor
    // clear. (Bulk-menu close is handled in bulk_select.js regardless of
    // focus; palette/cheatsheet/property-picker close is handled by their own
    // guards above or native <dialog> behavior.)
    if (key === "Escape" || key === "Esc") {
      if (window.ndDisplayPopover && window.ndDisplayPopover.isOpen()) {
        e.preventDefault();
        window.ndDisplayPopover.close({ refocus: true });
        return;
      }
      if (window.ndBulkSelect && window.ndBulkSelect.count() > 0) {
        e.preventDefault();
        window.ndBulkSelect.clear();
        return;
      }
      if (document.getElementById("board-grid")) {
        var hasCursor = document.querySelector("li[data-ticket-id][data-nd-cursor]");
        if (hasCursor) {
          e.preventDefault();
          clearBoardCursor();
          paintBoardSelectedCard(null);
          return;
        }
      }
      return;
    }

    if (window.ndDisplayPopover && window.ndDisplayPopover.isOpen()) return;

    if (hadG && (key === "i" || key === "I")) {
      e.preventDefault(); lastG = 0; location.href = "/analytics"; return;
    }
    if (hadG && (key === "s" || key === "S")) {
      e.preventDefault(); lastG = 0; location.href = "/settings"; return;
    }
    if (hadG && (key === "b" || key === "B")) {
      e.preventDefault(); lastG = 0; location.href = "/"; return;
    }
    if (hadG && (key === "l" || key === "L")) {
      e.preventDefault(); lastG = 0; location.href = "/list"; return;
    }
    if (hadG && (key === "a" || key === "A")) {
      e.preventDefault(); lastG = 0; location.href = "/archive"; return;
    }
    if (hadG && (key === "v" || key === "V")) {
      // g v: open the command palette pre-filtered to saved views.
      e.preventDefault(); lastG = 0; openPaletteFiltered("View:"); return;
    }

    if (key === "g" || key === "G") { lastG = now; return; }
    if (key === "c" || key === "C") { e.preventDefault(); openCreateTicket(); return; }
    if (key === "/") {
      e.preventDefault();
      var search = document.querySelector("[data-sb-input]") ||
        document.querySelector('#header-search input[name="q"]');
      if (search) { search.focus(); search.select(); }
      return;
    }
    if (key === "?") { e.preventDefault(); openCheatSheet(); return; }

    // --- Board cursor: J/K navigate cards (vim-style down/up) ------------
    // Shift+J / Shift+K extend the bulk selection while moving the cursor.
    if (key === "j" || key === "J") {
      if (isInboxPage()) {
        e.preventDefault();
        inboxMoveCursor(1);
      } else if (document.getElementById("board-grid")) {
        e.preventDefault();
        if (e.shiftKey && window.ndBulkSelect) window.ndBulkSelect.extend(1);
        else moveCursor(1);
      }
      return;
    }
    if (key === "k" || key === "K") {
      if (isInboxPage()) {
        e.preventDefault();
        inboxMoveCursor(-1);
      } else if (document.getElementById("board-grid")) {
        e.preventDefault();
        if (e.shiftKey && window.ndBulkSelect) window.ndBulkSelect.extend(-1);
        else moveCursor(-1);
      }
      return;
    }
    // --- X: toggle bulk selection of the focused card --------------------
    if ((key === "x" || key === "X") && !e.shiftKey) {
      if (document.getElementById("board-grid") && window.ndBulkSelect) {
        e.preventDefault();
        window.ndBulkSelect.toggleFocused();
      }
      return;
    }

    // --- Inbox triage shortcuts -------------------------------------------
    // On the inbox page, keys are repurposed for triage flow:
    //   Enter = open detail, A = promote, D = decline, E = edit,
    //   L = labels (plain L — no column nav on inbox), P = project,
    //   Shift+P = priority,
    //   S = unbound (snooze not yet implemented).
    if (isInboxPage()) {
      var ictx = inboxCurrentTicket();

      // Enter — open focused inbox item
      if (key === "Enter") {
        e.preventDefault();
        if (ictx) inboxOpenDetail(ictx);
        return;
      }
      // A — accept / promote (queued if complete, shows blocker feedback if not)
      if (key === "a" || key === "A") {
        if (ictx) { e.preventDefault(); inboxPromote(ictx, "queued"); }
        return;
      }
      // D — decline / archive
      if (key === "d" || key === "D") {
        if (ictx) { e.preventDefault(); inboxDecline(ictx); }
        return;
      }
      // E — flesh out / promote (opens the promote modal in-place)
      if (key === "e" || key === "E") {
        if (ictx) {
          e.preventDefault();
          var proj = (new URLSearchParams(location.search)).get("project") || "";
          window.ndOpenPromoteTicket && window.ndOpenPromoteTicket(ictx.id, proj);
        }
        return;
      }
      // L — labels (plain L on inbox; no column navigation here)
      if ((key === "l" || key === "L") && !e.shiftKey) {
        if (ictx) { e.preventDefault(); openLabelPicker(ictx); }
        return;
      }
      // P — project picker, Shift+P — priority picker
      if (key === "p" || key === "P") {
        if (ictx) {
          e.preventDefault();
          var prop = e.shiftKey ? "priority" : "project";
          var pok = window.ndOpenPropertyPicker &&
            window.ndOpenPropertyPicker(ictx.id, prop);
          if (!pok) flashStatus("No " + prop + " picker found for this item");
        }
        return;
      }
      // S — intentionally unbound on inbox (snooze does not exist yet).
      // Falls through so it doesn't fire any action.
      if (key === "s" || key === "S") {
        return;
      }

      // Anything else on the inbox page: stop here — don't leak board
      // shortcuts (R, Space, H/L column nav, Shift+L) into the inbox.
      return;
    }

    // --- Enter: open the detail page for the cursor-focused board ticket ---
    if (key === "Enter") {
      if (document.getElementById("board-grid")) {
        var enterCtx = currentTicket();
        if (enterCtx) {
          e.preventDefault();
          location.href = "/tickets/" + encodeURIComponent(enterCtx.id);
        }
      }
      return;
    }

    // --- Board cursor: H/L jump between columns (vim-style left/right) ----
    // Only intercept on the board; off-board (e.g. ticket detail) these keys
    // fall through so Shift+L can still open the label picker there.
    if (key === "h" || key === "H") {
      if (document.getElementById("board-grid")) {
        e.preventDefault();
        moveColumn(-1);
        return;
      }
    }
    if (key.toLowerCase() === "l" && !e.shiftKey) {
      if (document.getElementById("board-grid")) {
        e.preventDefault();
        moveColumn(1);
        return;
      }
    }

    // --- Focused-ticket property actions ----------------------------------
    // These require a selected/focused ticket context.
    var ctx = currentTicket();

    // E — edit the focused ticket
    if (key === "e" || key === "E") {
      if (ctx) { e.preventDefault(); openEditForTicket(ctx); }
      return;
    }
    // P — project picker, Shift+P — priority picker
    if (key === "p" || key === "P") {
      if (bulkCount() > 0) {
        e.preventDefault();
        openBulkMenu(e.shiftKey ? "priority" : "project");
        return;
      }
      if (ctx) {
        e.preventDefault();
        var prop = e.shiftKey ? "priority" : "project";
        var ok = window.ndOpenPropertyPicker &&
          window.ndOpenPropertyPicker(ctx.id, prop);
        if (!ok) flashStatus("Open the ticket to change its " + prop);
      }
      return;
    }
    // S — status picker
    if (key === "s" || key === "S") {
      if (bulkCount() > 0) {
        e.preventDefault();
        openBulkMenu("status");
        return;
      }
      if (ctx) {
        e.preventDefault();
        var ok2 = window.ndOpenPropertyPicker &&
          window.ndOpenPropertyPicker(ctx.id, "status");
        if (!ok2) flashStatus("Open the ticket to change its status");
      }
      return;
    }
    // Shift+L — bulk labels when a selection exists, otherwise focused-ticket
    // label picker. Plain "l" is board column-navigation (handled above), so
    // labels live on Shift+L to keep the H/J/K/L cursor spine whole.
    if (key.toLowerCase() === "l" && e.shiftKey) {
      e.preventDefault();
      openLabelPicker(ctx);
      return;
    }
    // A — archive the focused ticket
    if (key === "a" || key === "A") {
      if (bulkCount() > 0 && window.ndBulkSelect && window.ndBulkSelect.archive) {
        e.preventDefault();
        window.ndBulkSelect.archive();
        return;
      }
      if (ctx) { e.preventDefault(); runTicketAction("archive", ctx.id, ctx.title); }
      return;
    }
    // R — run/requeue the focused ticket
    if (key === "r" || key === "R") {
      if (ctx) { e.preventDefault(); smartRunOrRequeue(ctx); }
      return;
    }
    // Space — peek (opens the board sidebar for the focused ticket)
    if (key === " ") {
      if (document.getElementById("board-grid")) {
        e.preventDefault();
        if (ctx) openPeek(ctx);
      }
      return;
    }
    // I — send the cursor-focused draft ticket to Inbox
    if (key === "i" || key === "I") {
      if (document.getElementById("board-grid")) {
        var ictx3 = currentTicket();
        if (ictx3) {
          var iStatus = focusedTicketStatus(ictx3);
          if (iStatus !== "draft") {
            e.preventDefault();
            flashStatus(iStatus
              ? "Only draft tickets can be sent to Inbox (this is " + iStatus + ")"
              : "Only draft tickets can be sent to Inbox");
            return;
          }
          e.preventDefault();
          rememberBoardFocusAfterMutation(ictx3.id, { follow: false });
          fetch("/board/tickets/" + encodeURIComponent(ictx3.id) + "/property/status", {
            method: "POST",
            credentials: "same-origin",
            headers: {
              "Content-Type": "application/x-www-form-urlencoded",
              "HX-Request": "true",
            },
            body: "value=inbox",
          }).then(function (r) {
            if (r.ok) {
              flashStatus("Sent to Inbox: " + (ictx3.title || ictx3.id));
              var poll = document.getElementById("board-columns-poll");
              if (poll && typeof window.htmx !== "undefined") {
                try { window.htmx.trigger(poll, "poll"); } catch (ex) {}
              }
            } else if (r.status === 409) {
              flashStatus("Can't send to Inbox: transition not allowed");
            } else {
              flashStatus("Failed to send to Inbox (" + r.status + ")");
            }
          }).catch(function () {
            flashStatus("Failed to send to Inbox");
          });
        }
      }
      return;
    }
  }

  // ---- header-search arrow-key helper ------------------------------------

  function setSearchActive(anchors, idx) {
    anchors.forEach(function (a) {
      a.classList.remove("nd-opt-active", "bg-bg-elev-2");
    });
    if (idx >= 0 && idx < anchors.length) {
      anchors[idx].classList.add("nd-opt-active", "bg-bg-elev-2");
      anchors[idx].scrollIntoView({ block: "nearest" });
    }
  }

  // ---- init --------------------------------------------------------------

  function init() {
    wirePaletteInput();
    document.addEventListener("keydown", onKeydown, true);

    // Esc blurs the header search box; ArrowDown/Up move the result highlight;
    // Enter follows the active result. Without this the global onKeydown
    // early-returns inside typing targets, so Esc would never unfocus it.
    var headerSearch = document.querySelector('#header-search input[name="q"]');
    if (headerSearch && !headerSearch.__ndEscWired) {
      headerSearch.__ndEscWired = true;
      headerSearch.addEventListener("keydown", function (e) {
        if (e.key === "Escape") {
          e.preventDefault();
          e.stopPropagation();
          headerSearch.blur();
          return;
        }
        var anchors = Array.prototype.slice.call(
          document.querySelectorAll('#search-results a')
        );
        if (!anchors.length) return;
        var activeEl = document.querySelector('#search-results a.nd-opt-active');
        var idx = activeEl ? anchors.indexOf(activeEl) : -1;
        if (e.key === "ArrowDown") {
          e.preventDefault();
          setSearchActive(anchors, (idx + 1) % anchors.length);
          return;
        }
        if (e.key === "ArrowUp") {
          e.preventDefault();
          setSearchActive(anchors, (idx - 1 + anchors.length) % anchors.length);
          return;
        }
        if (e.key === "Enter" && activeEl) {
          e.preventDefault();
          location.href = activeEl.href;
          return;
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

    // Restore cursor highlight after HTMX column swaps on the board or inbox.
    // Also reset the header-search highlight when results are refreshed.
    document.body.addEventListener("htmx:afterSwap", function (ev) {
      if (ev.detail && ev.detail.target && ev.detail.target.id === "search-results") {
        document.querySelectorAll('#search-results a.nd-opt-active').forEach(function (a) {
          a.classList.remove("nd-opt-active", "bg-bg-elev-2");
        });
      }
      restoreCursor();
      restoreInboxCursor();
    });
    document.body.addEventListener("htmx:oobAfterSwap", function () {
      restoreCursor();
      restoreInboxCursor();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
