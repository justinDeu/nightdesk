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
from dataclasses import dataclass
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
        cwd = (input_.get("cwd") or "").strip()
        meta = f"in {cwd}" if cwd else ""
        return ToolSummary("Bash", "bash", first, meta)
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
    return ToolSummary(tool or "tool", "generic", "", "")


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
    out: list[PairedEvent] = []
    for e in events_list:
        t = e.get("type")
        if t == "result" and drop_result:
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
