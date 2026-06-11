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
  function _isCreateForm(root) {
    return !!(root && root.getAttribute('data-ticket-mode') === 'create');
  }

  function _workspaceDirty(root) {
    return !!(root && root.__ndWorkspaceDirty);
  }

  function _setWorkspaceDirty(root) {
    if (!root || root.__ndApplyingProjectDefaults) return;
    root.__ndWorkspaceDirty = true;
  }

  function _projectDefaults(root) {
    if (!root) return {};
    if (root.__ndProjectDefaultsCache) return root.__ndProjectDefaultsCache;
    const node = root.querySelector('[data-project-workspace-defaults]');
    if (!node) return {};
    try {
      root.__ndProjectDefaultsCache = JSON.parse(node.textContent || '{}') || {};
    } catch (_) {
      root.__ndProjectDefaultsCache = {};
    }
    return root.__ndProjectDefaultsCache;
  }

  function _slugify(value) {
    return String(value || '')
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '') || 'ticket';
  }

  function _derivedWorktreeName(root, project) {
    if (!project || !project.default_worktree_name_template) return '';
    const titleInput = root.querySelector('[data-title-input]');
    const title = titleInput ? titleInput.value : '';
    return project.default_worktree_name_template.replaceAll('{slug}', _slugify(title));
  }

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
  function _appendLinkedWorkspaceRow(host, data = {}) {
    if (!host) return null;
    const scope = host.closest('[data-ticket-form]') || document;
    const baseId = host.id || 'linked-workspaces';
    const cur = linkedSeqByHost.get(baseId) || host.querySelectorAll('[data-linked-workspace-row]').length;
    const idx = cur;
    linkedSeqByHost.set(baseId, cur + 1);
    const id = baseId + '-row-' + idx;
    const row = document.createElement('div');
    row.className = 'rounded-md border border-border bg-bg-elev-2 p-2 space-y-2';
    row.setAttribute('data-linked-workspace-row', '');
    const kind = data.kind || 'directory';
    const access = kind === 'git_worktree' ? 'read_write' : (data.access || 'read_only');
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
          value="${data.source_path || ''}"
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
            <option value="directory"${kind === 'directory' ? ' selected' : ''}>Directory</option>
            <option value="git_worktree"${kind === 'git_worktree' ? ' selected' : ''}>Git worktree</option>
          </select>
        </label>
        <label class="flex flex-col gap-1" data-linked-access-field>
          <span class="text-fg-muted text-[11px] uppercase tracking-wide">Access</span>
          <select name="linked_workspace_access" class="rounded border border-border bg-bg px-2 py-1.5 text-xs text-fg focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/30">
            <option value="read_only"${access === 'read_only' ? ' selected' : ''}>Read-only</option>
            <option value="read_write"${access === 'read_write' ? ' selected' : ''}>Read/write</option>
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
              value="${data.worktree_path || ''}"
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
    return row;
  }

  function _setLinkedWorkspaceRows(root, workspaces) {
    const host = root && root.querySelector('[id$="-linked"]');
    if (!host) return;
    host.innerHTML = '';
    linkedSeqByHost.set(host.id || 'linked-workspaces', 0);
    (workspaces || []).forEach((ws) => _appendLinkedWorkspaceRow(host, ws));
    window.ndRenumberLinkedWorkspaceRows(root);
  }

  // Sync toolchain-picker badge spans to reflect a new project's default_toolchains.
  // Called after a project change so the "Inherited" badge stays accurate without
  // a full page reload. Only the inherited-vs-none state is dynamic; explicit
  // enabled/disabled states come from the checkbox values and don't change.
  function _syncToolchainBadges(root, projectDefaults) {
    if (!root) return;
    const inherited = new Set(Array.isArray(projectDefaults) ? projectDefaults : []);
    root.querySelectorAll('[data-toolchain-row]').forEach((row) => {
      const enableInput = row.querySelector('[data-toolchain-enable]');
      const disableInput = row.querySelector('[data-toolchain-disable]');
      if (!enableInput) return;
      const name = enableInput.value;
      const isInherited = inherited.has(name);
      const isDisabled = !!(disableInput && disableInput.checked);
      const isExplicit = enableInput.checked && !isInherited;
      // Determine the desired badge state.
      let wantState;
      if (isInherited && !isDisabled) {
        wantState = 'inherited';
      } else if (isExplicit) {
        wantState = 'enabled';
      } else if (isDisabled) {
        wantState = 'disabled';
      } else {
        wantState = 'none';
      }
      // Find existing badge (server-rendered or previously injected).
      const existing = row.querySelector('[data-toolchain-badge]');
      const currentState = existing ? (existing.getAttribute('data-toolchain-badge') || 'none') : 'none';
      if (currentState === wantState) return;
      // Remove stale badge.
      if (existing) existing.remove();
      if (wantState === 'none') return;
      // Create replacement badge.
      const badge = document.createElement('span');
      badge.setAttribute('data-toolchain-badge', wantState);
      badge.className = (
        wantState === 'inherited'
          ? 'rounded border border-accent/40 bg-accent/10 px-1.5 py-0.5 text-[10px] font-medium text-accent'
          : wantState === 'enabled'
          ? 'rounded border border-warn/40 bg-warn/10 px-1.5 py-0.5 text-[10px] font-medium text-warn'
          : 'rounded border border-danger/40 bg-danger/10 px-1.5 py-0.5 text-[10px] font-medium text-danger'
      );
      badge.setAttribute('data-tooltip',
        wantState === 'inherited' ? 'Inherited from project — active unless disabled'
          : wantState === 'enabled' ? 'Explicitly enabled on this ticket'
          : 'Disabled on this ticket — opts out of project default'
      );
      badge.textContent = (
        wantState === 'inherited' ? 'Inherited'
          : wantState === 'enabled' ? 'Enabled'
          : 'Disabled'
      );
      // Insert before the Enable label so badge appears in the same visual slot.
      const enableLabel = enableInput.closest('label');
      if (enableLabel) {
        row.insertBefore(badge, enableLabel);
      } else {
        row.appendChild(badge);
      }
    });
  }

  function _applyProjectWorkspaceDefaults(root, projectId, force = false) {
    if (!root) return;
    // Auto-apply (force=false) is create-only and respects the dirty guard so
    // user-edited rows aren't clobbered as the project select / title change.
    // The explicit "reset to defaults" button passes force=true and bypasses
    // both gates so it works on edit too.
    if (!force && (!_isCreateForm(root) || _workspaceDirty(root))) return;
    const defaults = _projectDefaults(root);
    const project = projectId ? defaults[projectId] : null;
    root.__ndApplyingProjectDefaults = true;
    const sourcePathInput = root.querySelector('[data-source-path-input]');
    if (sourcePathInput) sourcePathInput.value = project && project.source_path ? project.source_path : '';
    const primaryKindSelect = root.querySelector('select[name="primary_kind"]');
    if (primaryKindSelect) {
      primaryKindSelect.value = (project && project.default_workspace_mode) ? project.default_workspace_mode : 'directory';
      window.ndSyncPrimaryWorkspaceKind(primaryKindSelect);
    }
    const worktreeNameInput = root.querySelector('input[name="worktree_name"]');
    if (worktreeNameInput) worktreeNameInput.value = _derivedWorktreeName(root, project);
    const baseRefInput = root.querySelector('[data-base-ref-input]');
    if (baseRefInput) baseRefInput.value = project && project.default_base_ref ? project.default_base_ref : '';
    _setLinkedWorkspaceRows(root, project && project.default_linked_workspaces ? project.default_linked_workspaces : []);
    // Refresh toolchain-picker badge states to reflect the new project's defaults.
    _syncToolchainBadges(root, project ? (project.default_toolchains || []) : []);
    root.__ndApplyingProjectDefaults = false;
    window.ndSyncWorktreeFields(root);
    window.ndScheduleWorktreePreview(0, root);
  }

  window.ndProjectChanged = function (selectEl) {
    if (!selectEl) return;
    const root = selectEl.closest('[data-ticket-form]');
    if (!root) return;
    _applyProjectWorkspaceDefaults(root, selectEl.value || '');
  };

  window.ndResetProjectWorkspaceDefaults = function (button) {
    if (!button) return;
    const root = button.closest('[data-ticket-form]');
    if (!root) return;
    const projectSelect = root.querySelector('[data-project-select]');
    root.__ndWorkspaceDirty = false;
    _applyProjectWorkspaceDefaults(root, projectSelect ? (projectSelect.value || '') : '', true);
  };

  // Edit-mode entry point: pop the styled ndConfirm before resetting so the
  // user can back out without losing custom workspace rows. Create mode skips
  // the prompt and uses ndResetProjectWorkspaceDefaults directly.
  window.ndConfirmResetProjectWorkspaceDefaults = function (button) {
    if (!button) return;
    const apply = function () { window.ndResetProjectWorkspaceDefaults(button); };
    if (typeof window.ndConfirm !== 'function') {
      apply();
      return;
    }
    window.ndConfirm({
      title: 'Reset workspaces to project defaults',
      message: 'Replace the workspaces on this ticket with the project defaults? Customized workspace rows will be overwritten.',
      okLabel: 'Reset',
      cancelLabel: 'Keep current',
      danger: true,
    }).then(function (ok) {
      if (ok) apply();
    });
  };

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
      if (window.ndSetBaseRefNote) window.ndSetBaseRefNote(root, '', null);
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

  // --- Effective execution-context preview -------------------------------
  //
  // Debounced refresh of the shared effective-config partial inside the
  // ticket create/edit modal. The actual request + swap is htmx's job (the
  // [data-effective-preview] element carries hx-post / hx-include / hx-swap);
  // we only fire the nd-config-refresh trigger so the panel reflects the
  // latest project / profile / workspace / toolchain selection.
  window.ndScheduleEffectiveConfigPreview = function (delay = 350, scope) {
    const root = (scope && scope.closest && scope.closest('[data-ticket-form]'))
      || (scope || document);
    clearTimeout(root.__ndEffConfigTimer);
    root.__ndEffConfigTimer = setTimeout(() => {
      const target = (root.querySelector && root.querySelector('[data-effective-preview]'))
        || document.querySelector('[data-effective-preview]');
      if (target && window.htmx) window.htmx.trigger(target, 'nd-config-refresh');
    }, delay);
  };

  // Fields whose changes should NOT refresh the execution-context preview
  // (they don't affect the merged run context). Everything else does.
  function _cfgIrrelevant(target) {
    if (!target) return true;
    const name = target.name || '';
    if (name === 'title' || name === 'prompt') return true;
    // Priority and label fields are purely organisational — they don't affect
    // the sandbox, toolchain, or permission resolution.
    if (name === 'priority') return true;
    if (name === 'label_ids' || name === 'labels_form') return true;
    // Dependency search box and its hidden depends_on_id rows don't change
    // the execution context either.
    if (target.matches && target.matches(
      '[data-dep-search], [name="depends_on_id"], [name="deps_form"]'
    )) return true;
    return false;
  }

  window.ndSetWorktreePreview = function (pathText, sourceText = '', scope) {
    const root = scope || document;
    const preview = root.querySelector('[data-worktree-path-preview]');
    if (!preview) return;
    const source = preview.querySelector('[data-worktree-preview-source]');
    const path = preview.querySelector('[data-worktree-preview-path]');
    if (source) source.textContent = sourceText ? `(${sourceText})` : '';
    if (path) path.textContent = pathText;
  };

  // Surface which ref the worktree branch will start from, and loudly flag
  // the "branch is gone" case (ref doesn't resolve in the repo) so the user
  // fixes it before the run fails at `git worktree add`.
  window.ndSetBaseRefNote = function (root, ref, status) {
    const note = (root || document).querySelector('[data-base-ref-note]');
    if (!note) return;
    const r = (ref || '').trim();
    if (!r) {
      note.textContent = '';
      note.className = 'mt-1 text-[11px]';
      return;
    }
    if (status === 'missing') {
      note.textContent = 'Base ref "' + r + '" not found in this repo — the worktree will fail to create. Check the branch/ref name.';
      note.className = 'mt-1 text-[11px] text-warn';
    } else {
      note.textContent = 'Branch will start from ' + r + '.';
      note.className = 'mt-1 text-[11px] text-fg-muted';
    }
  };

  window.ndUpdateWorktreePreview = async function (scope) {
    const root = scope || document;
    if (!_worktreeEnabled(root)) {
      window.ndSetWorktreePreview('Enable worktree to preview the target path.', '', root);
      window.ndSetBaseRefNote(root, '', null);
      return;
    }
    const sourcePath = (root.querySelector('[data-source-path-input]') || {}).value || '';
    const name = (root.querySelector('input[name="worktree_name"]') || {}).value || '';
    const path = (root.querySelector('[data-worktree-path-input]') || {}).value || '';
    const baseRef = (root.querySelector('[data-base-ref-input]') || {}).value || '';
    if (!sourcePath && !path) {
      window.ndSetWorktreePreview('Choose a source path or custom path to preview the target.', '', root);
      window.ndSetBaseRefNote(root, '', null);
      return;
    }
    const mySeq = ++previewSeq;
    try {
      const r = await fetch('/board/worktree-preview?' + new URLSearchParams({ source_path: sourcePath, name, path, base_ref: baseRef, format: 'json' }));
      if (!r.ok || mySeq !== previewSeq) return;
      const data = await r.json();
      window.ndSetWorktreePreview(data.path, data.source, root);
      window.ndSetBaseRefNote(root, data.base_ref || baseRef, data.base_ref_status);
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
      // Shift+Tab: keep native focus movement.
      if (ev.shiftKey) return;
      // Tab with suggestions visible: shell-style completion — commit the
      // highlighted item (or the first if none highlighted). When the list is
      // closed/empty fall through so Tab moves focus normally.
      if (!items.length) return;
      ev.preventDefault();
      const tabI = cur >= 0 ? cur : 0;
      window.ndPathSuggestPick(inputEl.id, items[tabI].getAttribute('data-suggest-value'));
    } else if (ev.key === 'Enter') {
      // Always preventDefault so a stray Enter never submits the enclosing
      // form. Only commit a suggestion when the list has items.
      ev.preventDefault();
      if (!items.length) return;
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
    _setWorkspaceDirty(scope);
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
      const r = await fetch('/board/worktree-preview?' + new URLSearchParams({ source_path: source, name, path: override, format: 'json' }));
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
    _appendLinkedWorkspaceRow(host);
    _setWorkspaceDirty(scope);
  };
  function _bindWorkspaceDirtyTracking(root) {
    if (!root || root.__ndWorkspaceDirtyBound) return;
    root.__ndWorkspaceDirtyBound = true;
    const matchesWorkspaceField = (target) => (
      target.matches('[data-source-path-input], input[name="worktree_name"], [data-base-ref-input], [data-worktree-path-input], input[name="linked_workspace_path"], input[name="linked_workspace_path_override"]')
      || target.matches('select[name="primary_kind"], select[name="linked_workspace_kind"], select[name="linked_workspace_access"]')
    );
    root.addEventListener('input', (event) => {
      const target = event.target;
      if (target instanceof Element && _isCreateForm(root) && matchesWorkspaceField(target)) {
        _setWorkspaceDirty(root);
      }
    });
    root.addEventListener('change', (event) => {
      const target = event.target;
      if (target instanceof Element && _isCreateForm(root) && matchesWorkspaceField(target)) {
        _setWorkspaceDirty(root);
      }
    });
  }


  // --- Dependency picker -------------------------------------------------
  //
  // Search-and-add control for ticket dependencies (depth 1). Candidates are
  // embedded as a JSON <script> in the picker; selecting one appends a
  // removable row carrying a hidden depends_on_id input. Mirrors the linked-
  // workspace add/remove UX. DOM-built (no HTML strings) so arbitrary ticket
  // titles can't break the markup.

  function _depPicker(el) { return el && el.closest('[data-dep-picker]'); }

  function _depCandidates(picker) {
    const tag = picker.querySelector('script[data-dep-candidates]');
    if (!tag) return [];
    try { return JSON.parse(tag.textContent || '[]'); } catch (e) { return []; }
  }

  function _depSelectedIds(picker) {
    return Array.from(picker.querySelectorAll('[data-dep-row]'))
      .map((r) => r.getAttribute('data-dep-id'));
  }

  function _depUpdateEmpty(picker) {
    const empty = picker.querySelector('[data-dep-empty]');
    if (empty) empty.classList.toggle('hidden', picker.querySelectorAll('[data-dep-row]').length > 0);
  }

  function _depStatusClass(status) {
    return (status === 'review' || status === 'archived')
      ? 'border-accent/40 text-accent' : 'border-warn/40 text-warn';
  }

  function _depAdd(picker, id, title, status) {
    if (!id || _depSelectedIds(picker).includes(id)) return;
    const li = document.createElement('li');
    li.setAttribute('data-dep-row', '');
    li.setAttribute('data-dep-id', id);
    li.className = 'flex items-center gap-2 rounded border border-border bg-bg px-2 py-1 text-xs';

    const hidden = document.createElement('input');
    hidden.type = 'hidden'; hidden.name = 'depends_on_id'; hidden.value = id;

    const st = document.createElement('span');
    st.className = 'shrink-0 rounded px-1.5 py-0.5 border ' + _depStatusClass(status);
    st.textContent = status || '';

    const link = document.createElement('a');
    link.href = '/tickets/' + encodeURIComponent(id);
    link.target = '_blank';
    link.className = 'flex-1 truncate text-fg hover:text-accent';
    link.title = title || id;
    link.textContent = title || id;

    const rm = document.createElement('button');
    rm.type = 'button';
    rm.className = 'shrink-0 rounded px-1.5 py-0.5 text-fg-muted hover:bg-bg-elev hover:text-danger';
    rm.title = 'Remove dependency';
    rm.textContent = '×';
    rm.addEventListener('click', () => window.ndDepRemove(rm));

    li.append(hidden, st, link, rm);
    picker.querySelector('[data-dep-selected]').appendChild(li);
    _depUpdateEmpty(picker);
  }

  window.ndDepSearch = function (input) {
    const picker = _depPicker(input);
    if (!picker) return;
    const dd = picker.querySelector('[data-dep-suggest]');
    const q = (input.value || '').trim().toLowerCase();
    const selected = new Set(_depSelectedIds(picker));
    let matches = _depCandidates(picker).filter((c) => !selected.has(c.id));
    if (q) {
      matches = matches.filter((c) =>
        (c.title || '').toLowerCase().includes(q) || (c.status || '').toLowerCase().includes(q));
    }
    matches = matches.slice(0, 8);
    dd.replaceChildren();
    if (!matches.length) { dd.classList.add('hidden'); return; }
    matches.forEach((c) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'block w-full cursor-pointer text-left px-2 py-1.5 text-xs hover:bg-bg-elev-2';
      const t = document.createElement('span'); t.className = 'text-fg'; t.textContent = c.title || c.id;
      const s = document.createElement('span'); s.className = 'text-fg-muted'; s.textContent = ' · ' + (c.status || '');
      btn.append(t, s);
      // mousedown (not click) so it fires before the input's blur-close.
      btn.addEventListener('mousedown', (e) => {
        e.preventDefault();
        _depAdd(picker, c.id, c.title, c.status);
        input.value = '';
        dd.replaceChildren();
        dd.classList.add('hidden');
        input.focus();
      });
      dd.appendChild(btn);
    });
    dd.classList.remove('hidden');
  };

  window.ndDepSearchKey = function (ev, input) {
    const picker = _depPicker(input);
    const dd = picker && picker.querySelector('[data-dep-suggest]');
    if (!dd) return;
    if (ev.key === 'Escape') {
      dd.classList.add('hidden');
    } else if (ev.key === 'Enter') {
      const first = dd.querySelector('button');
      if (first && !dd.classList.contains('hidden')) {
        ev.preventDefault();
        first.dispatchEvent(new MouseEvent('mousedown'));
      }
    }
  };

  window.ndDepSearchClose = function (input) {
    const picker = _depPicker(input);
    const dd = picker && picker.querySelector('[data-dep-suggest]');
    // Delay so a mousedown on a suggestion beats the blur-driven close.
    if (dd) setTimeout(() => dd.classList.add('hidden'), 180);
  };

  window.ndDepRemove = function (button) {
    const picker = _depPicker(button);
    const row = button && button.closest('[data-dep-row]');
    if (row) row.remove();
    if (picker) _depUpdateEmpty(picker);
  };

  // --- Init / wire-up ----------------------------------------------------
  //
  // Run once on page load AND whenever a fresh ticket form mounts (e.g. a
  // dialog opens via showModal()). The MutationObserver handles dynamic
  // inserts so callers don't have to remember to invoke init manually.

  function _initForm(scope) {
    const root = scope || document;
    const sourcePathInput = root.querySelector('[data-source-path-input]');
    if (sourcePathInput && !sourcePathInput.__ndSourcePathBound) {
      sourcePathInput.__ndSourcePathBound = true;
      sourcePathInput.addEventListener('input', () => window.ndScheduleWorktreePreview(250, root));
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
    const titleInput = root.querySelector('[data-title-input]');
    const projectSelect = root.querySelector('[data-project-select]');
    if (titleInput && !titleInput.__ndProjectTitleBound) {
      titleInput.__ndProjectTitleBound = true;
      titleInput.addEventListener('input', () => {
        if (projectSelect && !_workspaceDirty(root)) {
          _applyProjectWorkspaceDefaults(root, projectSelect.value || '');
        }
      });
    }
    // Make sure the primary kind select's initial value drives the git
    // fields visibility + hidden use_worktree checkbox. Server templates
    // the select's `selected` option but DOM toggling is on us.
    const primaryKindSelect = root.querySelector('select[name="primary_kind"]');
    if (primaryKindSelect) {
      window.ndSyncPrimaryWorkspaceKind(primaryKindSelect);
    }
    _bindWorkspaceDirtyTracking(root);
    root.querySelectorAll('[data-linked-workspace-row]').forEach(window.ndSyncLinkedWorkspaceRow);
    if (projectSelect && !_workspaceDirty(root)) {
      _applyProjectWorkspaceDefaults(root, projectSelect.value || '');
    } else {
      window.ndSyncWorktreeFields(root);
      window.ndScheduleWorktreePreview(0, root);
    }

    // Execution-context preview: refresh when any config-bearing field
    // changes. A single delegated listener (bound once) covers fields that
    // exist now and ones added later (linked workspaces, extra paths).
    const previewHost = root.querySelector('[data-effective-preview]');
    if (previewHost && !root.__ndEffConfigBound) {
      root.__ndEffConfigBound = true;
      const onChange = (e) => {
        if (_cfgIrrelevant(e.target)) return;
        window.ndScheduleEffectiveConfigPreview(400, root);
      };
      root.addEventListener('change', onChange);
      root.addEventListener('input', onChange);
      // Initial render once the form has settled (project defaults applied).
      window.ndScheduleEffectiveConfigPreview(120, root);
    }
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
