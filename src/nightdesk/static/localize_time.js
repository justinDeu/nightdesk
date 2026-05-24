// Client-side timestamp localization. Server templates emit UTC ISO strings
// in data-* attributes; this renders them in the viewer's own locale and
// timezone (nightdesk is local-first, so "local" is what the user wants).
//
//   <span data-ts="2026-05-19T14:30:00+00:00">2026-05-19 ...</span>
//     -> textContent replaced with a localized absolute date/time.
//   <span data-ts-title="..."> ... </span>
//     -> title (hover tooltip) set to the localized absolute time, inner text
//        left alone (used for relative times like "5m ago").
//
// Idempotent: processed nodes are marked so repeated calls (page load, HTMX
// swaps, live updates) don't reformat or clobber already-localized text.
(function () {
  var ABS = new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium', timeStyle: 'short',
  });

  function fmt(iso) {
    if (!iso) return null;
    var d = new Date(iso);
    if (isNaN(d.getTime())) return null;
    return ABS.format(d);
  }

  var HHMM = new Intl.DateTimeFormat(undefined, {
    hour: 'numeric', minute: '2-digit', hour12: true,
  });

  // Convert a UTC "HH:MM" wall-clock string to a friendly local 12-hour label
  // (e.g. "8:00 PM"). getTimezoneOffset() returns (UTC - local) in minutes, so
  // local = utc - off.
  function hhmmUtcToLocal(hhmm) {
    var m = /^([01]\d|2[0-3]):([0-5]\d)$/.exec(hhmm || '');
    if (!m) return null;
    var off = new Date().getTimezoneOffset();
    var total = (parseInt(m[1], 10) * 60 + parseInt(m[2], 10) - off);
    total = ((total % 1440) + 1440) % 1440;
    var d = new Date();
    d.setHours(Math.floor(total / 60), total % 60, 0, 0);
    return HHMM.format(d);
  }

  function localize(root) {
    var scope = root || document;

    scope.querySelectorAll('[data-hhmm-utc]:not([data-hhmm-done])').forEach(function (el) {
      var out = hhmmUtcToLocal(el.getAttribute('data-hhmm-utc'));
      el.setAttribute('data-hhmm-done', '1');
      if (out !== null) el.textContent = out;
    });

    scope.querySelectorAll('[data-ts]:not([data-ts-done])').forEach(function (el) {
      var out = fmt(el.getAttribute('data-ts'));
      el.setAttribute('data-ts-done', '1');
      if (out !== null) {
        el.textContent = out;
        el.setAttribute('title', out);
      }
    });

    scope.querySelectorAll('[data-ts-title]:not([data-ts-title-done])').forEach(function (el) {
      var out = fmt(el.getAttribute('data-ts-title'));
      el.setAttribute('data-ts-title-done', '1');
      if (out !== null) el.setAttribute('title', out);
    });
  }

  window.ndLocalizeTimes = localize;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { localize(document); });
  } else {
    localize(document);
  }

  // Re-localize content swapped in by HTMX (run-history panel, board updates,
  // archive rows, sidebar) once it lands in the DOM.
  document.body.addEventListener('htmx:afterSwap', function (e) {
    localize(e.target || document);
  });
})();
