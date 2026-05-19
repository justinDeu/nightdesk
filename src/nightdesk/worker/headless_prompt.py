from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


HEADLESS_POLICY_VERSION = "v1"

HEADLESS_PREAMBLE = """This is a headless Nightdesk worker run.
Do not ask the user questions.
Do not request confirmation.
Do not stop to wait for approval.
Choose reasonable defaults from repository context.
If materially blocked, end with a concise blocker summary.
Your final output must be either a completion summary or a blocker summary.
Do not end with a request for user choice.
"""


@dataclass(frozen=True)
class HeadlessEscapeFailure:
    kind: str
    summary: str


_ESCAPE_PHRASES = (
    "which do you prefer",
    "please confirm",
    "should i",
    "want me to",
    "before i implement",
    "if you say go",
    "i need your input",
)


def build_headless_prompt(
    *,
    ticket_id: str,
    ticket_title: str,
    base_prompt: str,
    run_intent: str,
    workspace_path: str,
    next_run_context: Optional[str],
    last_run_summary: Optional[str],
) -> str:
    parts = [
        f"HEADLESS POLICY VERSION: {HEADLESS_POLICY_VERSION}",
        HEADLESS_PREAMBLE.strip(),
        "RUN METADATA\n"
        f"- Ticket: {ticket_id}\n"
        f"- Title: {ticket_title}\n"
        f"- RUN INTENT: {run_intent}\n"
        f"- Workspace: {workspace_path}",
        "BASE TICKET PROMPT\n" + (base_prompt or "").strip(),
    ]
    if next_run_context:
        parts.append("NEXT RUN CONTEXT\n" + next_run_context.strip())
    if last_run_summary:
        parts.append("LAST RUN SUMMARY\n" + last_run_summary.strip())
    parts.append({
        "resume": "Continue from the current workspace state.",
        "retry": "Re-attempt from the current workspace state. Do not trust prior conclusions.",
        "restart": "Start fresh. Ignore prior agent reasoning unless restated above.",
        "first_run": "Do the task.",
    }.get(run_intent, "Do the task."))
    return "\n\n".join(parts).rstrip() + "\n"


def detect_headless_question_escape(
    *,
    final_summary: Optional[str],
    assistant_tail: list[str],
) -> Optional[HeadlessEscapeFailure]:
    texts = [text.strip() for text in assistant_tail if text and text.strip()]
    if final_summary and final_summary.strip():
        texts.append(final_summary.strip())
    for text in reversed(texts[-4:]):
        lowered = text.lower()
        if any(phrase in lowered for phrase in _ESCAPE_PHRASES):
            return HeadlessEscapeFailure(
                kind="headless_requested_input",
                summary=f"headless run requested user input: {text}",
            )
    return None
