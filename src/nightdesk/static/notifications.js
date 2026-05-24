/* Browser notification support for nightdesk.
 *
 * Tracks which ticket IDs are in the "running" column across OOB polls.
 * When a ticket was last seen in "running" and appears in "review" on the
 * next poll (or disappears from running), fires a desktop notification.
 */
(function () {
  if (typeof window === "undefined") return;

  var KEY = "nd_notify_seen";

  // -- permission request -------------------------------------------------
  function ensurePermission(cb) {
    if (!("Notification" in window)) return;
    if (Notification.permission === "granted") { cb(); return; }
    if (Notification.permission === "denied") return;
    Notification.requestPermission().then(function (p) {
      if (p === "granted") cb();
    });
  }

  function showNotification(title, body) {
    ensurePermission(function () {
      try {
        new Notification(title, { body: body, tag: "nd-run-" + Date.now() });
      } catch (e) { /* ignore */ }
    });
  }

  // -- snapshot tracking --------------------------------------------------
  function readSeen() {
    try { return JSON.parse(sessionStorage.getItem(KEY) || "{}"); }
    catch (e) { return {}; }
  }

  function writeSeen(obj) {
    try { sessionStorage.setItem(KEY, JSON.stringify(obj)); }
    catch (e) { /* ignore */ }
  }

  function snapshotRunning() {
    var seen = {};
    document.querySelectorAll('ul.board-list[data-status="running"] li[data-ticket-id]').forEach(function (li) {
      var id = li.dataset.ticketId;
      if (id) seen[id] = true;
    });
    return seen;
  }

  function snapshotReview() {
    var map = {};
    document.querySelectorAll('ul.board-list[data-status="review"] li[data-ticket-id]').forEach(function (li) {
      var id = li.dataset.ticketId;
      if (id) {
        var titleEl = li.querySelector('[data-field="title"]');
        map[id] = titleEl ? titleEl.textContent.trim() : "Ticket";
      }
    });
    return map;
  }

  // -- diff & fire --------------------------------------------------------
  function checkNotifications() {
    var prev = readSeen();
    var nowRunning = snapshotRunning();
    var review = snapshotReview();

    // Tickets that were running before but are no longer running now.
    Object.keys(prev).forEach(function (id) {
      if (!nowRunning[id]) {
        var title = review[id] || "Ticket";
        showNotification("Run complete: " + title, "Finished processing. Check the Review column.");
      }
    });

    writeSeen(nowRunning);
  }

  // -- init ---------------------------------------------------------------
  // Seed the snapshot on page load.
  writeSeen(snapshotRunning());

  // Re-check after every HTMX column swap.
  document.body.addEventListener("htmx:afterSwap", checkNotifications);
  document.body.addEventListener("htmx:oobAfterSwap", checkNotifications);

  // Expose a manual permission-request hook so a UI button can trigger it.
  window.ndRequestNotificationPermission = function (cb) {
    ensurePermission(cb || function () {});
  };

  // Expose whether browser notifications are supported and permitted.
  window.ndNotificationStatus = function () {
    if (!("Notification" in window)) return "unsupported";
    return Notification.permission; // "granted", "denied", "default"
  };
})();
