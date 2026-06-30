from nightdesk.worker.headless_prompt import (
    HEADLESS_POLICY_VERSION,
    build_continue_prompt,
    build_headless_prompt,
)


def test_build_prompt_for_resume_includes_headless_preamble_and_context():
    prompt = build_headless_prompt(
        ticket_id="t1",
        ticket_title="Drag bug",
        base_prompt="Fix the drag bug",
        run_intent="resume",
        workspace_path="/tmp/work",
        next_run_context="Use polling",
        last_run_summary="Last run asked for polling vs SSE",
    )

    assert HEADLESS_POLICY_VERSION in prompt
    assert "This is a headless Nightdesk worker run." in prompt
    assert "RUN INTENT: resume" in prompt
    assert "Fix the drag bug" in prompt
    assert "Use polling" in prompt
    assert "Last run asked for polling vs SSE" in prompt
    assert "Continue from the current workspace state." in prompt


def test_build_continue_prompt_carries_typed_message_as_user_turn():
    """A genuine continue run sends the user's typed message as the next user
    turn on top of the resumed conversation — NOT folded into a reconstructed
    headless blob. The prior base prompt + history are already in the resumed
    session, so they must not be re-sent."""
    prompt = build_continue_prompt(
        ticket_id="t1",
        ticket_title="Drag bug",
        user_message="Now also fix the touch variant",
        workspace_path="/tmp/work",
    )
    # The typed message is the body of the prompt (the SDK appends prompt
    # verbatim as the next user message when resume= is set).
    assert "USER MESSAGE\nNow also fix the touch variant" in prompt
    assert "RUN INTENT: continue" in prompt
    assert "resuming the prior Claude Code conversation" in prompt
    # Crucially the reconstructed-blob sections are absent — the message is a
    # real user turn, not folded into NEXT RUN CONTEXT / BASE TICKET PROMPT.
    assert "NEXT RUN CONTEXT" not in prompt
    assert "BASE TICKET PROMPT" not in prompt
    assert "LAST RUN SUMMARY" not in prompt


def test_build_continue_prompt_fresh_context_framing_is_honest():
    """When the continue fell back to fresh context (no resumable session), the
    prompt must not claim it is resuming the prior conversation."""
    prompt = build_continue_prompt(
        ticket_id="t1",
        ticket_title="Drag bug",
        user_message="try again",
        workspace_path="/tmp/work",
        fell_back_to_fresh_context=True,
    )
    assert "USER MESSAGE\ntry again" in prompt
    assert "no resumable Claude session" in prompt
    assert "resuming the prior Claude Code conversation" not in prompt

