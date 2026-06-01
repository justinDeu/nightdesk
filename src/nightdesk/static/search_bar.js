/*
 * Unified search/filter bar behaviour.
 *
 * Enhances every [data-nd-searchbar]: the text input owns the query string,
 * facet buttons + autocomplete pull existing values from /search/suggest, and
 * field=value comparisons render as removable chips below. The bar emits:
 *   - `nd:search` { query }  (debounced as you type, immediate on pick/remove)
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

  // ---- per-bar controller ------------------------------------------------ //
  function init(bar) {
    if (bar.__ndSearchInit) return;
    bar.__ndSearchInit = true;

    var input = bar.querySelector("[data-sb-input]");
    var chipsEl = bar.querySelector("[data-sb-chips]");
    var menu = bar.querySelector("[data-sb-menu]");
    var clearBtn = bar.querySelector("[data-sb-clear]");
    var facetBtns = Array.prototype.slice.call(bar.querySelectorAll("[data-sb-facet]"));
    var viewBtns = Array.prototype.slice.call(bar.querySelectorAll("[data-sb-view]"));
    var debounceTimer = null;
    var menuItems = [];
    var menuActive = -1;
    var menuMode = null; // 'facet' | 'auto'
    var menuCtx = null;

    function resource() { return bar.dataset.resource || "ticket"; }

    function emit(immediate) {
      if (debounceTimer) { clearTimeout(debounceTimer); debounceTimer = null; }
      var fire = function () {
        bar.dataset.query = input.value;
        bar.dispatchEvent(new CustomEvent("nd:search", {
          detail: { query: input.value }, bubbles: true,
        }));
      };
      if (immediate) fire();
      else debounceTimer = setTimeout(fire, 250);
    }

    function syncClear() {
      if (clearBtn) clearBtn.hidden = !input.value;
    }

    // ---- chips ----------------------------------------------------------- //
    function chipLabel(t) {
      if (t.op === "=" || t.op === ":") return t.field + ": " + t.value;
      return t.field + " " + t.op + " " + t.value;
    }

    function renderChips() {
      var cmps = tokenize(input.value).filter(function (t) { return t.kind === "cmp"; });
      chipsEl.innerHTML = "";
      cmps.forEach(function (t, ordinal) {
        var chip = document.createElement("span");
        chip.className = "nd-chip" + (t.neg ? " is-neg" : "");
        var label = document.createElement("span");
        label.textContent = (t.neg ? "not " : "") + chipLabel(t);
        var x = document.createElement("button");
        x.type = "button";
        x.className = "nd-chip-x";
        x.setAttribute("aria-label", "Remove " + t.field + " filter");
        x.textContent = "✕";
        x.addEventListener("click", function () { removeCmp(ordinal); });
        chip.appendChild(label);
        chip.appendChild(x);
        chipsEl.appendChild(chip);
      });
    }

    function removeCmp(ordinal) {
      var cmps = tokenize(input.value).filter(function (t) { return t.kind === "cmp"; });
      var target = cmps[ordinal];
      if (!target) return;
      var next = input.value.slice(0, target.start) + input.value.slice(target.end);
      input.value = next.replace(/\s{2,}/g, " ").trim();
      afterChange(true);
    }

    function afterChange(immediate) {
      renderChips();
      syncClear();
      emit(immediate);
    }

    // ---- dropdown menu --------------------------------------------------- //
    function closeMenu() {
      menu.hidden = true;
      menu.innerHTML = "";
      menuItems = [];
      menuActive = -1;
      menuMode = null;
      menuCtx = null;
      facetBtns.forEach(function (b) { b.setAttribute("aria-expanded", "false"); });
    }

    function positionMenu(rect) {
      menu.style.top = (rect.bottom + 4) + "px";
      menu.style.left = rect.left + "px";
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
        positionMenu(btn.getBoundingClientRect());
        menu.hidden = false;
        fetchSuggest(field, "").then(function (values) {
          if (menu.hidden) return;
          fillMenu(field, values, function (val) {
            setField(field, val);
            closeMenu();
            input.focus();
          });
        });
      });
    });

    // Insert or replace `field=value` in the query (first match of that field).
    function setField(field, value) {
      var toks = tokenize(input.value);
      var existing = null;
      for (var i = 0; i < toks.length; i++) {
        if (toks[i].kind === "cmp" && toks[i].field === field && !toks[i].neg) {
          existing = toks[i];
          break;
        }
      }
      var frag = field + "=" + quoteIfNeeded(value);
      if (existing) {
        input.value = input.value.slice(0, existing.start) + frag +
          input.value.slice(existing.end);
      } else {
        input.value = (input.value.trim() + " " + frag).trim();
      }
      afterChange(true);
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
      var prefix = m[3] || "";
      menuMode = "auto";
      menuCtx = tok;
      positionMenu(input.getBoundingClientRect());
      menu.hidden = false;
      fetchSuggest(field, prefix).then(function (values) {
        if (menu.hidden || menuMode !== "auto") return;
        if (!values.length) { closeMenu(); return; }
        fillMenu(field, values, function (val) {
          completeToken(tok, field, val);
        });
      });
    }

    function completeToken(tok, field, value) {
      var frag = field + "=" + quoteIfNeeded(value);
      input.value = input.value.slice(0, tok.start) + frag + " " +
        input.value.slice(tok.end);
      var caret = tok.start + frag.length + 1;
      afterChange(true);
      input.focus();
      input.setSelectionRange(caret, caret);
      closeMenu();
    }

    // ---- input events ---------------------------------------------------- //
    input.addEventListener("input", function () {
      renderChips();
      syncClear();
      emit(false);
      maybeAutocomplete();
    });

    input.addEventListener("keydown", function (e) {
      if (!menu.hidden && menuItems.length) {
        if (e.key === "ArrowDown") { e.preventDefault(); highlight((menuActive + 1) % menuItems.length); return; }
        if (e.key === "ArrowUp") { e.preventDefault(); highlight((menuActive - 1 + menuItems.length) % menuItems.length); return; }
        if (e.key === "Enter") {
          if (menuActive >= 0) { e.preventDefault(); menuItems[menuActive].dispatchEvent(new MouseEvent("mousedown")); return; }
        }
        if (e.key === "Escape") { e.preventDefault(); closeMenu(); return; }
      }
      if (e.key === "Enter") { e.preventDefault(); emit(true); closeMenu(); }
    });

    input.addEventListener("blur", function () {
      // Delay so a menu mousedown can fire first.
      setTimeout(function () { closeMenu(); }, 150);
    });

    if (clearBtn) {
      clearBtn.addEventListener("click", function () {
        input.value = "";
        afterChange(true);
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
          detail: { view: view, query: input.value }, bubbles: true,
        }));
      });
    });

    // Reposition / dismiss the floating menu on scroll + resize.
    window.addEventListener("resize", closeMenu);
    document.addEventListener("scroll", function () { if (!menu.hidden) closeMenu(); }, true);

    // Initial paint.
    renderChips();
    syncClear();
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
