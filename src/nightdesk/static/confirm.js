// Styled confirm modal.
//
// Two entry points:
//   1. htmx:confirm — fires for every htmx request; we only handle the ones
//      that actually carry an `hx-confirm` question. The previous version
//      prompted on every request because it ignored detail.question being
//      null, which is why this file was disabled in base.html.
//   2. window.ndConfirm({title, message, okLabel, cancelLabel, danger})
//      → Promise<boolean>. Used by the sidebar dirty-state guard and any
//      other code that needs a styled confirm without going through htmx.
(function () {
  var style = document.createElement("style");
  style.textContent = [
    // Native <dialog> renders in the browser's top layer, which sits above
    // any z-index. That's the only stacking primitive that reliably appears
    // above an already-open showModal() dialog (e.g. the ticket edit modal).
    "#nd-confirm-overlay {",
    "  position: fixed; inset: 0; margin: 0; padding: 0; border: 0;",
    "  background: transparent;",
    "  width: 100vw; max-width: 100vw; height: 100vh; max-height: 100vh;",
    "  display: flex; align-items: center; justify-content: center;",
    "  overflow: hidden;",
    "}",
    "#nd-confirm-overlay:not([open]) { display: none; }",
    "#nd-confirm-overlay::backdrop { background: rgba(0,0,0,.55); }",
    "",
    ".nd-confirm-dialog {",
    "  position: relative;",
    "  background: var(--color-bg-elev);",
    "  border: 1px solid var(--color-border);",
    "  border-radius: .5rem;",
    "  padding: 1.25rem 1.5rem;",
    "  min-width: 320px; max-width: 420px;",
    "  box-shadow: 0 10px 25px rgba(0,0,0,.4);",
    "}",
    "",
    ".nd-confirm-title {",
    "  color: var(--color-fg);",
    "  font-size: .875rem; font-weight: 600;",
    "  margin: 0 0 .5rem;",
    "}",
    "",
    ".nd-confirm-message {",
    "  color: var(--color-fg-muted);",
    "  font-size: .8125rem; line-height: 1.5;",
    "  margin: 0 0 1rem;",
    "}",
    "",
    ".nd-confirm-actions {",
    "  display: flex; justify-content: flex-end; gap: .5rem;",
    "}",
    "",
    ".nd-confirm-cancel, .nd-confirm-ok {",
    "  font-size: .8125rem; font-weight: 500;",
    "  padding: .375rem .875rem; border-radius: .25rem;",
    "  cursor: pointer; border: 1px solid var(--color-border);",
    "  transition: background .12s, color .12s, border-color .12s;",
    "}",
    "",
    ".nd-confirm-cancel {",
    "  background: var(--color-bg-elev-2);",
    "  color: var(--color-fg-muted);",
    "}",
    ".nd-confirm-cancel:hover {",
    "  background: var(--color-bg);",
    "  color: var(--color-fg);",
    "}",
    "",
    ".nd-confirm-ok {",
    "  background: var(--color-bg-elev-2);",
    "  color: var(--color-fg);",
    "}",
    ".nd-confirm-ok:hover {",
    "  background: var(--color-accent);",
    "  color: var(--color-bg);",
    "  border-color: var(--color-accent);",
    "}",
    "",
    ".nd-confirm-ok.is-danger {",
    "  background: var(--color-danger);",
    "  color: var(--color-bg);",
    "  border-color: var(--color-danger);",
    "}",
    ".nd-confirm-ok.is-danger:hover {",
    "  background: #ef4444;",
    "  border-color: #ef4444;",
    "}",
  ].join("\n");
  document.head.appendChild(style);

  var overlay = document.createElement("dialog");
  overlay.id = "nd-confirm-overlay";
  overlay.innerHTML =
    '<div class="nd-confirm-dialog" role="document">' +
    '  <h3 class="nd-confirm-title"></h3>' +
    '  <p class="nd-confirm-message"></p>' +
    '  <div class="nd-confirm-actions">' +
    '    <button type="button" class="nd-confirm-cancel"></button>' +
    '    <button type="button" class="nd-confirm-ok"></button>' +
    "  </div>" +
    "</div>";
  document.body.appendChild(overlay);

  var titleEl = overlay.querySelector(".nd-confirm-title");
  var msgEl = overlay.querySelector(".nd-confirm-message");
  var cancelBtn = overlay.querySelector(".nd-confirm-cancel");
  var okBtn = overlay.querySelector(".nd-confirm-ok");

  // Only one prompt at a time. Resolver fires with true/false.
  var pendingResolve = null;

  function open(opts) {
    titleEl.textContent = opts.title || "Confirm";
    msgEl.textContent = opts.message || "Are you sure?";
    okBtn.textContent = opts.okLabel || "OK";
    cancelBtn.textContent = opts.cancelLabel || "Cancel";
    okBtn.classList.toggle("is-danger", !!opts.danger);
    // showModal() puts the dialog in the top layer so it stacks above any
    // other open <dialog> (the ticket edit modal, project create modal, etc.).
    if (typeof overlay.showModal === "function") overlay.showModal();
    else overlay.setAttribute("open", "");
    okBtn.focus();
    return new Promise(function (resolve) {
      pendingResolve = resolve;
    });
  }

  function close(result) {
    if (overlay.open && typeof overlay.close === "function") overlay.close();
    else overlay.removeAttribute("open");
    var resolve = pendingResolve;
    pendingResolve = null;
    if (resolve) resolve(result);
  }

  cancelBtn.addEventListener("click", function () { close(false); });
  okBtn.addEventListener("click", function () { close(true); });
  // Backdrop click closes the dialog. The <dialog>'s ::backdrop is part of
  // the dialog's box, so a click whose target IS the dialog (not a child)
  // landed on the backdrop area outside .nd-confirm-dialog.
  overlay.addEventListener("click", function (e) {
    if (e.target === overlay) close(false);
  });
  // Native <dialog> dispatches "cancel" on Esc; suppress its default close
  // (we want our close() to also resolve the promise) then run our handler.
  overlay.addEventListener("cancel", function (e) {
    e.preventDefault();
    close(false);
  });

  window.ndConfirm = function (opts) {
    // If another prompt is open, resolve it false first to avoid leaks.
    if (pendingResolve) close(false);
    return open(opts || {});
  };

  // htmx:confirm fires on every htmx-initiated request. Only intercept when
  // a question is present (i.e. the element has hx-confirm); ignore the
  // rest so other requests proceed normally. Without this filter, the modal
  // popped on every column poll / search keystroke / nav action.
  document.body.addEventListener("htmx:confirm", function (e) {
    if (!e.detail || !e.detail.question) return;
    e.preventDefault();
    var issue = e.detail.issueRequest;
    open({
      title: "Confirm",
      message: e.detail.question,
      okLabel: "Confirm",
      danger: true,
    }).then(function (ok) {
      // issueRequest(true) re-issues with skipConfirmation set, so this
      // handler short-circuits via the `!e.detail.question` guard on the
      // re-fired event (htmx still dispatches htmx:confirm, but with the
      // confirmed flag — recent builds may not set question to null, so
      // the guard above plus the skip-flag both serve as belts).
      if (ok && typeof issue === "function") issue(true);
    });
  });
})();
