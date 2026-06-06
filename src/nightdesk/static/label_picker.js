/**
 * Multi-select label picker — client-side logic.
 *
 * This is the MULTI-VALUE search-and-select control used for ticket labels
 * (add/remove many, inline create). It is intentionally separate from the
 * single-value anchored property picker (property_picker.js, exposing
 * window.ndOpenPropertyPicker / ndPropertyPickerCommitted) which backs
 * priority/status/project/profile. The two share no globals — these
 * functions live under the window.ndPicker* namespace.
 *
 * Supports searching, selecting/deselecting items, and inline creation.
 * Each picker instance reads its candidates from a sibling
 * `<script type="application/json" data-picker-candidates>` block.
 *
 * Public API (all on `window`):
 *   ndPickerSearch(input)        — show suggestions matching input value
 *   ndPickerSearchKey(event, input) — keyboard nav (up/down/enter/esc)
 *   ndPickerSearchClose(input)   — hide suggestions
 *   ndPickerRemove(btn)          — remove a selected item
 *   ndPickerAdd(pickerEl, item)  — add a candidate to the selected list
 */
(function () {
  function _picker(input) {
    return input.closest("[data-property-picker]");
  }

  function _candidates(picker) {
    var script = picker.querySelector("[data-picker-candidates]");
    if (!script) return [];
    try { return JSON.parse(script.textContent); } catch (e) { return []; }
  }

  function _selectedIds(picker) {
    var ids = [];
    picker.querySelectorAll("[data-picker-item]").forEach(function (el) {
      ids.push(el.getAttribute("data-item-id"));
    });
    return ids;
  }

  function _name(picker) {
    var hidden = picker.querySelector('input[type=hidden][name]');
    if (hidden && hidden.name !== 'labels_form') return hidden.name;
    // fallback
    var items = picker.querySelectorAll('[data-picker-item] input[type=hidden]');
    return items.length ? items[0].name : 'label_ids';
  }

  function _renderItem(picker, item) {
    var name = _name(picker);
    var span = document.createElement("span");
    span.setAttribute("data-picker-item", "");
    span.setAttribute("data-item-id", item.id);
    span.className = "inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium border group";
    if (item.color) {
      span.style.backgroundColor = item.color + "22";
      span.style.borderColor = item.color + "66";
      span.style.color = item.color;
    } else {
      span.style.backgroundColor = "var(--color-bg-elev-2)";
      span.style.borderColor = "var(--color-border)";
      span.style.color = "var(--color-fg-muted)";
    }
    span.innerHTML =
      '<span>' + _esc(item.name) + '</span>' +
      '<button type="button" onclick="window.ndPickerRemove(this)" class="opacity-40 hover:opacity-100 leading-none">&times;</button>' +
      '<input type="hidden" name="' + _esc(name) + '" value="' + _esc(item.id) + '" />';
    return span;
  }

  function _esc(s) {
    var d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }

  window.ndPickerSearch = function (input) {
    var picker = _picker(input);
    if (!picker) return;
    var suggest = picker.querySelector("[data-picker-suggest]");
    if (!suggest) return;

    var q = (input.value || "").trim().toLowerCase();
    var selected = _selectedIds(picker);
    var all = _candidates(picker);
    var items = all.filter(function (c) {
      if (selected.indexOf(c.id) !== -1) return false;
      if (!q) return true;
      return c.name.toLowerCase().indexOf(q) !== -1;
    });

    if (!items.length) {
      suggest.classList.add("hidden");
      return;
    }

    suggest.innerHTML = "";
    items.slice(0, 20).forEach(function (item) {
      var div = document.createElement("div");
      div.className = "px-2 py-1.5 text-xs cursor-pointer hover:bg-bg-elev-2 text-fg flex items-center gap-1.5";
      var dot = item.color
        ? '<span class="inline-block w-2 h-2 rounded-full" style="background:' + _esc(item.color) + '"></span>'
        : '<span class="inline-block w-2 h-2 rounded-full bg-fg-muted/30"></span>';
      div.innerHTML = dot + " " + _esc(item.name);
      div.setAttribute("data-picker-candidate-id", item.id);
      div.addEventListener("mousedown", function (e) {
        e.preventDefault();
        window.ndPickerAdd(picker, item);
        input.value = "";
        suggest.classList.add("hidden");
        input.focus();
      });
      suggest.appendChild(div);
    });
    suggest.classList.remove("hidden");
  };

  window.ndPickerSearchKey = function (event, input) {
    var picker = _picker(input);
    if (!picker) return;
    var suggest = picker.querySelector("[data-picker-suggest]");
    if (!suggest) return;

    if (event.key === "Escape") {
      suggest.classList.add("hidden");
      return;
    }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      var items = suggest.querySelectorAll("[data-picker-candidate-id]");
      if (!items.length) return;
      var focused = suggest.querySelector(".bg-bg-elev-2");
      var idx = -1;
      items.forEach(function (el, i) { if (el === focused) idx = i; });
      if (focused) focused.classList.remove("bg-bg-elev-2");
      if (event.key === "ArrowDown") idx = (idx + 1) % items.length;
      else idx = idx <= 0 ? items.length - 1 : idx - 1;
      items[idx].classList.add("bg-bg-elev-2");
      return;
    }
    if (event.key === "Enter") {
      var focused = suggest.querySelector(".bg-bg-elev-2");
      if (focused) {
        event.preventDefault();
        focused.dispatchEvent(new Event("mousedown"));
      }
    }
  };

  window.ndPickerSearchClose = function (input) {
    var picker = _picker(input);
    if (!picker) return;
    var suggest = picker.querySelector("[data-picker-suggest]");
    if (suggest) {
      setTimeout(function () { suggest.classList.add("hidden"); }, 150);
    }
  };

  window.ndPickerRemove = function (btn) {
    var item = btn.closest("[data-picker-item]");
    if (item) item.remove();
  };

  window.ndPickerAdd = function (picker, item) {
    var container = picker.querySelector("[data-picker-selected]");
    if (!container) return;
    // Don't add duplicates
    var existing = _selectedIds(picker);
    if (existing.indexOf(item.id) !== -1) return;
    container.appendChild(_renderItem(picker, item));
  };
})();
