/* Browser notification support for nightdesk.
 *
 * Fires a desktop notification once when a ticket leaves the "running"
 * column (i.e. its run finished). The board polls every few seconds and
 * swaps each column in place via hx-swap-oob, so this script must be
 * resilient to:
 *   - many swap events per poll cycle (afterSwap + one oobAfterSwap per
 *     column) — we coalesce them into a single settled check;
 *   - transient/partial DOM where the running column isn't present — we
 *     bail rather than treat "absent" as "everything completed";
 *   - re-notifying an already-acknowledged completion — a persisted
 *     "notified" set guarantees one notification per completion.
 */
(function () {
  if (typeof window === "undefined") return;

  var RUNNING_KEY = "nd_notify_running"; // {id: title} last seen running
  var DONE_KEY = "nd_notify_done";       // {id: true} already notified

  // -- permission ---------------------------------------------------------
  function ensurePermission(cb) {
    if (!("Notification" in window)) return;
    if (Notification.permission === "granted") { cb(); return; }
    if (Notification.permission === "denied") return;
    Notification.requestPermission().then(function (p) {
      if (p === "granted") cb();
    });
  }

  function showNotification(id, title) {
    ensurePermission(function () {
      try {
        // Stable per-ticket tag: a repeat for the same ticket replaces the
        // existing notification instead of stacking a new one.
        new Notification("Run complete: " + title, {
          body: title + " finished. Check the Review column.",
          tag: "nd-run-" + id,
        });
      } catch (e) { /* ignore */ }
    });
  }

  // -- storage ------------------------------------------------------------
  function readObj(key) {
    try { return JSON.parse(sessionStorage.getItem(key) || "{}"); }
    catch (e) { return {}; }
  }
  function writeObj(key, obj) {
    try { sessionStorage.setItem(key, JSON.stringify(obj)); }
    catch (e) { /* ignore */ }
  }

  // -- DOM snapshots ------------------------------------------------------
  function runningColumn() {
    return document.querySelector('ul.board-list[data-status="running"]');
  }

  function cardTitle(li) {
    var el = li.querySelector('[data-field="title"], .font-medium');
    return el ? el.textContent.trim() : "A ticket";
  }

  // {id: title} for every card currently in the running column.
  function snapshotRunning() {
    var map = {};
    var col = runningColumn();
    if (!col) return map;
    col.querySelectorAll('li[data-ticket-id]').forEach(function (li) {
      var id = li.dataset.ticketId;
      if (id) map[id] = cardTitle(li);
    });
    return map;
  }

  // {id: title} for cards currently in the review column.
  function snapshotReview() {
    var map = {};
    document.querySelectorAll('ul.board-list[data-status="review"] li[data-ticket-id]').forEach(function (li) {
      var id = li.dataset.ticketId;
      if (id) map[id] = cardTitle(li);
    });
    return map;
  }

  // -- diff & fire --------------------------------------------------------
  function checkNotifications() {
    // Only reason about completions when the running column is actually in
    // the DOM. On a non-board page (no board), or a partial swap that hasn't
    // rendered it, bail — "absent" must not be read as "everything finished".
    if (!runningColumn()) return;

    var prevRunning = readObj(RUNNING_KEY);
    var nowRunning = snapshotRunning();
    var review = snapshotReview();
    var done = readObj(DONE_KEY);

    // A ticket that was running before and isn't now → completed. Notify once.
    Object.keys(prevRunning).forEach(function (id) {
      if (!nowRunning[id] && !done[id]) {
        var title = review[id] || prevRunning[id] || "A ticket";
        showNotification(id, title);
        done[id] = true;
      }
    });

    // If a ticket is running again, clear its "notified" flag so a fresh
    // completion later notifies again. (Reads settled DOM thanks to the
    // coalescing scheduler, so this won't trip on transient glitches.)
    Object.keys(done).forEach(function (id) {
      if (nowRunning[id]) delete done[id];
    });

    writeObj(RUNNING_KEY, nowRunning);
    writeObj(DONE_KEY, done);
  }

  // Coalesce the burst of swap events in a single poll cycle into one check
  // against the settled DOM.
  var scheduled = null;
  function schedule() {
    if (scheduled) clearTimeout(scheduled);
    scheduled = setTimeout(function () {
      scheduled = null;
      checkNotifications();
    }, 80);
  }

  // -- init ---------------------------------------------------------------
  // Seed the running snapshot so tickets already running at load aren't
  // treated as freshly completed on the first swap.
  writeObj(RUNNING_KEY, snapshotRunning());

  document.body.addEventListener("htmx:afterSwap", schedule);
  document.body.addEventListener("htmx:oobAfterSwap", schedule);

  // Manual permission hook for a settings/UI button.
  window.ndRequestNotificationPermission = function (cb) {
    ensurePermission(cb || function () {});
  };

  window.ndNotificationStatus = function () {
    if (!("Notification" in window)) return "unsupported";
    return Notification.permission;
  };
})();
