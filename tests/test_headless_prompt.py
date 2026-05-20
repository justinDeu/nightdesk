from nightdesk.worker.headless_prompt import (
    HEADLESS_POLICY_VERSION,
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
