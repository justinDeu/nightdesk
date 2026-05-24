"""Unit tests for the transcript view helpers.

These helpers feed the Jinja transcript renderer and are mirrored by JS in
templates/partials/transcript_panel.html. The tests pin down the public
shape so the JS mirror has a stable target.
"""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from nightdesk import transcript_view as _tv
from nightdesk.transcript_view import (
    assistant_segments,
    diff_counts,
    elide_output,
    pair_tool_events,
    tool_summary,
    unified_diff_rows,
)


# --- unified_diff_rows ----------------------------------------------------


def test_diff_rows_empty_inputs():
    assert unified_diff_rows("", "") == []


def test_diff_rows_pure_insert():
    rows = unified_diff_rows("", "a\nb\n")
    assert [(r.kind, r.gutter, r.line_no, r.text) for r in rows] == [
        ("ins", "+", "1", "a"),
        ("ins", "+", "2", "b"),
    ]


def test_diff_rows_pure_delete():
    rows = unified_diff_rows("a\nb\n", "")
    assert [(r.kind, r.gutter, r.line_no, r.text) for r in rows] == [
        ("del", "-", "1", "a"),
        ("del", "-", "2", "b"),
    ]


def test_diff_rows_context_and_replace():
    rows = unified_diff_rows("one\ntwo\nthree\n", "one\nTWO\nthree\n")
    kinds = [r.kind for r in rows]
    assert kinds == ["ctx", "del", "ins", "ctx"]
    # Line numbers track old-side for deletes, new-side for inserts/context.
    assert [r.line_no for r in rows] == ["1", "2", "2", "3"]


def test_diff_rows_trailing_newline_does_not_create_phantom_row():
    # Trailing newlines should be ignored so the visible row count matches.
    rows = unified_diff_rows("x\n", "x")
    assert [r.kind for r in rows] == ["ctx"]


# --- diff_counts ----------------------------------------------------------


def test_diff_counts_no_change_is_zero_zero():
    assert diff_counts("x\ny\n", "x\ny\n") == (0, 0)
    # Empty inputs are a degenerate no-change case.
    assert diff_counts("", "") == (0, 0)


def test_diff_counts_pure_insert():
    # Three lines added against an empty original.
    assert diff_counts("", "a\nb\nc\n") == (3, 0)


def test_diff_counts_pure_delete():
    assert diff_counts("a\nb\nc\n", "") == (0, 3)


def test_diff_counts_mixed_replace_counts_both_sides():
    # One line replaced -> contributes 1 to both ins and dels.
    assert diff_counts("one\ntwo\nthree\n", "one\nTWO\nthree\n") == (1, 1)


def test_diff_counts_attribute_access_matches_index():
    c = diff_counts("a\n", "a\nb\n")
    assert (c.ins, c.dels) == (c[0], c[1]) == (1, 0)


def test_diff_counts_matches_row_totals():
    # The aggregate must agree row-for-row with what the renderer shows.
    old = "alpha\nbeta\ngamma\ndelta\n"
    new = "alpha\nBETA\ngamma\nepsilon\nzeta\n"
    rows = unified_diff_rows(old, new)
    expected_ins = sum(1 for r in rows if r.kind == "ins")
    expected_dels = sum(1 for r in rows if r.kind == "del")
    assert diff_counts(old, new) == (expected_ins, expected_dels)


# --- elide_output ---------------------------------------------------------


def test_elide_short_output_no_hidden():
    out = "\n".join(f"l{i}" for i in range(5))
    e = elide_output(out)
    assert e.total == 5 and e.hidden == 0
    assert e.head == [f"l{i}" for i in range(5)]
    assert e.tail == []


def test_elide_at_threshold_no_hidden():
    out = "\n".join(f"l{i}" for i in range(20))
    e = elide_output(out)
    assert e.total == 20 and e.hidden == 0
    assert len(e.head) == 20


def test_elide_long_output_keeps_first10_last5():
    lines = [f"l{i}" for i in range(30)]
    e = elide_output("\n".join(lines))
    assert e.total == 30
    assert e.hidden == 30 - 10 - 5
    assert e.head == lines[:10]
    assert e.tail == lines[-5:]


# --- assistant_segments ---------------------------------------------------


def test_segments_plain_text_one_segment():
    segs = assistant_segments("just a line")
    assert len(segs) == 1
    assert segs[0].kind == "text" and segs[0].body == "just a line"


def test_segments_fenced_code_preserves_newlines():
    text = "before\n```python\nprint('x')\nprint('y')\n```\nafter"
    segs = assistant_segments(text)
    assert [(s.kind, s.lang) for s in segs] == [
        ("text", ""), ("code", "python"), ("text", "")
    ]
    assert segs[1].body == "print('x')\nprint('y')"


def test_segments_unterminated_fence_flushes_as_code():
    text = "intro\n```\nstill code"
    segs = assistant_segments(text)
    assert segs[0].kind == "text" and segs[0].body == "intro"
    assert segs[1].kind == "code" and segs[1].body == "still code"


# --- tool_summary ---------------------------------------------------------


def test_tool_summary_read_with_range():
    s = tool_summary({"tool": "Read", "input": {"file_path": "/x", "offset": 10, "limit": 20}})
    assert s.tag == "Read" and s.primary == "/x" and s.meta == "lines 10-30"


def test_tool_summary_bash_truncates_long_first_line():
    long_cmd = "echo " + ("a" * 200)
    s = tool_summary({"tool": "Bash", "input": {"command": long_cmd}})
    assert s.tag == "Bash"
    assert s.primary.endswith("...")
    assert len(s.primary) == 80


def test_tool_summary_grep_includes_flags_and_glob():
    s = tool_summary({"tool": "Grep", "input": {
        "pattern": "foo", "-i": True, "-n": True, "glob": "*.py", "path": "src",
    }})
    assert s.tag == "Grep" and s.primary == "foo"
    # All extras concatenated into meta.
    for fragment in ("-i", "-n", "glob=*.py", "in src"):
        assert fragment in s.meta


def test_tool_summary_multiedit_counts_edits():
    s = tool_summary({"tool": "MultiEdit", "input": {"file_path": "/x", "edits": [{}, {}, {}]}})
    assert s.meta == "3 edits"


def test_tool_summary_unknown_tool_falls_through():
    s = tool_summary({"tool": "Weird", "input": {}})
    assert s.tag == "Weird" and s.tag_class == "generic"


def test_tool_summary_task_create_and_update():
    from nightdesk.transcript_view import tool_summary
    c = tool_summary({"type": "tool_use", "tool": "TaskCreate",
                      "input": {"subject": "Read main.py", "description": "d"}})
    assert c.tag == "TaskCreate" and c.tag_class == "task" and c.primary == "Read main.py"
    u = tool_summary({"type": "tool_use", "tool": "TaskUpdate",
                      "input": {"taskId": "2", "status": "completed"}})
    assert u.primary == "task #2" and u.meta == "completed" and u.tag_class == "task"


# --- default open/collapsed state on rendered partials --------------------
#
# The transcript page should show the body of tools whose body IS the
# information (Edit/MultiEdit/Write/Bash) and of tool_result without the
# user having to click. Read/Glob/Grep have no body so they render as a
# summary-only card; the generic fallback stays collapsed. Thinking stays
# collapsed (it is noisy by design).
#
# The JS live-tail in templates/partials/transcript_panel.html mirrors
# these defaults — keep both sides in sync.


def _jinja_env() -> Environment:
    templates_dir = Path(__file__).resolve().parents[1] / "src" / "nightdesk" / "templates"
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(["html"]),
    )
    env.globals["tv"] = _tv
    return env


def _render_tool(evt: dict) -> str:
    tmpl = _jinja_env().from_string(
        "{% import 'partials/tool_card.html' as tc %}{{ tc.render_tool(evt) }}"
    )
    return tmpl.render(evt=evt)


def _render_event(evt: dict) -> str:
    tmpl = _jinja_env().from_string(
        "{% import 'partials/transcript_event.html' as te %}{{ te.render_event(evt) }}"
    )
    return tmpl.render(evt=evt)


def test_tool_card_edit_defaults_open():
    html = _render_tool({"tool": "Edit", "input": {
        "file_path": "/x", "old_string": "a\n", "new_string": "b\n",
    }})
    assert '<details class="tc-card" open>' in html


def test_tool_card_bash_defaults_open():
    html = _render_tool({"tool": "Bash", "input": {"command": "echo hi"}})
    assert '<details class="tc-card" open>' in html


def test_tool_card_write_defaults_open():
    html = _render_tool({"tool": "Write", "input": {
        "file_path": "/x", "content": "line\n",
    }})
    assert '<details class="tc-card" open>' in html


def test_tool_card_multi_edit_defaults_open():
    html = _render_tool({"tool": "MultiEdit", "input": {
        "file_path": "/x",
        "edits": [{"old_string": "a", "new_string": "b"}],
    }})
    assert '<details class="tc-card" open>' in html


def test_tool_card_read_stays_collapsed():
    html = _render_tool({"tool": "Read", "input": {"file_path": "/x"}})
    assert '<details class="tc-card">' in html
    # No open attribute anywhere in the Read card.
    assert " open>" not in html


def test_tool_card_glob_stays_collapsed():
    html = _render_tool({"tool": "Glob", "input": {"pattern": "*.py"}})
    assert '<details class="tc-card">' in html
    assert " open>" not in html


def test_tool_card_grep_stays_collapsed():
    html = _render_tool({"tool": "Grep", "input": {"pattern": "foo"}})
    assert '<details class="tc-card">' in html
    assert " open>" not in html


def test_tool_card_generic_stays_collapsed():
    html = _render_tool({"tool": "WeirdTool", "input": {"foo": 1}})
    assert '<details class="tc-card">' in html
    assert " open>" not in html


def test_tool_result_defaults_open():
    html = _render_event({"type": "tool_result", "output": "ok",
                          "is_error": False})
    assert 'class="tool-result" open>' in html


def test_tool_result_error_defaults_open():
    html = _render_event({"type": "tool_result", "output": "boom",
                          "is_error": True})
    assert 'class="tool-result is-error" open>' in html


def test_thinking_block_stays_collapsed():
    html = _render_event({"type": "thinking", "text": "hmm"})
    assert '<details class="thinking-block">' in html
    assert " open>" not in html


def test_rate_limit_event_renders_card_with_countdown():
    # A rate_limit event must render a visible RATE LIMITED card with the
    # status, limit type, and a countdown wired to resets_at — not be dropped.
    html = _render_event({
        "type": "rate_limit", "status": "rejected",
        "rate_limit_type": "five_hour", "resets_at": 1_900_000_000,
        "utilization": 1.0, "raw": "status: rejected\nresets_at: 1900000000",
    })
    assert "rate limited" in html.lower()
    assert "rejected" in html
    assert "five_hour" in html
    assert 'data-resets-at="1900000000"' in html
    assert "rate-limit-countdown" in html
    # The full payload is available under a raw-response dropdown.
    assert "raw response" in html
    assert "<details" in html


def test_rate_limit_event_renders_raw_only_when_fields_missing():
    # Generic case: an SDK that doesn't expose the known fields still shows the
    # RATE LIMITED badge and the raw payload dropdown (no countdown, no status).
    html = _render_event({
        "type": "rate_limit",
        "raw": "some_field: some_value\nother: 123",
    })
    assert "rate limited" in html.lower()
    assert "raw response" in html
    assert "some_field: some_value" in html
    assert "rate-limit-countdown" not in html


def test_cancelled_event_renders_marker_card():
    # A cancelled event must render a visible CANCELLED card with the reason,
    # so the user sees why the run stopped instead of an abrupt stream end.
    html = _render_event({
        "type": "cancelled", "message": "Run cancelled by user.",
    })
    assert "cancelled" in html.lower()
    assert "Run cancelled by user." in html
    assert "border-warn" in html


def test_cancelled_event_defaults_message_when_absent():
    # Legacy/partial events without a message still render a sensible default.
    html = _render_event({"type": "cancelled"})
    assert "Run cancelled by user." in html


def test_thinking_empty_renders_nothing():
    # Legacy transcripts on disk may carry thinking events with empty
    # content (some Claude variants returned blocks where the raw CoT
    # never left the API). The renderer must drop those rather than
    # show a misleading "thinking (0 tokens)" placeholder card.
    html = _render_event({"type": "thinking", "text": ""}).strip()
    assert html == ""


# --- subagent_index -----------------------------------------------------------


def test_subagent_index_lists_each_card():
    from nightdesk.transcript_view import subagent_index
    events = [
        {"type": "subagent", "phase": "started", "task_id": "k1",
         "tool_use_id": "A", "subagent_type": "Explore"},
        {"type": "subagent", "phase": "notification", "task_id": "k1",
         "tool_use_id": "A", "status": "completed",
         "usage": {"tool_uses": 6, "total_tokens": 1200, "duration_ms": 4000}},
        {"type": "subagent", "phase": "started", "task_id": "k2",
         "tool_use_id": "B", "subagent_type": "Plan"},
    ]
    idx = subagent_index(events)
    assert [r["label"] for r in idx] == ["Explore", "Plan"]   # creation order preserved
    explore = idx[0]
    assert explore["tool_use_id"] == "A"
    assert explore["status"] == "completed"
    assert explore["tool_uses"] == 6
    assert explore["done"] is True


def test_subagent_index_ignores_non_subagent_events():
    from nightdesk.transcript_view import subagent_index
    events = [
        {"type": "tool_use", "id": "x", "tool": "Read", "input": {}},
        {"type": "subagent", "phase": "started", "task_id": "k1",
         "tool_use_id": "A", "subagent_type": "Explore"},
    ]
    idx = subagent_index(events)
    assert len(idx) == 1 and idx[0]["label"] == "Explore"


def test_thinking_whitespace_only_renders_nothing():
    html = _render_event({"type": "thinking", "text": "   \n\t  "}).strip()
    assert html == ""


# --- assistant_text block chrome ------------------------------------------
#
# Assistant turns render as first-class blocks: visible chrome (left accent
# border, elevated background) plus an author chip at the top. The chip
# prefers ``evt.model`` (if a future translator surfaces one) and falls back
# to 'claude'. The JS live-tail in templates/partials/transcript_panel.html
# mirrors this — keep both sides in sync.


def test_assistant_text_renders_author_chip_default():
    html = _render_event({"type": "assistant_text", "text": "hi there"})
    assert 'class="assistant-text"' in html
    assert 'class="assistant-author"' in html
    # Default attribution when no model field is carried on the event.
    assert ">claude<" in html
    # Prose still renders.
    assert "<p>hi there</p>" in html


def test_assistant_text_uses_model_when_present():
    html = _render_event({
        "type": "assistant_text",
        "text": "hello",
        "model": "claude-sonnet-4-5",
    })
    assert ">claude-sonnet-4-5<" in html
    # Default label is not also emitted.
    assert ">claude<" not in html


def test_assistant_text_empty_renders_nothing():
    # An empty-text assistant event renders nothing at all — otherwise the
    # new chrome would show as a bare "claude" chip with no body.
    assert _render_event({"type": "assistant_text", "text": ""}).strip() == ""
    assert _render_event({"type": "assistant_text", "text": "   \n  "}).strip() == ""


# --- pair_tool_events -----------------------------------------------------
#
# Pairing binds each tool_result to its parent tool_use so the static renderer
# can place them inside the same .tc-card. The live-tail JS in
# templates/partials/transcript_panel.html mirrors this — keep both sides in
# sync.


def test_pair_simple_use_then_result():
    use = {"type": "tool_use", "id": "A", "tool": "Bash", "input": {"command": "x"}}
    res = {"type": "tool_result", "tool_use_id": "A", "output": "ok"}
    paired = pair_tool_events([use, res])
    # The tool_result is filtered out of the top-level list — it's carried
    # inside the tool_use's paired_result.
    assert len(paired) == 1
    assert paired[0].event is use
    assert paired[0].paired_result is res


def test_pair_orphan_result_renders_standalone():
    # Result with no matching tool_use in this slice still appears in the
    # output list so the renderer can fall back to a standalone card.
    res = {"type": "tool_result", "tool_use_id": "missing", "output": "ok"}
    paired = pair_tool_events([res])
    assert len(paired) == 1
    assert paired[0].event is res
    assert paired[0].paired_result is None


def test_pair_interleaved_uses_and_results():
    # A_use, B_use, A_result, B_result — each result lands on the right card
    # regardless of intervening events.
    a_use = {"type": "tool_use", "id": "A", "tool": "Bash", "input": {}}
    b_use = {"type": "tool_use", "id": "B", "tool": "Bash", "input": {}}
    a_res = {"type": "tool_result", "tool_use_id": "A", "output": "a-ok"}
    b_res = {"type": "tool_result", "tool_use_id": "B", "output": "b-ok"}
    paired = pair_tool_events([a_use, b_use, a_res, b_res])
    assert [p.event for p in paired] == [a_use, b_use]
    assert paired[0].paired_result is a_res
    assert paired[1].paired_result is b_res


def test_pair_preserves_non_tool_events_in_order():
    text = {"type": "assistant_text", "text": "hi"}
    use = {"type": "tool_use", "id": "A", "tool": "Bash", "input": {}}
    res = {"type": "tool_result", "tool_use_id": "A", "output": "ok"}
    thinking = {"type": "thinking", "text": "..."}
    paired = pair_tool_events([text, use, res, thinking])
    assert [p.event for p in paired] == [text, use, thinking]
    assert paired[1].paired_result is res


# --- duplicate final-message suppression (BUG 22) -------------------------
#
# The CC SDK's terminal result event usually echoes the model's last
# assistant_text, so the transcript ends with the same prose twice. When they
# match (trimmed), the redundant result is dropped; when they differ both show.
# The live-tail JS in transcript_panel.html mirrors this rule.


def test_result_equal_to_last_assistant_is_suppressed():
    text = {"type": "assistant_text", "text": "All done. Tests pass."}
    result = {"type": "result", "subtype": "success",
              "summary": "All done. Tests pass."}
    paired = pair_tool_events([text, result])
    types = [p.event["type"] for p in paired]
    assert types == ["assistant_text"]
    assert "result" not in types


def test_result_equal_modulo_whitespace_is_suppressed():
    text = {"type": "assistant_text", "text": "All done.\n"}
    result = {"type": "result", "subtype": "success", "summary": "  All done.  "}
    paired = pair_tool_events([text, result])
    assert [p.event["type"] for p in paired] == ["assistant_text"]


def test_result_differing_from_last_assistant_keeps_both():
    text = {"type": "assistant_text", "text": "Working on it."}
    result = {"type": "result", "subtype": "success", "summary": "All done."}
    paired = pair_tool_events([text, result])
    assert [p.event["type"] for p in paired] == ["assistant_text", "result"]


def test_result_with_no_prior_assistant_still_renders():
    result = {"type": "result", "subtype": "error", "summary": "crashed"}
    paired = pair_tool_events([result])
    assert [p.event["type"] for p in paired] == ["result"]


def test_empty_result_summary_does_not_suppress():
    text = {"type": "assistant_text", "text": ""}
    result = {"type": "result", "subtype": "success", "summary": ""}
    paired = pair_tool_events([text, result])
    # Empty assistant_text is itself dropped at render time; an empty result
    # must not be considered a duplicate.
    assert "result" in [p.event["type"] for p in paired]


# --- live "tailing live" indicator (BUG 18) -------------------------------
#
# The status element should carry an animated pulsing dot + ellipsis and a
# larger label than the surrounding muted captions. The animation is driven
# by CSS classes/keyframes in src/styles/app.css (compiled to static/app.css).


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_live_status_markup_has_animated_dot_and_label():
    src = (_repo_root() / "src" / "nightdesk" / "templates"
           / "partials" / "transcript_panel.html").read_text()
    # The server-rendered status carries the pulsing dot, the label, and the
    # animated ellipsis spans inside the nd-live-status container.
    assert 'id="transcript-status"' in src
    assert "nd-live-status" in src
    assert "nd-live-dot" in src
    assert "nd-live-ellipsis" in src
    assert "tailing live" in src
    # The live-tail JS keeps the animation by only stripping it on terminal
    # states (stream closed / error).
    assert "setStatusDone" in src


def test_live_status_animation_compiled_into_css():
    css = (_repo_root() / "src" / "nightdesk" / "static" / "app.css").read_text()
    # Keyframes + selectors must survive the Tailwind build so the dot pulses
    # and the ellipsis animates.
    assert "nd-live-pulse" in css
    assert "nd-live-ellipsis" in css
    assert ".nd-live-status" in css
    assert ".nd-live-dot" in css


# --- pairing in the rendered DOM ------------------------------------------


def _render_paired(events: list[dict]) -> str:
    # Mirror the iteration in templates/partials/transcript_panel.html so the
    # tests exercise the same pairing pathway the page does.
    env = _jinja_env()
    tmpl = env.from_string(
        "{% import 'partials/transcript_event.html' as te %}"
        "{% for item in tv.pair_tool_events(events) %}"
        "{{ te.render_event(item.event, item.paired_result) }}"
        "{% endfor %}"
    )
    return tmpl.render(events=events)


def test_paired_use_and_result_render_as_one_card():
    use = {"type": "tool_use", "id": "X", "tool": "Bash",
           "input": {"command": "echo hi"}}
    res = {"type": "tool_result", "tool_use_id": "X", "output": "hi"}
    html = _render_paired([use, res])
    # Exactly one tool card.
    assert html.count('class="tc-card"') == 1
    # The tool_result block is inside the tc-body, after the bash-cmd input
    # and the input/output divider.
    bash_pos = html.index("bash-cmd")
    div_pos = html.index("tc-divider")
    result_pos = html.index("tool-result")
    assert bash_pos < div_pos < result_pos
    # And the surrounding wrapper still carries the use id for the JS to
    # locate the card.
    assert 'data-tool-use-id="X"' in html


def test_unmatched_result_renders_standalone():
    # No matching tool_use -> result renders on its own outside any card.
    res = {"type": "tool_result", "tool_use_id": "missing", "output": "lonely"}
    html = _render_paired([res])
    assert 'class="tool-result' in html
    # No tool card and no divider since there's no parent to attach to.
    assert "tc-card" not in html
    assert "tc-divider" not in html


def test_paired_error_result_keeps_is_error_styling():
    use = {"type": "tool_use", "id": "E", "tool": "Bash",
           "input": {"command": "false"}}
    res = {"type": "tool_result", "tool_use_id": "E",
           "output": "boom", "is_error": True}
    html = _render_paired([use, res])
    # is-error class is preserved inside the parent card; only one card.
    assert html.count('class="tc-card"') == 1
    assert "tool-result is-error" in html
    # Card body still carries the divider since the bash input took a slot.
    assert "tc-divider" in html


def test_paired_result_inside_read_card_with_no_input_body():
    # Read has no input body. The paired result lives in a tc-body that
    # only contains the result — no divider, since there's nothing to
    # separate from.
    use = {"type": "tool_use", "id": "R", "tool": "Read",
           "input": {"file_path": "/x"}}
    res = {"type": "tool_result", "tool_use_id": "R", "output": "contents"}
    html = _render_paired([use, res])
    assert html.count('class="tc-card"') == 1
    assert "tool-result" in html
    assert "tc-divider" not in html


def test_intervening_events_do_not_break_pairing():
    # assistant_text and another tool_use land between A_use and A_result.
    # The result still ends up inside A's card.
    a_use = {"type": "tool_use", "id": "A", "tool": "Bash",
             "input": {"command": "a"}}
    text = {"type": "assistant_text", "text": "thinking out loud"}
    b_use = {"type": "tool_use", "id": "B", "tool": "Bash",
             "input": {"command": "b"}}
    a_res = {"type": "tool_result", "tool_use_id": "A", "output": "a-ok"}
    b_res = {"type": "tool_result", "tool_use_id": "B", "output": "b-ok"}
    html = _render_paired([a_use, text, b_use, a_res, b_res])
    # Two tool cards, no standalone result block at the top level.
    assert html.count('class="tc-card"') == 2
    # Both results are present (inside their respective cards).
    assert html.count('class="tool-result" open') == 2


# --- group_by_subagent -------------------------------------------------------


def test_group_paired_events_by_subagent():
    from nightdesk.transcript_view import pair_tool_events, group_by_subagent
    events = [
        {"type": "tool_use", "id": "agent1", "tool": "Agent", "input": {}},
        {"type": "subagent", "phase": "started", "task_id": "k1",
         "tool_use_id": "agent1", "subagent_type": "Explore"},
        {"type": "tool_use", "id": "g1", "tool": "Glob", "input": {},
         "parent_tool_use_id": "agent1"},
        {"type": "tool_result", "tool_use_id": "g1", "output": "..."},
        {"type": "subagent", "phase": "notification", "task_id": "k1",
         "tool_use_id": "agent1", "status": "completed"},
        {"type": "tool_use", "id": "top1", "tool": "Read", "input": {}},
    ]
    groups = group_by_subagent(pair_tool_events(events))
    sub = next(g for g in groups if g.event.get("type") == "subagent")
    assert [c.event.get("tool") for c in sub.children] == ["Glob"]
    # the Glob's tool_result is carried as the child's paired_result, not a separate node
    glob_child = sub.children[0]
    assert glob_child.paired_result is not None
    assert glob_child.paired_result.get("tool_use_id") == "g1"
    # the top-level Read stays top-level
    assert any(g.event.get("tool") == "Read" for g in groups)


def test_group_handles_parallel_subagents():
    from nightdesk.transcript_view import pair_tool_events, group_by_subagent
    events = [
        {"type": "subagent", "phase": "started", "task_id": "a",
         "tool_use_id": "A", "subagent_type": "Explore"},
        {"type": "subagent", "phase": "started", "task_id": "b",
         "tool_use_id": "B", "subagent_type": "Plan"},
        {"type": "tool_use", "id": "x", "tool": "Glob", "input": {}, "parent_tool_use_id": "B"},
        {"type": "tool_use", "id": "y", "tool": "Bash", "input": {}, "parent_tool_use_id": "A"},
    ]
    groups = group_by_subagent(pair_tool_events(events))
    by_label = {g.event.get("subagent_type"): g for g in groups
                if g.event.get("type") == "subagent"}
    assert [c.event["tool"] for c in by_label["Plan"].children] == ["Glob"]
    assert [c.event["tool"] for c in by_label["Explore"].children] == ["Bash"]


def test_group_orphan_parent_stays_top_level():
    from nightdesk.transcript_view import pair_tool_events, group_by_subagent
    # parent_tool_use_id references a sub-agent not present -> render top-level
    events = [
        {"type": "tool_use", "id": "z", "tool": "Bash", "input": {}, "parent_tool_use_id": "ghost"},
    ]
    groups = group_by_subagent(pair_tool_events(events))
    assert len(groups) == 1
    assert groups[0].event.get("tool") == "Bash"


# --- build_todo_list ----------------------------------------------------------


def test_todo_list_from_task_tools():
    from nightdesk.transcript_view import build_todo_list
    events = [
        {"type": "tool_use", "id": "c1", "tool": "TaskCreate",
         "input": {"subject": "alpha", "activeForm": "Doing alpha"}, "seq": 1},
        {"type": "tool_use", "id": "c2", "tool": "TaskCreate",
         "input": {"subject": "beta"}, "seq": 2},
        {"type": "tool_use", "id": "u1", "tool": "TaskUpdate",
         "input": {"taskId": "1", "status": "completed"}, "seq": 3},
    ]
    todos = build_todo_list(events)
    assert [(t["label"], t["status"]) for t in todos] == [
        ("alpha", "completed"), ("beta", "pending")]
    assert todos[0]["activeForm"] == "Doing alpha"


def test_todo_list_task_delete_removes_item():
    from nightdesk.transcript_view import build_todo_list
    events = [
        {"type": "tool_use", "id": "c1", "tool": "TaskCreate", "input": {"subject": "a"}, "seq": 1},
        {"type": "tool_use", "id": "c2", "tool": "TaskCreate", "input": {"subject": "b"}, "seq": 2},
        {"type": "tool_use", "id": "u1", "tool": "TaskUpdate",
         "input": {"taskId": "1", "status": "deleted"}, "seq": 3},
    ]
    todos = build_todo_list(events)
    assert [t["label"] for t in todos] == ["b"]


def test_todo_list_from_todowrite_snapshot():
    from nightdesk.transcript_view import build_todo_list
    events = [
        {"type": "tool_use", "id": "w1", "tool": "TodoWrite", "seq": 1,
         "input": {"todos": [{"content": "a", "status": "pending", "activeForm": "A"}]}},
        {"type": "tool_use", "id": "w2", "tool": "TodoWrite", "seq": 2,
         "input": {"todos": [
            {"content": "a", "status": "completed", "activeForm": "A"},
            {"content": "b", "status": "in_progress", "activeForm": "B"}]}},
    ]
    todos = build_todo_list(events)
    assert [(t["label"], t["status"]) for t in todos] == [
        ("a", "completed"), ("b", "in_progress")]


def test_todo_list_empty_when_no_task_tools():
    from nightdesk.transcript_view import build_todo_list
    assert build_todo_list([{"type": "tool_use", "id": "x", "tool": "Read", "input": {}}]) == []


def test_todo_list_prefers_task_tools_over_todowrite():
    from nightdesk.transcript_view import build_todo_list
    events = [
        {"type": "tool_use", "id": "c1", "tool": "TaskCreate", "input": {"subject": "from-task"}, "seq": 1},
        {"type": "tool_use", "id": "w1", "tool": "TodoWrite", "seq": 2,
         "input": {"todos": [{"content": "from-todo", "status": "pending"}]}},
    ]
    todos = build_todo_list(events)
    assert [t["label"] for t in todos] == ["from-task"]


# --- accumulate_stats --------------------------------------------------------


def test_accumulate_stats_counts_subagent_tools_once():
    from nightdesk.transcript_view import accumulate_stats
    events = [
        {"type": "tool_use", "id": "agent1", "tool": "Agent", "input": {}, "seq": 1},
        {"type": "subagent", "phase": "started", "task_id": "k1",
         "tool_use_id": "agent1", "subagent_type": "Explore", "seq": 2},
        {"type": "tool_use", "id": "g1", "tool": "Glob", "input": {},
         "parent_tool_use_id": "agent1", "seq": 3},
        {"type": "tool_use", "id": "g2", "tool": "Bash", "input": {},
         "parent_tool_use_id": "agent1", "seq": 4},
        {"type": "subagent", "phase": "notification", "task_id": "k1",
         "tool_use_id": "agent1", "status": "completed",
         "usage": {"tool_uses": 2, "total_tokens": 100, "duration_ms": 500}, "seq": 5},
    ]
    stats = accumulate_stats(events)
    # 1 Agent dispatch + 2 nested tool calls = 3 real tool_use events.
    # The sub-agent's usage.tool_uses (2) must NOT be added on top.
    assert stats["tool_count"] == 3
