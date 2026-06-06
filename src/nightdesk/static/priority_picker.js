/* priority_picker.js — toggle/close logic for the inline priority picker.
 *
 * The dropdown state is managed via the `hidden` class on the
 * [data-priority-dropdown] element. One global click-listener closes any
 * open picker when clicking outside, so only one is visible at a time.
 */
(function () {
  // Toggle a picker open/closed.
  window.ndTogglePriorityPicker = function (chip) {
    var container = chip.closest("[data-priority-picker]");
    if (!container) return;
    var ticketId = container.getAttribute("data-priority-picker");
    var dd = document.querySelector('[data-priority-dropdown="' + ticketId + '"]');
    if (!dd) return;
    // Close any other open pickers first.
    document.querySelectorAll("[data-priority-dropdown]:not(.hidden)").forEach(function (el) {
      if (el !== dd) el.classList.add("hidden");
    });
    dd.classList.toggle("hidden");
  };

  // Close a specific picker by ticket id.
  window.ndClosePriorityPicker = function (ticketId) {
    var dd = document.querySelector('[data-priority-dropdown="' + ticketId + '"]');
    if (dd) dd.classList.add("hidden");
  };

  // Close all pickers when clicking outside.
  document.addEventListener("click", function (e) {
    var openPickers = document.querySelectorAll("[data-priority-dropdown]:not(.hidden)");
    openPickers.forEach(function (dd) {
      var picker = dd.closest("[data-priority-picker]");
      if (picker && !picker.contains(e.target)) {
        dd.classList.add("hidden");
      }
    });
  });

  // Close on Escape.
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") {
      document.querySelectorAll("[data-priority-dropdown]:not(.hidden)").forEach(function (dd) {
        dd.classList.add("hidden");
      });
    }
  });
})();
