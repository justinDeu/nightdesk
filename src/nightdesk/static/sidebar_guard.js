// Sidebar view/edit pane swap + dirty-state navigation guard.
//
// Edit mode renders two panes in the sidebar:
//   * data-sidebar-view  — readonly summary (title, prompt, source path, workspace,
//                          actions). Visible by default.
//   * data-sidebar-form-pane (also has data-sidebar-form) — the full form,
//                          initially `hidden`. Clicking Edit shows it and
//                          hides the view; Cancel restores the view and
//                          calls form.reset() so typed-but-unsaved values
//                          are discarded.
//
// Create mode only renders the form (no view pane, no Edit button).
//
// Dirty state is tracked while the form pane is visible. Navigation away
// (htmx swap targeting #sidebar, or plain anchor click) is intercepted and
// gated via ndConfirm. Board column polls hit a different target and are
// not affected.
(function () {
  var dirty = false;
  var activeForm = null;

  function form() {
    return document.querySelector("[data-sidebar-form]");
  }

  function viewPane() {
    return document.querySelector("[data-sidebar-view]");
  }

  function editing() {
    return !!(activeForm && !activeForm.hasAttribute("hidden"));
  }

  function showEdit() {
    var v = viewPane();
    if (v) v.setAttribute("hidden", "");
    if (activeForm) {
      activeForm.removeAttribute("hidden");
      dirty = false;
      var first = activeForm.querySelector('input[name="title"]');
      if (first) {
        first.focus();
        try { first.setSelectionRange(first.value.length, first.value.length); } catch (e) {}
      }
    }
  }

  function showView() {
    if (activeForm) {
      try { activeForm.reset(); } catch (e) {}
      activeForm.setAttribute("hidden", "");
    }
    var v = viewPane();
    if (v) v.removeAttribute("hidden");
    dirty = false;
  }

  function bind(formEl) {
    if (!formEl || formEl.__ndBound) return;
    formEl.__ndBound = true;
    activeForm = formEl;
    dirty = false;

    formEl.addEventListener("input", function () { dirty = true; });
    formEl.addEventListener("change", function () { dirty = true; });
    // Save submit clears dirty; the server's HX-Redirect reloads the page
    // back into view mode with the saved values.
    formEl.addEventListener("submit", function () { dirty = false; });

    // Edit triggers may live in the view pane (only in edit mode).
    document.querySelectorAll("[data-sidebar-edit-trigger]").forEach(function (btn) {
      if (btn.__ndBound) return;
      btn.__ndBound = true;
      btn.addEventListener("click", showEdit);
    });
    // Cancel lives inside the form pane.
    formEl.querySelectorAll("[data-sidebar-cancel]").forEach(function (btn) {
      if (btn.__ndBound) return;
      btn.__ndBound = true;
      btn.addEventListener("click", function () {
        if (!dirty) { showView(); return; }
        ask().then(function (ok) { if (ok) showView(); });
      });
    });
  }

  function ask() {
    if (!window.ndConfirm) return Promise.resolve(true);
    return window.ndConfirm({
      title: "Discard unsaved changes?",
      message: "You have unsaved changes in the sidebar. Leaving will lose them.",
      okLabel: "Discard",
      cancelLabel: "Keep editing",
      danger: true,
    });
  }

  // --- htmx interception ------------------------------------------------
  document.body.addEventListener("htmx:confirm", function (e) {
    if (!editing() || !dirty) return;
    var detail = e.detail || {};
    var elt = detail.elt;
    // Anything inside the form (submit, etc.) is in-form activity, not a
    // navigation away. The Cancel button is handled with its own listener
    // and isn't an htmx trigger.
    if (elt && activeForm && (elt === activeForm || activeForm.contains(elt))) return;
    var target = detail.target;
    var wouldSwapSidebar =
      (target && target.id === "sidebar") ||
      (elt && elt.getAttribute && elt.getAttribute("hx-target") === "#sidebar");
    if (!wouldSwapSidebar) return;
    // Don't double-prompt on top of an hx-confirm question (e.g. Delete).
    if (detail.question) return;

    e.preventDefault();
    var issue = detail.issueRequest;
    ask().then(function (ok) {
      if (ok) {
        dirty = false;
        if (typeof issue === "function") issue(true);
      }
    });
  });

  // --- anchor navigation interception -----------------------------------
  document.addEventListener("click", function (e) {
    if (!editing() || !dirty) return;
    if (e.defaultPrevented) return;
    if (e.button !== 0) return;
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var anchor = e.target.closest && e.target.closest("a[href]");
    if (!anchor) return;
    if (anchor.target === "_blank") return;
    var href = anchor.getAttribute("href");
    if (!href || href.charAt(0) === "#") return;
    // htmx anchors go through htmx:confirm.
    if (anchor.hasAttribute("hx-get") || anchor.hasAttribute("hx-post") ||
        anchor.hasAttribute("hx-put") || anchor.hasAttribute("hx-delete")) {
      return;
    }
    e.preventDefault();
    ask().then(function (ok) {
      if (ok) {
        dirty = false;
        window.location.assign(anchor.href);
      }
    });
  }, true);

  function init() { bind(form()); }
  document.addEventListener("DOMContentLoaded", init);
  document.body.addEventListener("htmx:afterSwap", init);
  init();
})();
