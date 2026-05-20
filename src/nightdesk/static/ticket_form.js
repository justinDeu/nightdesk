// Shared interactive behavior for the ticket edit form. Used by the
// board sidebar AND the standalone edit modal so the same path-suggest,
// worktree-preview, and linked-workspace logic runs in both contexts.
//
// All entry points are attached to window.nd* so inline HTML handlers
// (oninput, onfocus, etc.) can call them from any partial.
//
// Initial counters for new-row id generation were previously templated
// into the script via Jinja. Now they're seeded from the live DOM so a
// single shared file can serve any number of forms.
(function () {
  let suggestSeq = 0;
  let previewSeq = 0;
  // dropdownId -> highlighted item index in its suggestion list.
  const activeIdx = new Map();
  // dropdownId -> the input element it is currently anchored to. The
  // dropdown hosts are position:fixed so they overlay the modal instead of
  // expanding/being clipped by the overflow-y-auto body. We reposition them
  // on scroll/resize so they keep tracking their input.
  const suggestAnchor = new Map();
  // Per-host-id counters so two forms on the same page (modal + sidebar)
  // don't collide on generated row ids.
  const linkedSeqByHost = new Map();
  // dropdownIds whose very next ndPathSuggest call must be suppressed. After
  // picking a value we fire an `input` event so the worktree preview refreshes,
  // but that same event would otherwise reopen the suggestion list. We consume
  // one suppression so the list stays closed after a pick.
  const suppressOpen = new Set();

  // Place a fixed-position dropdown host directly under its input. Width
  // matches the input; coordinates are viewport-relative (position: fixed).
  function _positionSuggest(dropdownId, inputEl) {
    const dd = document.getElementById(dropdownId);
    if (!dd || !inputEl) return;
    const r = inputEl.getBoundingClientRect();
    dd.style.left = r.left + 'px';
    dd.style.top = (r.bottom + 4) + 'px';
    dd.style.width = r.width + 'px';
  }

  function _openSuggest(dropdownId, inputEl) {
    const dd = document.getElementById(dropdownId);
    if (!dd) return;
    suggestAnchor.set(dropdownId, inputEl);
    dd.classList.remove('hidden');
    _positionSuggest(dropdownId, inputEl);
  }

  function _closeSuggest(dropdownId) {
    const dd = document.getElementById(dropdownId);
    if (dd) {
      dd.innerHTML = '';
      dd.classList.add('hidden');
    }
    suggestAnchor.delete(dropdownId);
    activeIdx.set(dropdownId, -1);
  }

  // Keep open dropdowns glued to their inputs as the form body scrolls or
  // the window resizes. Capture phase catches scrolls on the modal body.
  function _repositionAll() {
    suggestAnchor.forEach((inputEl, dropdownId) => {
      const dd = document.getElementById(dropdownId);
      if (dd && !dd.classList.contains('hidden')) {
        _positionSuggest(dropdownId, inputEl);
      }
    });
  }
  window.addEventListener('scroll', _repositionAll, true);
  window.addEventListener('resize', _repositionAll);

  function _items(dropdownId) {
    const dd = document.getElementById(dropdownId);
    if (!dd) return [];
    return Array.from(dd.querySelectorAll('[data-suggest-value]'));
  }

  function _setActive(dropdownId, idx) {
    const items = _items(dropdownId);
    if (!items.length) { activeIdx.set(dropdownId, -1); return; }
    if (idx < 0) idx = items.length - 1;
    if (idx >= items.length) idx = 0;
    items.forEach((it, i) => {
      if (i === idx) {
        it.setAttribute('data-active', 'true');
        it.classList.add('bg-bg-elev', 'text-accent');
      } else {
        it.removeAttribute('data-active');
        it.classList.remove('bg-bg-elev', 'text-accent');
      }
    });
    items[idx].scrollIntoView({ block: 'nearest' });
    activeIdx.set(dropdownId, idx);
  }

  // --- Worktree fields & preview ----------------------------------------
  //
  // The form's "use git worktree" checkbox gates the worktree_name and
  // worktree_path inputs. When toggled off we disable them and clear the
  // preview so the form can't ship a half-configured worktree request.

  function _worktreeEnabled(scope) {
    const toggle = (scope || document).querySelector('input[name="use_worktree"]');
    return !!(toggle && toggle.checked);
  }

  // Primary workspace kind select → hidden use_worktree checkbox + git
  // field visibility. Keeps the unified-list UI in sync with the legacy
  // form-field names the backend still expects.
  window.ndSyncPrimaryWorkspaceKind = function (selectEl) {
    if (!selectEl) return;
    var form = selectEl.closest('[data-ticket-form]');
    if (!form) return;
    var isGit = selectEl.value === 'git_worktree';
    var checkbox = form.querySelector('input[name="use_worktree"]');
    if (checkbox) checkbox.checked = isGit;
    var gitFields = form.querySelector('[data-primary-git-fields]');
    if (gitFields) gitFields.classList.toggle('hidden', !isGit);
    window.ndSyncWorktreeFields(form);
    window.ndScheduleWorktreePreview(0, form);
  };

  window.ndSyncWorktreeFields = function (scope) {
    const root = scope || document;
    const enabled = _worktreeEnabled(root);
    root.querySelectorAll('[data-worktree-field]').forEach((el) => {
      el.disabled = !enabled;
    });
    if (!enabled) {
      window.ndSetWorktreePreview('Enable worktree to preview the target path.', '', root);
    }
    window.ndSyncWorktreeNameHint(root);
  };

  window.ndSyncWorktreeNameHint = function (scope) {
    const root = scope || document;
    const hint = root.querySelector('[data-worktree-name-hint]');
    const nameInput = root.querySelector('input[name="worktree_name"]');
    if (!hint || !nameInput) return;
    hint.style.display = nameInput.value.trim().length > 0 ? 'none' : '';
  };

  window.ndScheduleWorktreePreview = function (delay = 250, scope) {
    clearTimeout(window.__ndWorktreePreviewTimer);
    window.__ndWorktreePreviewTimer = setTimeout(
      () => window.ndUpdateWorktreePreview(scope), delay,
    );
  };

  window.ndSetWorktreePreview = function (pathText, sourceText = '', scope) {
    const root = scope || document;
    const preview = root.querySelector('[data-worktree-path-preview]');
    if (!preview) return;
    const source = preview.querySelector('[data-worktree-preview-source]');
    const path = preview.querySelector('[data-worktree-preview-path]');
    if (source) source.textContent = sourceText ? `(${sourceText})` : '';
    if (path) path.textContent = pathText;
  };

  window.ndUpdateWorktreePreview = async function (scope) {
    const root = scope || document;
    if (!_worktreeEnabled(root)) {
      window.ndSetWorktreePreview('Enable worktree to preview the target path.', '', root);
      return;
    }
    const cwd = (root.querySelector('[data-cwd-input]') || {}).value || '';
    const name = (root.querySelector('input[name="worktree_name"]') || {}).value || '';
    const path = (root.querySelector('[data-worktree-path-input]') || {}).value || '';
    if (!cwd && !path) {
      window.ndSetWorktreePreview('Choose a working dir or custom path to preview the target.', '', root);
      return;
    }
    const mySeq = ++previewSeq;
    try {
      const r = await fetch('/board/worktree-preview?' + new URLSearchParams({ cwd, name, path, format: 'json' }));
      if (!r.ok || mySeq !== previewSeq) return;
      const data = await r.json();
      window.ndSetWorktreePreview(data.path, data.source, root);
    } catch (e) {
      if (mySeq === previewSeq) {
        window.ndSetWorktreePreview('Preview unavailable.', '', root);
      }
    }
  };

  // --- Path suggestion dropdown -----------------------------------------

  window.ndPathSuggest = async function (inputEl, dropdownId) {
    const dd = document.getElementById(dropdownId);
    if (!dd) return;
    // A pick just fired our synthetic input event; don't reopen the list.
    if (suppressOpen.has(dropdownId)) {
      suppressOpen.delete(dropdownId);
      _closeSuggest(dropdownId);
      return;
    }
    if (inputEl && inputEl.readOnly) { _closeSuggest(dropdownId); return; }
    const q = inputEl.value || '';
    if (q.length < 1) { _closeSuggest(dropdownId); return; }
    const mySeq = ++suggestSeq;
    try {
      const r = await fetch('/fs/suggest?' + new URLSearchParams({ q, target: inputEl.id }));
      if (!r.ok) { _closeSuggest(dropdownId); return; }
      if (mySeq !== suggestSeq) return;
      dd.innerHTML = await r.text();
      activeIdx.set(dropdownId, -1);
      if (dd.querySelector('[data-suggest-value]')) {
        _openSuggest(dropdownId, inputEl);
      } else {
        _closeSuggest(dropdownId);
      }
    } catch (e) { /* ignore */ }
  };

  window.ndPathSuggestClose = function (dropdownId) {
    // Delay so mousedown on a suggestion item beats the blur-driven close.
    setTimeout(() => { _closeSuggest(dropdownId); }, 180);
  };

  window.ndPathSuggestPick = function (targetId, value) {
    const el = document.getElementById(targetId);
    if (!el) return;
    el.value = value;
    el.focus();
    try { el.setSelectionRange(value.length, value.length); } catch (e) {}
    const dropdownId = targetId + '-suggest';
    // Refresh dependent UI (worktree preview) via an input event, but suppress
    // the suggestion reopen that event would trigger, then close the list.
    suppressOpen.add(dropdownId);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    _closeSuggest(dropdownId);
  };

  window.ndPathSuggestKey = function (ev, inputEl, dropdownId) {
    const items = _items(dropdownId);
    const cur = activeIdx.has(dropdownId) ? activeIdx.get(dropdownId) : -1;
    if (ev.key === 'ArrowDown') {
      if (!items.length) return;
      ev.preventDefault();
      _setActive(dropdownId, cur + 1);
    } else if (ev.key === 'ArrowUp') {
      if (!items.length) return;
      ev.preventDefault();
      _setActive(dropdownId, (cur <= 0 ? items.length : cur) - 1);
    } else if (ev.key === 'Tab') {
      // While the list is open, Tab/Shift-Tab move the highlight (down/up,
      // wrapping) instead of leaving the field. When it's closed/empty we
      // fall through so Tab moves focus normally — never trap focus.
      if (!items.length) return;
      ev.preventDefault();
      if (ev.shiftKey) {
        _setActive(dropdownId, (cur <= 0 ? items.length : cur) - 1);
      } else {
        _setActive(dropdownId, cur + 1);
      }
    } else if (ev.key === 'Enter') {
      // Enter selects the highlighted item and closes the list. With no
      // highlight yet, default to the first item so one Enter still picks.
      if (!items.length) return;
      ev.preventDefault();
      const i = cur >= 0 ? cur : 0;
      window.ndPathSuggestPick(inputEl.id, items[i].getAttribute('data-suggest-value'));
    } else if (ev.key === 'Escape') {
      _closeSuggest(dropdownId);
    }
  };

  // --- Linked workspaces -------------------------------------------------

  window.ndRenumberLinkedWorkspaceRows = function (scope) {
    const root = scope || document;
    root.querySelectorAll('[data-linked-workspace-row]').forEach((row, idx) => {
      const title = row.querySelector('[data-linked-workspace-title]');
      if (title) title.textContent = 'Workspace ' + (idx + 1);
    });
  };

  window.ndRemoveLinkedWorkspaceRow = function (button) {
    const row = button && button.closest('[data-linked-workspace-row]');
    if (!row) return;
    const scope = row.closest('[data-ticket-form]') || document;
    row.remove();
    window.ndRenumberLinkedWorkspaceRows(scope);
  };

  function _primaryWorktreeName(scope) {
    const el = (scope || document).querySelector('input[name="worktree_name"]');
    return el ? el.value : '';
  }

  window.ndScheduleLinkedWorkspacePreview = function (row, delay = 250) {
    if (!row) return;
    clearTimeout(row.__ndLinkedPreviewTimer);
    row.__ndLinkedPreviewTimer = setTimeout(
      () => window.ndUpdateLinkedWorkspacePreview(row), delay,
    );
  };

  window.ndSetLinkedWorkspacePreview = function (row, pathText, sourceText = '') {
    if (!row) return;
    const preview = row.querySelector('[data-linked-worktree-preview]');
    if (!preview) return;
    const source = preview.querySelector('[data-worktree-preview-source]');
    const path = preview.querySelector('[data-worktree-preview-path]');
    if (source) source.textContent = sourceText ? `(${sourceText})` : '';
    if (path) path.textContent = pathText;
  };

  window.ndUpdateLinkedWorkspacePreview = async function (row) {
    if (!row) return;
    const preview = row.querySelector('[data-linked-worktree-preview]');
    if (!preview) return;
    const kind = (row.querySelector('select[name="linked_workspace_kind"]') || {}).value || 'directory';
    if (kind !== 'git_worktree') {
      window.ndSetLinkedWorkspacePreview(row, 'Switch kind to git worktree to preview.');
      return;
    }
    const source = (row.querySelector('input[name="linked_workspace_path"]') || {}).value || '';
    const override = (row.querySelector('input[name="linked_workspace_path_override"]') || {}).value || '';
    const scope = row.closest('[data-ticket-form]') || document;
    const name = _primaryWorktreeName(scope);
    if (!source && !override) {
      window.ndSetLinkedWorkspacePreview(row, 'Choose a linked path or custom path to preview.');
      return;
    }
    try {
      const r = await fetch('/board/worktree-preview?' + new URLSearchParams({ cwd: source, name, path: override, format: 'json' }));
      if (!r.ok) return;
      const data = await r.json();
      window.ndSetLinkedWorkspacePreview(row, data.path, data.source);
    } catch (e) {
      window.ndSetLinkedWorkspacePreview(row, 'Preview unavailable.');
    }
  };

  window.ndSyncLinkedWorkspaceRow = function (row) {
    if (!row) return;
    const kind = (row.querySelector('select[name="linked_workspace_kind"]') || {}).value || 'directory';
    const isGit = kind === 'git_worktree';
    row.querySelectorAll('[data-linked-git-fields]').forEach((el) => {
      el.classList.toggle('hidden', !isGit);
      el.classList.toggle('flex', isGit && el.hasAttribute('data-linked-git-inline'));
    });
    const accessField = row.querySelector('[data-linked-access-field]');
    if (accessField) accessField.classList.toggle('hidden', isGit);
    const accessSelect = row.querySelector('select[name="linked_workspace_access"]');
    if (accessSelect && isGit) accessSelect.value = 'read_write';
    window.ndScheduleLinkedWorkspacePreview(row, 0);
  };

  window.ndAddLinkedWorkspaceRow = function (hostId) {
    const host = document.getElementById(hostId || 'linked-workspaces');
    if (!host) return;
    const scope = host.closest('[data-ticket-form]') || document;
    const baseId = host.id || 'linked-workspaces';
    const cur = linkedSeqByHost.get(baseId) || host.querySelectorAll('[data-linked-workspace-row]').length;
    const idx = cur;
    linkedSeqByHost.set(baseId, cur + 1);
    const id = baseId + '-row-' + idx;
    const row = document.createElement('div');
    row.className = 'rounded-md border border-border bg-bg-elev-2 p-2 space-y-2';
    row.setAttribute('data-linked-workspace-row', '');
    row.innerHTML = `
      <div class="flex items-center justify-between gap-2">
        <span class="text-xs font-medium text-fg" data-linked-workspace-title>Workspace ${idx + 1}</span>
        <button
          type="button"
          onclick="window.ndRemoveLinkedWorkspaceRow(this);"
          class="rounded px-2 py-0.5 text-xs text-fg-muted hover:bg-bg hover:text-danger"
          title="Remove linked workspace">&times;</button>
      </div>
      <div class="relative">
        <input
          id="${id}"
          name="linked_workspace_path"
          autocomplete="off"
          spellcheck="false"
          placeholder="/home/you/other-repo"
          oninput="window.ndPathSuggest(this, '${id}-suggest'); window.ndScheduleLinkedWorkspacePreview(this.closest('[data-linked-workspace-row]'));"
          onfocus="window.ndPathSuggest(this, '${id}-suggest')"
          onblur="window.ndPathSuggestClose('${id}-suggest')"
          onkeydown="window.ndPathSuggestKey(event, this, '${id}-suggest')"
          class="w-full rounded border border-border bg-bg px-2 py-1.5 text-xs font-mono text-fg placeholder:text-fg-muted focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/30"
        />
        <div id="${id}-suggest" class="nd-suggest-host fixed z-50 hidden"></div>
      </div>
      <div class="grid grid-cols-2 gap-2">
        <label class="flex flex-col gap-1">
          <span class="text-fg-muted text-[11px] uppercase tracking-wide">Kind</span>
          <select name="linked_workspace_kind" onchange="window.ndSyncLinkedWorkspaceRow(this.closest('[data-linked-workspace-row]'));" class="rounded border border-border bg-bg px-2 py-1.5 text-xs text-fg focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/30">
            <option value="directory">Directory</option>
            <option value="git_worktree">Git worktree</option>
          </select>
        </label>
        <label class="flex flex-col gap-1" data-linked-access-field>
          <span class="text-fg-muted text-[11px] uppercase tracking-wide">Access</span>
          <select name="linked_workspace_access" class="rounded border border-border bg-bg px-2 py-1.5 text-xs text-fg focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/30">
            <option value="read_only" selected>Read-only</option>
            <option value="read_write">Read/write</option>
          </select>
        </label>
        <div class="hidden flex-col gap-1" data-linked-git-fields data-linked-git-inline>
          <span class="text-fg-muted text-[11px] uppercase tracking-wide">Git worktree</span>
          <div class="rounded border border-border bg-bg px-2 py-1.5 text-xs text-fg-muted">
            Linked git worktrees use the primary worktree name.
          </div>
        </div>
      </div>
      <div class="hidden space-y-2" data-linked-git-fields>
        <label class="flex flex-col gap-1">
          <span class="text-fg-muted text-[11px] uppercase tracking-wide">Custom worktree path</span>
          <div class="relative">
            <input
              id="${id}-path-override"
              name="linked_workspace_path_override"
              autocomplete="off"
              spellcheck="false"
              placeholder="optional exact absolute path"
              oninput="window.ndPathSuggest(this, '${id}-path-override-suggest'); window.ndScheduleLinkedWorkspacePreview(this.closest('[data-linked-workspace-row]'));"
              onfocus="window.ndPathSuggest(this, '${id}-path-override-suggest')"
              onblur="window.ndPathSuggestClose('${id}-path-override-suggest')"
              onkeydown="window.ndPathSuggestKey(event, this, '${id}-path-override-suggest')"
              class="w-full rounded border border-border bg-bg px-2 py-1.5 text-xs font-mono text-fg placeholder:text-fg-muted focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/30"
            />
            <div id="${id}-path-override-suggest" class="nd-suggest-host fixed z-50 hidden"></div>
          </div>
        </label>
        <div class="rounded border border-border bg-bg px-2 py-1.5 text-xs" data-linked-worktree-preview>
          <div class="mb-0.5 text-[11px] uppercase tracking-wide text-fg-muted">Worktree Path Preview <span data-worktree-preview-source></span></div>
          <code class="block break-all font-mono text-accent" data-worktree-preview-path>Switch kind to git worktree to preview.</code>
        </div>
      </div>`;
    host.appendChild(row);
    window.ndRenumberLinkedWorkspaceRows(scope);
    window.ndSyncLinkedWorkspaceRow(row);
    row.querySelector('input').focus();
  };

  // --- Init / wire-up ----------------------------------------------------
  //
  // Run once on page load AND whenever a fresh ticket form mounts (e.g. a
  // dialog opens via showModal()). The MutationObserver handles dynamic
  // inserts so callers don't have to remember to invoke init manually.

  function _initForm(scope) {
    const root = scope || document;
    const cwdInput = root.querySelector('[data-cwd-input]');
    if (cwdInput && !cwdInput.__ndCwdBound) {
      cwdInput.__ndCwdBound = true;
      cwdInput.addEventListener('input', () => window.ndScheduleWorktreePreview(250, root));
    }
    const primaryNameInput = root.querySelector('input[name="worktree_name"]');
    if (primaryNameInput && !primaryNameInput.__ndNameBound) {
      primaryNameInput.__ndNameBound = true;
      primaryNameInput.addEventListener('input', () => {
        root.querySelectorAll('[data-linked-workspace-row]').forEach((row) => {
          window.ndScheduleLinkedWorkspacePreview(row);
        });
      });
    }
    // Make sure the primary kind select's initial value drives the git
    // fields visibility + hidden use_worktree checkbox. Server templates
    // the select's `selected` option but DOM toggling is on us.
    const primaryKindSelect = root.querySelector('select[name="primary_kind"]');
    if (primaryKindSelect) {
      window.ndSyncPrimaryWorkspaceKind(primaryKindSelect);
    }
    root.querySelectorAll('[data-linked-workspace-row]').forEach(window.ndSyncLinkedWorkspaceRow);
    window.ndSyncWorktreeFields(root);
    window.ndScheduleWorktreePreview(0, root);
  }

  window.ndInitTicketForm = _initForm;

  function _initAllForms() {
    document.querySelectorAll('[data-ticket-form]').forEach((form) => _initForm(form));
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _initAllForms);
  } else {
    _initAllForms();
  }
})();
