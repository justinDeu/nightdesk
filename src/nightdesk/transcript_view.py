"""Pure helpers used by the server-side transcript renderer.

These helpers turn canonical events into the small structured shapes the
Jinja templates render: diff rows for Edit/MultiEdit, head+tail elision for
long Bash output, fenced-code segmentation for assistant_text, and one-line
summary metadata for tool_use cards.

The JS live-tail in ``templates/partials/transcript_panel.html`` mirrors the
same logic. Keep the two sides in sync — both should produce identical DOM
for the same canonical event, using the CSS classes declared in
``static/app.css`` (.tc-card / .diff / .bash-cmd / .tool-result /
.thinking-block / .assistant-text).
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import NamedTuple


# ---------------------------------------------------------------------------
# Edit / MultiEdit: unified single-column diff with +/- gutter + line numbers.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiffRow:
    kind: str            # 'ctx' | 'del' | 'ins'
    gutter: str          # ' ' | '-' | '+'
    line_no: str         # display string, e.g. '12' or ''
    text: str            # the source line (no trailing newline)


class DiffCounts(NamedTuple):
    """Aggregate line-change counts for a single diff.

    NamedTuple so callers can index ``(ins, dels)`` like a plain tuple while
    Jinja templates use the attribute form (``c.ins`` / ``c.dels``).
    """
    ins: int
    dels: int


def diff_counts(old: str, new: str) -> DiffCounts:
    """Return (ins, dels) line counts for the diff between ``old`` and ``new``.

    Mirrors :func:`unified_diff_rows` so the totals match exactly what's
    rendered: ``insert`` and ``delete`` opcodes contribute one side; the
    ``replace`` opcode contributes to both. Equal/context rows do not count.
    """
    old_lines = old.splitlines() if old else []
    new_lines = new.splitlines() if new else []
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    ins = 0
    dels = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "delete":
            dels += i2 - i1
        elif tag == "insert":
            ins += j2 - j1
        elif tag == "replace":
            dels += i2 - i1
            ins += j2 - j1
    return DiffCounts(ins, dels)


def unified_diff_rows(old: str, new: str) -> list[DiffRow]:
    """Return per-line diff rows aligned to old/new line numbers.

    Context rows show the new-side line number; deletes show the old-side
    line number; inserts show the new-side line number. Trailing newlines on
    the inputs are ignored so the row count matches what the user sees.
    """
    old_lines = old.splitlines() if old else []
    new_lines = new.splitlines() if new else []
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    rows: list[DiffRow] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                rows.append(DiffRow("ctx", " ", str(j1 + k + 1), new_lines[j1 + k]))
        elif tag == "delete":
            for k in range(i2 - i1):
                rows.append(DiffRow("del", "-", str(i1 + k + 1), old_lines[i1 + k]))
        elif tag == "insert":
            for k in range(j2 - j1):
                rows.append(DiffRow("ins", "+", str(j1 + k + 1), new_lines[j1 + k]))
        elif tag == "replace":
            for k in range(i2 - i1):
                rows.append(DiffRow("del", "-", str(i1 + k + 1), old_lines[i1 + k]))
            for k in range(j2 - j1):
                rows.append(DiffRow("ins", "+", str(j1 + k + 1), new_lines[j1 + k]))
    return rows


# ---------------------------------------------------------------------------
# Bash: head/tail elision for long output (>20 lines -> first 10 + last 5).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ElidedOutput:
    head: list[str]
    tail: list[str]
    hidden: int          # 0 means not elided
    total: int
    hidden_lines: list[str] = ()  # middle lines omitted from head/tail


def elide_output(text: str, head: int = 10, tail: int = 5, threshold: int = 20) -> ElidedOutput:
    """Return head/tail slices for long output.

    When ``len(lines) <= threshold`` everything goes in ``head`` and
    ``hidden`` is 0, so callers can render uniformly: always show head, show
    tail only when hidden > 0, gate hidden middle behind a ``<details>``.
    """
    lines = text.splitlines() if text else []
    total = len(lines)
    if total <= threshold:
        return ElidedOutput(head=lines, tail=[], hidden=0, total=total)
    return ElidedOutput(
        head=lines[:head], tail=lines[-tail:],
        hidden=total - head - tail, total=total,
        hidden_lines=lines[head:-tail],
    )


# ---------------------------------------------------------------------------
# assistant_text: split on ``` fenced code blocks.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TextSegment:
    kind: str            # 'text' | 'code'
    lang: str            # '' for text segments and unfenced code
    body: str


_FENCE_RE = re.compile(r"^```([A-Za-z0-9_+\-.]*)\s*$")


def assistant_segments(text: str) -> list[TextSegment]:
    """Split assistant text into alternating text and fenced-code segments.

    A fence is a line that is exactly ``` followed by an optional language
    tag. Unterminated fences (a starting ``` with no closing fence) flush
    the remaining lines as a code segment so nothing is dropped.
    """
    if not text:
        return [TextSegment("text", "", "")]
    out: list[TextSegment] = []
    buf: list[str] = []
    in_code = False
    lang = ""
    for line in text.splitlines():
        m = _FENCE_RE.match(line)
        if m:
            if in_code:
                out.append(TextSegment("code", lang, "\n".join(buf)))
                buf = []
                in_code = False
                lang = ""
            else:
                if buf:
                    out.append(TextSegment("text", "", "\n".join(buf)))
                buf = []
                in_code = True
                lang = m.group(1) or ""
            continue
        buf.append(line)
    if buf:
        out.append(TextSegment("code" if in_code else "text", lang, "\n".join(buf)))
    return out


# ---------------------------------------------------------------------------
# Tool-use summaries: one-liner metadata used in <summary>.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSummary:
    tag: str             # short label: 'Read', 'Edit', 'Bash', ...
    tag_class: str       # css color class suffix: 'read', 'edit', 'bash', ...
    primary: str         # main identifier: path / pattern / first command line
    meta: str            # extra muted bits: 'lines 1-50', '(3 edits)', flags


def tool_summary(evt: dict) -> ToolSummary:
    """Return the always-visible summary for a tool_use event."""
    tool = (evt.get("tool") or "").strip()
    input_ = evt.get("input") or {}
    if tool == "Read":
        path = input_.get("file_path") or input_.get("path") or "?"
        offset = input_.get("offset")
        limit = input_.get("limit")
        if offset is not None and limit is not None:
            meta = f"lines {offset}-{int(offset) + int(limit)}"
        elif offset is not None:
            meta = f"from line {offset}"
        else:
            meta = ""
        return ToolSummary("Read", "read", str(path), meta)
    if tool == "Write":
        path = input_.get("file_path") or input_.get("path") or "?"
        content = input_.get("content") or ""
        lc = len(content.splitlines()) if content else 0
        return ToolSummary("Write", "write", str(path), f"{lc} lines" if lc else "")
    if tool == "Edit":
        path = input_.get("file_path") or input_.get("path") or "?"
        return ToolSummary("Edit", "edit", str(path), "")
    if tool == "MultiEdit":
        path = input_.get("file_path") or input_.get("path") or "?"
        n = len(input_.get("edits") or [])
        return ToolSummary("MultiEdit", "multiedit", str(path), f"{n} edits")
    if tool == "Bash":
        cmd = (input_.get("command") or "").strip()
        first = cmd.splitlines()[0] if cmd else ""
        if len(first) > 80:
            first = first[:77] + "..."
        return ToolSummary("Bash", "bash", first, "")
    if tool == "Glob":
        pattern = input_.get("pattern") or ""
        path = input_.get("path") or "."
        return ToolSummary("Glob", "glob", str(pattern), f"in {path}" if path else "")
    if tool == "Grep":
        pattern = input_.get("pattern") or ""
        flags: list[str] = []
        if input_.get("-i"):
            flags.append("-i")
        if input_.get("-n"):
            flags.append("-n")
        if input_.get("-C") is not None:
            flags.append(f"-C{input_['-C']}")
        elif input_.get("-A") is not None or input_.get("-B") is not None:
            if input_.get("-A") is not None:
                flags.append(f"-A{input_['-A']}")
            if input_.get("-B") is not None:
                flags.append(f"-B{input_['-B']}")
        glob = input_.get("glob")
        ftype = input_.get("type")
        path = input_.get("path") or "."
        extras: list[str] = []
        if flags:
            extras.append(" ".join(flags))
        if glob:
            extras.append(f"glob={glob}")
        if ftype:
            extras.append(f"type={ftype}")
        extras.append(f"in {path}")
        return ToolSummary("Grep", "grep", str(pattern), " ".join(extras))
    if tool == "TaskCreate":
        subject = (input_.get("subject") or "").strip()
        return ToolSummary("TaskCreate", "task", subject or "new task", "")
    if tool == "TaskUpdate":
        tid = str(input_.get("taskId") or "").strip()
        status = (input_.get("status") or "").strip()
        primary = f"task #{tid}" if tid else "task"
        return ToolSummary("TaskUpdate", "task", primary, status)
    if tool in ("TaskList", "TaskGet", "TaskOutput", "TaskStop"):
        tid = str(input_.get("taskId") or "").strip()
        return ToolSummary(tool, "task", (f"task #{tid}" if tid else ""), "")
    return ToolSummary(tool or "tool", "generic", "", "")


# ---------------------------------------------------------------------------
# Sub-agent (Task tool) lifecycle cards.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SubagentSummary:
    """One-line + body metadata for a sub-agent (Task tool) card.

    ``label`` is the sub-agent type (e.g. ``Explore``); ``detail`` is the
    current activity (latest description or the tool it last ran). ``status``
    is the terminal disposition (``completed`` / ``failed``) once the
    notification phase arrives, empty while running.
    """
    label: str
    phase: str
    status: str
    detail: str
    tool_uses: int
    duration: str
    tokens: int
    summary: str
    done: bool
    failed: bool


def _fmt_duration_ms(ms) -> str:
    """Format a millisecond duration as a compact human string ('' if absent)."""
    try:
        ms_i = int(ms)
    except (TypeError, ValueError):
        return ""
    if ms_i <= 0:
        return ""
    sec = ms_i / 1000.0
    if sec < 60:
        return f"{sec:.1f}s"
    m = int(sec // 60)
    s = int(sec % 60)
    if m < 60:
        return f"{m}m {s}s"
    h = m // 60
    m = m % 60
    return f"{h}h {m}m {s}s"


def subagent_summary(evt: dict) -> SubagentSummary:
    """Return display metadata for a (collapsed) ``subagent`` event."""
    label = (evt.get("subagent_type") or "").strip() or "subagent"
    phase = (evt.get("phase") or "progress").strip()
    status = (evt.get("status") or "").strip()
    detail = (evt.get("description") or "").strip()
    if not detail:
        last = (evt.get("last_tool_name") or "").strip()
        if last:
            detail = f"running {last}"
    usage = evt.get("usage") or {}
    tool_uses = 0
    tokens = 0
    duration = ""
    if isinstance(usage, dict):
        try:
            tool_uses = int(usage.get("tool_uses") or 0)
        except (TypeError, ValueError):
            tool_uses = 0
        try:
            tokens = int(usage.get("total_tokens") or 0)
        except (TypeError, ValueError):
            tokens = 0
        duration = _fmt_duration_ms(usage.get("duration_ms"))
    done = phase == "notification"
    failed = status.lower() in {"failed", "error", "errored"}
    return SubagentSummary(
        label=label, phase=phase, status=status, detail=detail,
        tool_uses=tool_uses, duration=duration, tokens=tokens,
        summary=(evt.get("summary") or "").strip(),
        done=done, failed=failed,
    )


_SUBAGENT_MERGE_FIELDS = (
    "subagent_type", "task_type", "tool_use_id", "task_id", "description",
    "prompt", "last_tool_name", "status", "summary", "output_file", "usage",
    "phase", "raw",
)


def _merge_subagent(base: dict, evt: dict) -> None:
    """Fold a later sub-agent event's fields into the existing card dict.

    Latest non-empty value wins so the single card reflects the most advanced
    state (e.g. the terminal ``status``/``summary`` from the notification phase
    and the newest cumulative ``usage``).
    """
    for k in _SUBAGENT_MERGE_FIELDS:
        v = evt.get(k)
        if v not in (None, "", {}):
            base[k] = v


def subagent_index(events) -> list[dict]:
    """One row per sub-agent for the sidebar, folding lifecycle phases.

    Reuses the same per-task merge as the inline card so the sidebar and the
    card never disagree. Each row carries ``tool_use_id`` so a click can filter
    the main panel by ``parent_tool_use_id``. Rows preserve first-seen order.
    """
    cards: dict[str, dict] = {}
    order: list[str] = []
    # The dispatch description + prompt arrive on the ``started`` phase, then
    # ``progress`` phases overwrite ``description`` with the transient current
    # activity ("Reading X"). Capture the stable started-phase values so the
    # sidebar tooltip shows the same task description/prompt as the Agent tool
    # card, not whatever the sub-agent happened to be doing last.
    task_desc: dict[str, str] = {}
    task_prompt: dict[str, str] = {}
    for e in events:
        if e.get("type") != "subagent":
            continue
        key = e.get("task_id") or e.get("tool_use_id")
        if not key:
            continue
        if key not in cards:
            cards[key] = dict(e)
            order.append(key)
        else:
            _merge_subagent(cards[key], e)
        d = (e.get("description") or "").strip()
        if d and (e.get("phase") == "started" or key not in task_desc):
            task_desc[key] = d
        p = (e.get("prompt") or "").strip()
        if p and key not in task_prompt:
            task_prompt[key] = p
    rows: list[dict] = []
    for key in order:
        card = cards[key]
        s = subagent_summary(card)
        rows.append({
            "label": s.label,
            "status": s.status or ("done" if s.done else "run"),
            "tool_use_id": card.get("tool_use_id", ""),
            "task_id": card.get("task_id", ""),
            "tool_uses": s.tool_uses,
            "tokens": s.tokens,
            "duration": s.duration,
            "done": s.done,
            "failed": s.failed,
            "detail": s.detail,
            "description": task_desc.get(key, ""),
            "prompt": task_prompt.get(key, ""),
        })
    return rows


# ---------------------------------------------------------------------------
# Event pairing: bind each tool_result to its parent tool_use so the static
# renderer can place them inside the same card. Mirrored client-side by the
# live-tail in templates/partials/transcript_panel.html, which uses the
# ``data-tool-use-id`` attribute to find the parent card at append time.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PairedEvent:
    """One render unit: the event plus its paired tool_result, if any.

    ``paired_result`` is non-None only when ``event`` is a tool_use that has a
    matching tool_result later in the stream. A tool_result that has been
    paired is filtered out of the resulting list so the template loop renders
    it only once, inside its parent card.
    """
    event: dict
    paired_result: dict | None = None


@dataclass
class GroupedEvent:
    """A render unit that may own nested child tool calls (sub-agent card).

    ``children`` holds the tool events whose ``parent_tool_use_id`` matched this
    node's sub-agent ``tool_use_id``. Empty for non-subagent nodes.
    """
    event: dict
    paired_result: dict | None = None
    children: list["GroupedEvent"] = field(default_factory=list)


def group_by_subagent(paired: list[PairedEvent]) -> list[GroupedEvent]:
    """Nest child tool events under the sub-agent card that spawned them.

    A tool_use/tool_result whose ``parent_tool_use_id`` matches a subagent
    card's ``tool_use_id`` is moved into that card's ``children`` instead of
    rendering at top level. Events with no/unknown parent stay top-level, in
    their original order.
    """
    cards_by_tuid: dict[str, GroupedEvent] = {}
    out: list[GroupedEvent] = []
    for pe in paired:
        ev = pe.event
        if ev.get("type") == "subagent":
            node = GroupedEvent(event=ev, paired_result=None)
            tuid = ev.get("tool_use_id")
            if tuid:
                cards_by_tuid[tuid] = node
            out.append(node)
            continue
        ptid = ev.get("parent_tool_use_id")
        if ptid and ptid in cards_by_tuid:
            cards_by_tuid[ptid].children.append(
                GroupedEvent(event=ev, paired_result=pe.paired_result)
            )
            continue
        out.append(GroupedEvent(event=ev, paired_result=pe.paired_result))
    return out


def accumulate_stats(events) -> dict:
    """Sum stats events and count tool_use events from a canonical event list.

    Used by the server-side stats bar so initial page render matches what the
    client live-tail would show after replaying the same transcript. Mirrors
    the JS accumulator in ``transcript_panel.html``.

    Returns a dict with ``input_tokens``, ``output_tokens``, ``cache_read_tokens``,
    ``cache_write_tokens``, ``tool_count``, ``model`` (last seen), ``cost_usd``
    (final run-scoped value, if any).
    """
    out = {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_write_tokens": 0,
        "tool_count": 0, "model": None, "cost_usd": None,
        "last_seq": -1,
    }
    for e in events:
        seq = e.get("seq")
        if isinstance(seq, int) and seq > out["last_seq"]:
            out["last_seq"] = seq
        t = e.get("type")
        if t == "tool_use":
            out["tool_count"] += 1
            continue
        if t != "stats":
            continue
        scope = e.get("scope")
        if scope == "run":
            # Run-scope stats carry the cumulative totals; replace, don't sum.
            for k in ("input_tokens", "output_tokens",
                      "cache_read_tokens", "cache_write_tokens"):
                if e.get(k) is not None:
                    out[k] = int(e[k])
            if e.get("cost_usd") is not None:
                out["cost_usd"] = e["cost_usd"]
        else:
            # Per-turn stats accumulate.
            for k in ("input_tokens", "output_tokens",
                      "cache_read_tokens", "cache_write_tokens"):
                out[k] += int(e.get(k) or 0)
        if e.get("model"):
            out["model"] = e["model"]
    return out


# ---------------------------------------------------------------------------
# Task / TodoWrite: normalize either tool family into a unified todo list.
# ---------------------------------------------------------------------------


_TASK_TOOLS = {"TaskCreate", "TaskUpdate"}


def _todos_from_tasks(events) -> list[dict]:
    items: list[dict] = []          # in creation order
    by_id: dict[str, dict] = {}     # "1","2",... -> item
    for e in sorted(events, key=lambda x: x.get("seq", 0)):
        if e.get("type") != "tool_use":
            continue
        tool = e.get("tool")
        inp = e.get("input") or {}
        if tool == "TaskCreate":
            item = {
                "id": str(len(items) + 1),
                "label": (inp.get("subject") or "").strip(),
                "activeForm": (inp.get("activeForm") or "").strip(),
                "status": "pending",
            }
            items.append(item)
            by_id[item["id"]] = item
        elif tool == "TaskUpdate":
            tid = str(inp.get("taskId") or "")
            status = inp.get("status")
            item = by_id.get(tid)
            if item and status:
                item["status"] = status
    return [i for i in items if i["status"] != "deleted"]


def _todos_from_todowrite(events) -> list[dict]:
    last = None
    for e in sorted(events, key=lambda x: x.get("seq", 0)):
        if e.get("type") == "tool_use" and e.get("tool") == "TodoWrite":
            last = e
    if last is None:
        return []
    out: list[dict] = []
    for i, td in enumerate((last.get("input") or {}).get("todos") or []):
        out.append({
            "id": str(i + 1),
            "label": (td.get("content") or "").strip(),
            "activeForm": (td.get("activeForm") or "").strip(),
            "status": td.get("status") or "pending",
        })
    return out


def build_todo_list(events) -> list[dict]:
    """Normalized todo list from whichever task tool the run used.

    Task* and TodoWrite are mutually exclusive per run; prefer Task* when both
    somehow appear. Returns ``[]`` when neither is present.
    """
    events = list(events)
    has_task = any(e.get("type") == "tool_use" and e.get("tool") in _TASK_TOOLS
                   for e in events)
    if has_task:
        return _todos_from_tasks(events)
    return _todos_from_todowrite(events)


def result_duplicates_last_assistant(events) -> bool:
    """Return True when the final ``result`` summary echoes the last
    ``assistant_text``.

    The CC SDK's terminal ``result`` event usually carries the same text the
    model already produced as its final ``assistant_text`` block, so the
    transcript ends with the same prose twice. This detects that case so the
    redundant ``result`` can be suppressed. Comparison is whitespace-trimmed
    so trailing-newline / padding differences don't defeat the match. A
    ``result`` whose summary differs (e.g. an error subtype, or a distinct
    wrap-up) returns False so both still render.
    """
    last_assistant: str | None = None
    last_result: dict | None = None
    for e in events:
        t = e.get("type")
        if t == "assistant_text":
            text = e.get("text") or ""
            if text.strip():
                last_assistant = text
        elif t == "result":
            last_result = e
    if last_result is None or last_assistant is None:
        return False
    summary = last_result.get("summary") or ""
    return summary.strip() == last_assistant.strip() and bool(summary.strip())


def pair_tool_events(events) -> list[PairedEvent]:
    """Group tool_use events with their matching tool_result events.

    The pairing key is ``tool_use.id`` -> ``tool_result.tool_use_id``. Events
    that don't pair (assistant_text, thinking, meta, result, and orphan
    tool_result events whose tool_use is not present in this slice) are
    emitted standalone in their original order.

    When the terminal ``result`` event merely echoes the last
    ``assistant_text`` (see :func:`result_duplicates_last_assistant`), the
    redundant ``result`` is dropped so the transcript shows the closing prose
    once. The JS live-tail mirrors this in ``transcript_panel.html``.
    """
    events_list = list(events)
    drop_result = result_duplicates_last_assistant(events_list)
    results_by_id: dict[str, dict] = {}
    for e in events_list:
        if e.get("type") == "tool_result":
            tid = e.get("tool_use_id")
            if tid:
                results_by_id[tid] = e
    paired_ids: set[str] = set()
    # First sub-agent event per task_id seeds a card; later events for the same
    # task fold into that card's dict so a flood of progress events renders as
    # one updating card. The dict is mutated in place, so the PairedEvent that
    # already holds a reference reflects the merged state.
    subagent_cards: dict[str, dict] = {}
    out: list[PairedEvent] = []
    for e in events_list:
        t = e.get("type")
        if t == "result" and drop_result:
            continue
        if t == "subagent":
            tid = e.get("task_id") or e.get("tool_use_id")
            if tid and tid in subagent_cards:
                _merge_subagent(subagent_cards[tid], e)
                continue
            card = dict(e)
            if tid:
                subagent_cards[tid] = card
            out.append(PairedEvent(event=card, paired_result=None))
            continue
        if t == "tool_use":
            tid = e.get("id")
            result = results_by_id.get(tid) if tid else None
            if result is not None and tid is not None:
                paired_ids.add(tid)
            out.append(PairedEvent(event=e, paired_result=result))
        elif t == "tool_result":
            tid = e.get("tool_use_id")
            if tid and tid in paired_ids:
                continue
            out.append(PairedEvent(event=e, paired_result=None))
        else:
            out.append(PairedEvent(event=e, paired_result=None))
    return out
