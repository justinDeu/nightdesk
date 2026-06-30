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


def build_continue_prompt(
    *,
    ticket_id: str,
    ticket_title: str,
    user_message: str,
    workspace_path: str,
    fell_back_to_fresh_context: bool = False,
) -> str:
    """Build the SDK prompt for a continue run so the user's typed message is
    the next user turn on top of the resumed conversation.

    The prior conversation (base ticket prompt + every prior turn) is already in
    the resumed session, so unlike ``build_headless_prompt`` we do NOT re-send
    the reconstructed policy blob — that would bury the user's message inside a
    "NEXT RUN CONTEXT" header and make it read as boilerplate instead of a real
    user turn. The SDK call ``query(prompt=<this>, options=Options(resume=sid))``
    appends ``<this>`` verbatim as the next user message, so the user's typed
    text below genuinely becomes the next turn in the resumed conversation.

    A short headless-mode reminder is prefixed so the continued run keeps
    operating headlessly (the prior turn that established that is in history,
    but the reinforcement is cheap insurance). ``fell_back_to_fresh_context``
    switches the framing to honest fresh-context wording: the agent is NOT
    resuming a conversation in that case, so it must not assume prior reasoning.
    """
    parts = [
        f"HEADLESS POLICY VERSION: {HEADLESS_POLICY_VERSION}",
        (
            "This is a headless Nightdesk worker continuation. Do not ask the "
            "user questions, request confirmation, or wait for approval; choose "
            "reasonable defaults from repository context and keep working until "
            "the task is done."
        ),
        "RUN METADATA\n"
        f"- Ticket: {ticket_id}\n"
        f"- Title: {ticket_title}\n"
        f"- RUN INTENT: continue\n"
        f"- Workspace: {workspace_path}",
    ]
    if fell_back_to_fresh_context:
        parts.append(
            "The prior run had no resumable Claude session, so this run begins "
            "from the current workspace state — the prior conversation history "
            "is not available. Do not assume prior conclusions; treat the user "
            "message below as the next thing to work on."
        )
    else:
        parts.append(
            "You are resuming the prior Claude Code conversation on this ticket, "
            "with its full message history loaded. The user's message below is "
            "the next turn — act on it and continue toward the goal, picking up "
            "where the prior session left off."
        )
    parts.append("USER MESSAGE\n" + (user_message or "").strip())
    return "\n\n".join(parts).rstrip() + "\n"
