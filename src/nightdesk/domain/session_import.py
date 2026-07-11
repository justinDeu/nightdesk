"""Delta-import of terminal turns from the Claude Code session jsonl.

An agent's ``resume_handle.session_id`` is a real ``~/.claude`` session: the
"Open in terminal" handoff runs ``claude --resume <id>`` against the same
jsonl. Turns made in that terminal land only in CC's file — nightdesk's
canonical transcript never sees them until someone replays the delta. This
module owns that replay so both the session host (on boot, before the
resident resumes) and the API (``POST /agents/{aid}/sync-terminal``, for a
parked agent with an open screen) run the exact same logic:

- ``cc_session_jsonl``  — locate the jsonl for a cwd + session id.
- ``import_delta``      — translate lines past the watermark through a caller
                          -supplied writer (the host uses its live seq counter;
                          the API uses ``transcript.append_events``).
- ``advance_import_watermark`` — stamp ``resume_handle.imported_lines`` to the
                          file's current length after live-streamed turns.
- ``terminal_drift``    — lines past the watermark (0 when clean / no jsonl).
- ``sync_terminal``     — the standalone API-side import: append the delta to
                          the canonical transcript and advance the watermark.

LIVE-HOST CAVEAT: while a host process is alive it owns the jsonl — it streams
the same turns live and advances the watermark itself as each turn completes.
Importing out-of-band would race that accounting and double-write turns, and a
terminal attached to a live session is already a two-writers problem upstream
of us. So drift is only meaningful (and ``sync_terminal`` only safe) when the
session has NO live host; callers must gate on host liveness and report drift
as 0 while live. We deliberately do not try to solve concurrent terminal use.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Callable, Optional

from nightdesk.db.models import Session
from nightdesk.transcript import append_events
from nightdesk.worker.claude_translator import translate


log = logging.getLogger(__name__)


def cc_session_jsonl(source_path: Optional[str], session_id: str) -> Optional[Path]:
    """Locate the claude session jsonl for the agent's cwd + session id.

    Trusted posture reads the real ``~/.claude/projects/<slug>/<session_id>.jsonl``.
    An explicit ``$NIGHTDESK_CC_PROJECTS_DIR`` overrides the projects root (used
    by tests). Best-effort: returns None if the directory-key helper is absent.
    """
    if not source_path:
        return None
    try:
        from claude_agent_sdk import project_key_for_directory
        slug = project_key_for_directory(source_path)
    except Exception:  # noqa: BLE001
        return None
    root = os.environ.get("NIGHTDESK_CC_PROJECTS_DIR") or os.path.expanduser("~/.claude/projects")
    return Path(root) / slug / f"{session_id}.jsonl"


# Slash commands and local-command output land in the jsonl as user entries
# whose text is wrapped in these tags. They are UI plumbing, not something the
# human said conversationally — never render them as user bubbles.
_NON_CONVERSATIONAL_PREFIXES = (
    "<command-name>",
    "<command-message>",
    "<local-command-stdout>",
    "<local-command-caveat>",
    # CC writes this synthetic user entry when a turn is interrupted (Esc);
    # the human did not type it.
    "[Request interrupted",
)


def _terminal_user_message(entry: dict) -> Optional[dict]:
    """A ``user_message`` event for a conversational CC-jsonl user entry.

    Returns None for everything that is not a human prompt:
    - entries flagged ``isMeta`` (caveat preambles), ``isSidechain`` (subagent
      traffic) or ``isCompactSummary`` (compaction artifacts);
    - entries carrying ``tool_result`` blocks — that is tool plumbing the
      translator already renders as tool_result cards;
    - slash-command / local-command wrapper entries (see the prefixes above);
    - entries with no extractable text (content neither a string nor a list
      of text blocks, or empty after extraction).

    Content may be a plain string or a list of ``{"type": "text"}`` blocks
    (joined with newlines). The emitted shape matches the host's
    delivery-time ``user_message`` so the frontend renders it as the same
    user bubble; ``source: "terminal"`` marks its provenance.
    """
    if entry.get("type") != "user":
        return None
    if entry.get("isMeta") or entry.get("isSidechain") or entry.get("isCompactSummary"):
        return None
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                return None
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        text = "\n".join(parts)
    else:
        return None
    if not text.strip():
        return None
    if text.lstrip().startswith(_NON_CONVERSATIONAL_PREFIXES):
        return None
    return {"type": "user_message", "text": text, "source": "terminal"}


def import_delta(
    path: Path, watermark: int, write: Callable[[dict], None],
) -> tuple[int, int]:
    """Translate jsonl entries past ``watermark`` through ``write``.

    ``--resume`` keeps one jsonl per session (no fork), so this is a trivial
    delta: read the session file past the last line already imported and feed
    each entry's canonical events to the writer. Returns
    ``(imported_entries, total_lines)``; writes nothing when the file has not
    grown. The caller owns updating the watermark to ``total_lines``.

    Conversational user entries (the prompt the human typed in the terminal)
    are emitted as ``user_message`` events ahead of the translator output, so
    an imported round-trip shows both sides. This cannot double-bubble live
    nightdesk turns: the host writes ``user_message`` itself at delivery time
    and advances the watermark past the CLI's mirror of the turn when it
    completes (and again at teardown), so those lines are never re-read here
    — only genuine cold-window terminal lines land past the watermark.
    """
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if len(lines) <= watermark:
        return 0, len(lines)
    imported = 0
    for line in lines[watermark:]:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            um = _terminal_user_message(entry)
            if um is not None:
                write(um)
            for c in translate(entry):
                write(c)
        imported += 1
    return imported, len(lines)


def advance_import_watermark(db, srow: Session) -> None:
    """Mark the CC session jsonl as fully imported up to its current length.

    Called after live-streamed output lands in the transcript (per-turn and
    at host teardown). Uses the same read/count semantics as ``import_delta``
    so the watermark is exact. Best-effort: silently a no-op when the jsonl
    cannot be located.
    """
    handle = srow.resume_handle or {}
    cc_sid = handle.get("session_id")
    if not cc_sid:
        return
    try:
        path = cc_session_jsonl(srow.source_path, str(cc_sid))
        if path is None or not path.exists():
            return
        n = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        log.exception("watermark advance failed for session %s", srow.id)
        return
    if int(handle.get("imported_lines") or 0) == n:
        return
    srow.resume_handle = {**handle, "imported_lines": n}
    db.commit()


def terminal_drift(row: Session) -> int:
    """Jsonl lines past the import watermark; 0 when clean or no jsonl.

    Purely a file-vs-handle diff — it does NOT check host liveness. Callers
    exposing this to the UI must report 0 while a host is alive (see the
    module docstring): the live host streams those turns itself.
    """
    handle = row.resume_handle or {}
    cc_sid = handle.get("session_id")
    if not cc_sid:
        return 0
    try:
        path = cc_session_jsonl(row.source_path, str(cc_sid))
        if path is None or not path.exists():
            return 0
        n = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return 0
    return max(0, n - int(handle.get("imported_lines") or 0))


def sync_terminal(db, row: Session) -> int:
    """Standalone delta-import for the API process (no live host).

    Appends the translated delta to the canonical transcript via
    ``append_events`` (which continues the file's monotonic seq space, so the
    open SSE tail picks the lines up) and advances the watermark. Returns the
    number of jsonl entries imported. The caller must have verified there is
    no live host — see the module docstring.
    """
    handle = row.resume_handle or {}
    cc_sid = handle.get("session_id")
    if not cc_sid:
        return 0
    path = cc_session_jsonl(row.source_path, str(cc_sid))
    if path is None or not path.exists():
        return 0
    watermark = int(handle.get("imported_lines") or 0)
    batch: list[dict] = []
    imported, total = import_delta(path, watermark, batch.append)
    if total <= watermark:
        return 0
    if batch:
        append_events(row.transcript_path, batch)
    row.resume_handle = {**handle, "session_id": str(cc_sid), "imported_lines": total}
    db.commit()
    log.info("delta-imported %d jsonl entries for session %s (api sync)",
             imported, row.id)
    return imported
