from __future__ import annotations

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
        "continue": (
            "You are resuming the prior Claude Code conversation on this ticket, "
            "with its full message history loaded. Pick up exactly where that "
            "session left off and keep working toward the goal."
        ),
        "resume": "Continue from the current workspace state.",
        "retry": "Re-attempt from the current workspace state. Do not trust prior conclusions.",
        "restart": "Start fresh. Ignore prior agent reasoning unless restated above.",
        "first_run": "Do the task.",
    }.get(run_intent, "Do the task."))
    return "\n\n".join(parts).rstrip() + "\n"
