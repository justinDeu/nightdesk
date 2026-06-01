/*
 * Unified search/filter bar behaviour.
 *
 * The bar looks like one input box: a magnifying glass, then inline filter
 * chips, then the text you type. Simple `field=value` filters (from facet
 * picks, autocomplete, or typing a token + space) become inline chips. Free
 * text and advanced boolean (OR / NOT / parens / "phrases") stay as typed text.
 * The full query sent to the backend is the chips serialized plus the free
 * text. The bar emits:
 *   - `nd:search` { query }  (debounced while typing, immediate on chip change)
 *   - `nd:view`   { view }   (Tickets/Runs toggle, when present)
 * The host page owns the effect (poll the board, fetch runs, run the palette).
 */
(function () {
  "use strict";

  // Mirrors domain/query.py FIELD_ALIASES.
  var FIELDS = {
    project: 1, status: 1, latest_status: 1, latest: 1, outcome: 1, profile: 1,
    backend: 1, model: 1, cost: 1, priority: 1, created: 1, started: 1,
    finished: 1, intent: 1, failure_kind: 1,
  };
  // Fields with a server-backed value list (others are free-typed).
  var SUGGESTABLE = {
    project: 1, status: 1, latest_status: 1, latest: 1, outcome: 1, profile: 1,
    backend: 1, model: 1, intent: 1,
  };
  var FIELD_RE = /^-?([A-Za-z_]+)(!=|>=|<=|=|:|>|<)(.*)$/;

  // ---- tokenizer (mirror of the Python scanner, with positions) ---------- //
  function tokenize(s) {
    var toks = [];
    var i = 0, n = s.length;
    while (i < n) {
      var c = s[i];
      if (/\s/.test(c)) { i++; continue; }
      var start = i;
      if (c === "(" || c === ")") {
        toks.push({ text: c, start: start, end: i + 1, kind: c === "(" ? "lparen" : "rparen" });
        i++;
        continue;
      }
      var lead = c === '"';
      var buf = "";
      while (i < n && !/\s/.test(s[i]) && s[i] !== "(" && s[i] !== ")") {
        if (s[i] === '"') {
          i++;
          while (i < n && s[i] !== '"') { buf += s[i]; i++; }
          i++; // closing quote
        } else {
          buf += s[i];
          i++;
        }
      }
      toks.push(classify(buf, lead, start, i));
    }
    return toks;
  }

  function classify(raw, lead, start, end) {
    var low = raw.toLowerCase();
    if (!lead) {
      if (low === "or") return { text: raw, start: start, end: end, kind: "or" };
      if (low === "and") return { text: raw, start: start, end: end, kind: "and" };
      if (low === "not") return { text: raw, start: start, end: end, kind: "not" };
    }
    var neg = false, body = raw;
    if (!lead && raw[0] === "-" && raw.length > 1) { neg = true; body = raw.slice(1); }
    var m = !lead && FIELD_RE.exec(body);
    if (m && FIELDS[m[1].toLowerCase()] && m[3] !== "") {
      return {
        text: raw, start: start, end: end, kind: "cmp",
        neg: neg, field: m[1].toLowerCase(), op: m[2], value: m[3],
      };
    }
    return { text: raw, start: start, end: end, kind: lead ? "phrase" : "word" };
  }

  function quoteIfNeeded(v) {
    return /\s/.test(v) ? '"' + v.replace(/"/g, "") + '"' : v;
  }

  function serializeFilter(f) {
    return (f.neg ? "-" : "") + f.field + (f.op || "=") + quoteIfNeeded(f.value);
  }

  // ---- per-bar controller ------------------------------------------------ //
  function init(bar) {
    if (bar.__ndSearchInit) return;
    bar.__ndSearchInit = true;

    var box = bar.querySelector("[data-sb-box]");
    var input = bar.querySelector("[data-sb-input]");
    var menu = bar.querySelector("[data-sb-menu]");
    var clearBtn = bar.querySelector("[data-sb-clear]");
    var facetBtns = Array.prototype.slice.call(bar.querySelectorAll("[data-sb-facet]"));
    var viewBtns = Array.prototype.slice.call(bar.querySelectorAll("[data-sb-view]"));

    var filters = [];           // [{field, op, value, neg}]
    var debounceTimer = null;
    var menuItems = [];
    var menuActive = -1;
    var menuMode = null;        // 'facet' | 'auto'
    var menuCtx = null;
    var menuAnchor = null;      // element the floating menu is positioned under

    function resource() { return bar.dataset.resource || "ticket"; }

    function buildQuery() {
      var parts = filters.map(serializeFilter);
      var free = input.value.trim();
      if (free) parts.push(free);
      return parts.join(" ").trim();
    }

    function emit(immediate) {
      if (debounceTimer) { clearTimeout(debounceTimer); debounceTimer = null; }
      var fire = function () {
        var q = buildQuery();
        bar.dataset.query = q;
        bar.dispatchEvent(new CustomEvent("nd:search", { detail: { query: q }, bubbles: true }));
      };
      if (immediate) fire();
      else debounceTimer = setTimeout(fire, 250);
    }

    function syncClear() {
      if (clearBtn) clearBtn.hidden = !(filters.length || input.value);
    }

    // ---- chips ----------------------------------------------------------- //
    function chipLabel(f) {
      var op = f.op || "=";
      if (op === "=" || op === ":") return f.field + ": " + f.value;
      return f.field + " " + op + " " + f.value;
    }

    function renderChips() {
      Array.prototype.slice.call(box.querySelectorAll(".nd-chip")).forEach(function (c) {
        c.remove();
      });
      filters.forEach(function (f, idx) {
        var chip = document.createElement("span");
        chip.className = "nd-chip" + (f.neg ? " is-neg" : "");
        var label = document.createElement("button");
        label.type = "button";
        label.className = "nd-chip-label";
        label.title = "Click to edit";
        label.textContent = (f.neg ? "not " : "") + chipLabel(f);
        label.addEventListener("click", function () { editFilter(idx); });
        var x = document.createElement("button");
        x.type = "button";
        x.className = "nd-chip-x";
        x.setAttribute("aria-label", "Remove " + f.field + " filter");
        x.textContent = "✕";
        x.addEventListener("click", function () { removeFilter(idx); });
        chip.appendChild(label);
        chip.appendChild(x);
        box.insertBefore(chip, input);
      });
    }

    function addFilter(field, value, op, neg) {
      op = op || "=";
      neg = !!neg;
      var replaced = false;
      for (var i = 0; i < filters.length; i++) {
        if (filters[i].field === field && !!filters[i].neg === neg) {
          filters[i] = { field: field, op: op, value: value, neg: neg };
          replaced = true;
          break;
        }
      }
      if (!replaced) filters.push({ field: field, op: op, value: value, neg: neg });
      renderChips();
      syncClear();
      emit(true);
    }

    function removeFilter(idx) {
      filters.splice(idx, 1);
      renderChips();
      syncClear();
      emit(true);
    }

    // Click a chip to edit it: drop it back into the input as text, so you can
    // tweak the value (e.g. add ,running) and re-chip with space or Enter.
    function editFilter(idx) {
      var f = filters[idx];
      filters.splice(idx, 1);
      renderChips();
      var frag = serializeFilter(f);
      var cur = input.value.trim();
      input.value = cur ? cur + " " + frag : frag;
      syncClear();
      // Query is unchanged (chip text moved into the input), so no re-search.
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    }

    // ---- facet multi-select (build comma value lists) -------------------- //
    function fieldChipIndex(field) {
      for (var i = 0; i < filters.length; i++) {
        if (filters[i].field === field && !filters[i].neg &&
            (filters[i].op === "=" || filters[i].op === ":")) {
          return i;
        }
      }
      return -1;
    }

    function facetSelectedValues(field) {
      var i = fieldChipIndex(field);
      if (i < 0) return [];
      return filters[i].value.split(",").map(function (s) { return s.trim(); })
        .filter(Boolean);
    }

    function toggleFacetValue(field, value) {
      var i = fieldChipIndex(field);
      if (i < 0) {
        filters.push({ field: field, op: "=", value: value, neg: false });
      } else {
        var vals = facetSelectedValues(field);
        var at = vals.indexOf(value);
        if (at >= 0) vals.splice(at, 1);
        else vals.push(value);
        if (vals.length === 0) filters.splice(i, 1);
        else filters[i].value = vals.join(",");
      }
      renderChips();
      syncClear();
      emit(true);
    }

    // ---- dropdown menu --------------------------------------------------- //
    function closeMenu() {
      menu.hidden = true;
      menu.innerHTML = "";
      menuItems = [];
      menuActive = -1;
      menuMode = null;
      menuCtx = null;
      menuAnchor = null;
      facetBtns.forEach(function (b) { b.setAttribute("aria-expanded", "false"); });
    }

    function positionMenu(rect) {
      menu.style.top = (rect.bottom + 4) + "px";
      menu.style.left = rect.left + "px";
    }

    function repositionMenu() {
      if (menu.hidden || !menuAnchor || !document.contains(menuAnchor)) return;
      positionMenu(menuAnchor.getBoundingClientRect());
    }

    function highlight(idx) {
      menuItems.forEach(function (el, i) { el.classList.toggle("is-active", i === idx); });
      menuActive = idx;
      if (menuItems[idx]) menuItems[idx].scrollIntoView({ block: "nearest" });
    }

    function fillMenu(field, values, onPick) {
      menu.innerHTML = "";
      menuItems = [];
      var head = document.createElement("div");
      head.className = "nd-sb-menu-head";
      head.textContent = field;
      menu.appendChild(head);
      if (!values.length) {
        var empty = document.createElement("div");
        empty.className = "nd-sb-menu-empty";
        empty.textContent = "No values";
        menu.appendChild(empty);
        return;
      }
      values.forEach(function (v) {
        var item = document.createElement("button");
        item.type = "button";
        item.className = "nd-sb-menu-item";
        item.setAttribute("role", "option");
        var label = document.createElement("span");
        label.textContent = v.label;
        item.appendChild(label);
        if (v.label !== v.value) {
          var sub = document.createElement("span");
          sub.className = "nd-sb-menu-head";
          sub.style.marginLeft = "auto";
          sub.style.padding = "0";
          sub.textContent = v.value;
          item.appendChild(sub);
        }
        item.addEventListener("mousedown", function (e) {
          e.preventDefault();
          onPick(v.value);
        });
        menu.appendChild(item);
        menuItems.push(item);
      });
      highlight(0);
    }

    function fetchSuggest(field, prefix) {
      var url = "/search/suggest?resource=" + encodeURIComponent(resource()) +
        "&field=" + encodeURIComponent(field) +
        "&q=" + encodeURIComponent(prefix || "");
      return fetch(url, { credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.json() : { values: [] }; })
        .then(function (d) { return (d && d.values) || []; })
        .catch(function () { return []; });
    }

    // ---- facet buttons --------------------------------------------------- //
    facetBtns.forEach(function (btn) {
      btn.addEventListener("click", function () {
        var field = btn.dataset.sbFacet;
        if (menuMode === "facet" && menuCtx === field && !menu.hidden) {
          closeMenu();
          return;
        }
        closeMenu();
        menuMode = "facet";
        menuCtx = field;
        btn.setAttribute("aria-expanded", "true");
        menuAnchor = btn;
        positionMenu(btn.getBoundingClientRect());
        menu.hidden = false;
        fetchSuggest(field, "").then(function (values) {
          if (menu.hidden) return;
          fillFacetMenu(field, values);
        });
      });
    });

    // Facet menu: multi-select toggles that build a comma value list. Stays
    // open across picks so you can choose several (status=review,running).
    function fillFacetMenu(field, values) {
      var selected = facetSelectedValues(field);
      menu.innerHTML = "";
      menuItems = [];
      var head = document.createElement("div");
      head.className = "nd-sb-menu-head";
      head.textContent = field + " — pick one or more";
      menu.appendChild(head);
      if (!values.length) {
        var empty = document.createElement("div");
        empty.className = "nd-sb-menu-empty";
        empty.textContent = "No values";
        menu.appendChild(empty);
        return;
      }
      values.forEach(function (v) {
        var isSel = selected.indexOf(v.value) >= 0;
        var item = document.createElement("button");
        item.type = "button";
        item.className = "nd-sb-menu-item" + (isSel ? " is-selected" : "");
        item.setAttribute("role", "option");
        item.setAttribute("aria-selected", isSel ? "true" : "false");
        var check = document.createElement("span");
        check.className = "nd-sb-check";
        check.textContent = isSel ? "✓" : "";
        item.appendChild(check);
        var label = document.createElement("span");
        label.textContent = v.label;
        item.appendChild(label);
        if (v.label !== v.value) {
          var sub = document.createElement("span");
          sub.className = "nd-sb-menu-head";
          sub.style.marginLeft = "auto";
          sub.style.padding = "0";
          sub.textContent = v.value;
          item.appendChild(sub);
        }
        item.addEventListener("mousedown", function (e) {
          e.preventDefault();
          toggleFacetValue(field, v.value);
          fillFacetMenu(field, values); // re-render with updated ticks, keep open
        });
        menu.appendChild(item);
        menuItems.push(item);
      });
    }

    // ---- autocomplete as you type --------------------------------------- //
    function caretToken() {
      var pos = input.selectionStart;
      var toks = tokenize(input.value);
      for (var i = 0; i < toks.length; i++) {
        if (pos >= toks[i].start && pos <= toks[i].end) return toks[i];
      }
      return null;
    }

    function maybeAutocomplete() {
      var tok = caretToken();
      if (!tok || (tok.kind !== "cmp" && tok.kind !== "word")) { closeMenu(); return; }
      var m = FIELD_RE.exec(tok.text);
      if (!m) { closeMenu(); return; }
      var field = m[1].toLowerCase();
      if (!SUGGESTABLE[field]) { closeMenu(); return; }
      var op = m[2];
      var prefix = m[3] || "";
      menuMode = "auto";
      menuCtx = tok;
      menuAnchor = box;
      positionMenu(box.getBoundingClientRect());
      menu.hidden = false;
      fetchSuggest(field, prefix).then(function (values) {
        if (menu.hidden || menuMode !== "auto") return;
        if (!values.length) { closeMenu(); return; }
        fillMenu(field, values, function (val) {
          completeToken(tok, field, val, op);
        });
      });
    }

    function completeToken(tok, field, value, op) {
      // Drop the partial token from the free text and add a chip instead.
      input.value = (input.value.slice(0, tok.start) + input.value.slice(tok.end))
        .replace(/\s{2,}/g, " ").trim();
      closeMenu();
      addFilter(field, value, op === ":" ? ":" : "=");
      input.focus();
    }

    // Type `field=value` + space -> inline chip (unless it's part of a boolean
    // expression, which stays as free text).
    function maybeChipifyOnSpace() {
      if (!/\s$/.test(input.value)) return false;
      var trimmed = input.value.trim();
      if (!trimmed) return false;
      var toks = tokenize(trimmed);
      if (toks.length === 1 && toks[0].kind === "cmp") {
        var t = toks[0];
        input.value = "";
        addFilter(t.field, t.value, t.op, t.neg);
        return true;
      }
      return false;
    }

    // ---- input events ---------------------------------------------------- //
    input.addEventListener("input", function () {
      if (maybeChipifyOnSpace()) return;
      syncClear();
      emit(false);
      maybeAutocomplete();
    });

    input.addEventListener("keydown", function (e) {
      if (!menu.hidden && menuItems.length) {
        if (e.key === "ArrowDown") { e.preventDefault(); highlight((menuActive + 1) % menuItems.length); return; }
        if (e.key === "ArrowUp") { e.preventDefault(); highlight((menuActive - 1 + menuItems.length) % menuItems.length); return; }
        if (e.key === "Enter" && menuActive >= 0) { e.preventDefault(); menuItems[menuActive].dispatchEvent(new MouseEvent("mousedown")); return; }
        if (e.key === "Escape") { e.preventDefault(); closeMenu(); return; }
      }
      // Backspace at the very start with no text removes the last chip.
      if (e.key === "Backspace" && !input.value &&
          input.selectionStart === 0 && input.selectionEnd === 0 && filters.length) {
        e.preventDefault();
        removeFilter(filters.length - 1);
        return;
      }
      if (e.key === "Enter") {
        e.preventDefault();
        var trimmed = input.value.trim();
        var toks = trimmed ? tokenize(trimmed) : [];
        if (toks.length === 1 && toks[0].kind === "cmp") {
          input.value = "";
          addFilter(toks[0].field, toks[0].value, toks[0].op, toks[0].neg);
        } else {
          emit(true);
        }
        closeMenu();
      }
    });

    input.addEventListener("focus", function () {
      // Switching from facet-picking to typing dismisses the facet menu.
      if (menuMode === "facet") closeMenu();
    });

    input.addEventListener("blur", function () {
      // Only auto-dismiss the autocomplete menu on blur; a facet menu opened by
      // a button click must not be torn down by the input losing focus.
      setTimeout(function () { if (menuMode === "auto") closeMenu(); }, 150);
    });

    // Clicking empty space in the box focuses the input.
    box.addEventListener("mousedown", function (e) {
      if (e.target === box || e.target === input.previousElementSibling) {
        input.focus();
      }
    });

    if (clearBtn) {
      clearBtn.addEventListener("click", function () {
        filters = [];
        input.value = "";
        renderChips();
        syncClear();
        emit(true);
        input.focus();
      });
    }

    // ---- view toggle ----------------------------------------------------- //
    viewBtns.forEach(function (btn) {
      btn.addEventListener("click", function () {
        var view = btn.dataset.sbView;
        viewBtns.forEach(function (b) {
          b.setAttribute("aria-pressed", b === btn ? "true" : "false");
        });
        bar.dataset.resource = view === "runs" ? "run" : "ticket";
        bar.dispatchEvent(new CustomEvent("nd:view", {
          detail: { view: view, query: buildQuery() }, bubbles: true,
        }));
      });
    });

    // Reposition (don't close) on scroll/resize: a board refresh re-mounts the
    // columns and fires spurious scroll events, which must not dismiss an open
    // facet menu mid-multi-select. The anchor stays put, so this is a no-op
    // unless the bar itself actually moves.
    window.addEventListener("resize", repositionMenu);
    document.addEventListener("scroll", repositionMenu, true);
    // Dismiss on any mousedown that is not inside the menu itself or on a facet
    // button. Clicking the input, a chip, elsewhere in the bar, or off the bar
    // all close it. composedPath is captured at dispatch, so it still reflects
    // the menu even though picking a value re-renders (and detaches) the node.
    function pathHas(e, el) {
      var path = typeof e.composedPath === "function" ? e.composedPath() : [];
      return path.indexOf(el) !== -1 || (el && el.contains && el.contains(e.target));
    }
    document.addEventListener("mousedown", function (e) {
      if (menu.hidden) return;
      var onFacet = facetBtns.some(function (b) { return pathHas(e, b); });
      if (!pathHas(e, menu) && !onFacet) closeMenu();
    });
    // Escape closes it even when the input isn't focused (facet menu).
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !menu.hidden) closeMenu();
    });

    // ---- initial state from the prefilled query -------------------------- //
    (function initFilters() {
      var q = bar.dataset.query || "";
      if (!q.trim()) { input.value = ""; renderChips(); syncClear(); return; }
      var toks = tokenize(q);
      var hasBoolean = toks.some(function (t) {
        return t.kind === "or" || t.kind === "lparen" || t.kind === "rparen";
      });
      if (hasBoolean) {
        // Keep advanced boolean intact as raw text rather than flatten it.
        input.value = q;
        renderChips();
        syncClear();
        return;
      }
      var freeParts = [];
      var pendingNeg = false;
      toks.forEach(function (t) {
        if (t.kind === "not") { pendingNeg = true; return; }
        if (t.kind === "cmp") {
          filters.push({ field: t.field, op: t.op, value: t.value, neg: !!t.neg || pendingNeg });
          pendingNeg = false;
        } else if (t.kind === "word") {
          freeParts.push(t.text);
          pendingNeg = false;
        } else if (t.kind === "phrase") {
          freeParts.push('"' + t.text.replace(/"/g, "") + '"');
          pendingNeg = false;
        }
      });
      input.value = freeParts.join(" ");
      renderChips();
      syncClear();
    })();
  }

  function initAll(root) {
    (root || document).querySelectorAll("[data-nd-searchbar]").forEach(init);
  }

  window.ndSearchBarInit = initAll;

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { initAll(); });
  } else {
    initAll();
  }
  document.body.addEventListener("htmx:afterSwap", function (e) { initAll(e.target); });
})();
