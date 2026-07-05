"""Unit tests for the transcript view helpers.

These are pure-Python helpers the worker uses to normalize/summarize
canonical transcript events (see worker/claude_translator.py,
worker/_sdk_runner.py). They used to also feed the now-removed Jinja
transcript renderer; the render-through-the-actual-macro tests went with
that renderer (the SPA renders transcripts in TypeScript instead), leaving
this file to unit-test the pure functions directly.
"""
from __future__ import annotations

from nightdesk import transcript_view as _tv
from nightdesk.transcript_view import (
    assistant_segments,
    diff_counts,
    elide_output,
    pair_tool_events,
    render_markdown,
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


# --- render_markdown ------------------------------------------------------
#
# Assistant prose is markdown; render_markdown turns one non-code segment into
# XSS-safe formatted HTML. The JS live-tail (renderMarkdown in
# transcript_panel.html) mirrors this — keep both sides in sync.


def test_markdown_plain_text_is_a_paragraph():
    assert render_markdown("just a line") == "<p>just a line</p>"


def test_markdown_empty_and_blank_render_nothing():
    assert render_markdown("") == ""
    assert render_markdown("   \n\t ") == ""


def test_markdown_headings():
    assert render_markdown("# Title") == "<h1>Title</h1>"
    assert render_markdown("### Sub") == "<h3>Sub</h3>"


def test_markdown_bold_italic_inline_code():
    html = render_markdown("a **bold**, an *italic*, and `code` here")
    assert "<strong>bold</strong>" in html
    assert "<em>italic</em>" in html
    assert "<code>code</code>" in html


def test_markdown_underscore_emphasis_but_not_intra_word():
    # Leading/closing underscores emphasize; intra-identifier underscores do not.
    assert "<strong>x</strong>" in render_markdown("__x__")
    assert "<em>y</em>" in render_markdown("_y_")
    # snake_case identifiers must survive untouched (no spurious <em>).
    assert render_markdown("call foo_bar_baz now") == "<p>call foo_bar_baz now</p>"


def test_markdown_unordered_list():
    html = render_markdown("- one\n- two\n- three")
    assert html == "<ul><li>one</li><li>two</li><li>three</li></ul>"


def test_markdown_ordered_list():
    html = render_markdown("1. first\n2. second")
    assert html == "<ol><li>first</li><li>second</li></ol>"


def test_markdown_switching_list_marker_closes_and_opens():
    html = render_markdown("- a\n1. b")
    assert html == "<ul><li>a</li></ul><ol><li>b</li></ol>"


def test_markdown_blockquote():
    html = render_markdown("> a quote\n> second line")
    assert html == "<blockquote><p>a quote second line</p></blockquote>"


def test_markdown_horizontal_rule():
    assert render_markdown("---") == "<hr>"


def test_markdown_table():
    html = render_markdown("| a | b |\n|---|---|\n| 1 | 2 |")
    assert html == (
        "<table><thead><tr><th>a</th><th>b</th></tr></thead>"
        "<tbody><tr><td>1</td><td>2</td></tr></tbody></table>"
    )


def test_markdown_paragraphs_split_on_blank_line():
    html = render_markdown("line one\nstill one\n\nline two")
    assert html == "<p>line one still one</p><p>line two</p>"


def test_markdown_safe_link_renders_anchor():
    html = render_markdown("see [docs](https://example.com/x?a=1&b=2)")
    # The ampersand in the URL is escaped; rel hardening is present.
    assert ('<a href="https://example.com/x?a=1&amp;b=2"'
            ' rel="noopener noreferrer nofollow">docs</a>') in html


def test_markdown_mailto_and_relative_links_allowed():
    assert '<a href="mailto:a@b.com"' in render_markdown("[m](mailto:a@b.com)")
    assert '<a href="/board"' in render_markdown("[b](/board)")
    assert '<a href="#top"' in render_markdown("[t](#top)")


# --- render_markdown: XSS safety ------------------------------------------
#
# Transcript content is hostile-by-assumption. render_markdown must never let
# raw HTML or a dangerous URL scheme through.


def test_markdown_escapes_raw_html():
    html = render_markdown("Raw <script>alert(1)</script> & <b>x</b>")
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&amp;" in html
    assert "<b>x</b>" not in html


def test_markdown_escapes_html_inside_inline_code():
    html = render_markdown("`<img src=x onerror=alert(1)>`")
    assert "<img" not in html
    assert "<code>&lt;img src=x onerror=alert(1)&gt;</code>" in html


def test_markdown_rejects_javascript_url():
    html = render_markdown("[click](javascript:alert(1))")
    # No anchor and no href is emitted; the markdown falls back to inert
    # literal text so the dangerous scheme can never execute.
    assert "<a " not in html
    assert "href=" not in html
    assert html == "<p>[click](javascript:alert(1))</p>"


def test_markdown_rejects_data_and_vbscript_urls():
    for bad in ("data:text/html,<script>1</script>", "vbscript:msgbox(1)"):
        html = render_markdown(f"[x]({bad})")
        assert "<a " not in html
        assert "href=" not in html


def test_markdown_link_text_is_escaped():
    html = render_markdown("[<b>hi</b>](https://example.com)")
    assert "<b>hi</b>" not in html
    assert "&lt;b&gt;hi&lt;/b&gt;" in html


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


def test_tool_summary_bash_has_no_meta():
    s = tool_summary({"tool": "Bash", "input": {"command": "ls"}})
    assert s.meta == ""


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


def test_rate_limit_is_limited_classification():
    # allowed + no overage trouble → benign.
    assert _tv.rate_limit_is_limited(
        {"status": "allowed", "overage_status": "allowed"}) is False
    # allowed, no overage fields at all → benign.
    assert _tv.rate_limit_is_limited({"status": "allowed"}) is False
    # allowed but using overage (overage still allowed) → benign.
    assert _tv.rate_limit_is_limited(
        {"status": "allowed", "is_using_overage": True,
         "overage_status": "allowed"}) is False
    # status not allowed → limited.
    assert _tv.rate_limit_is_limited({"status": "rejected"}) is True
    assert _tv.rate_limit_is_limited({"status": "allowed_warning"}) is True
    # allowed but overage exhausted → limited.
    assert _tv.rate_limit_is_limited(
        {"status": "allowed", "overage_status": "rejected"}) is True
    # No status at all → can't confirm benign, treat as limited.
    assert _tv.rate_limit_is_limited({}) is True
    # Case/whitespace insensitive.
    assert _tv.rate_limit_is_limited({"status": "  ALLOWED  "}) is False


def test_rate_limit_window_label():
    assert _tv.rate_limit_window_label("five_hour") == "5-hour"
    assert _tv.rate_limit_window_label("seven_day") == "7-day"
    assert _tv.rate_limit_window_label("") == ""
    assert _tv.rate_limit_window_label(None) == ""
    # Unknown identifiers fall back to a readable form.
    assert _tv.rate_limit_window_label("one_minute") == "one minute"


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


def test_subagent_index_includes_description_and_prompt():
    from nightdesk.transcript_view import subagent_index
    events = [
        {"type": "subagent", "phase": "started", "task_id": "k1",
         "tool_use_id": "A", "subagent_type": "Explore",
         "description": "explore the codebase",
         "prompt": "find all Python files"},
        # A progress phase overwrites description with transient activity and
        # carries no prompt; the row must keep the stable started values.
        {"type": "subagent", "phase": "progress", "task_id": "k1",
         "tool_use_id": "A", "subagent_type": "Explore",
         "description": "Reading src/nightdesk/db/models.py"},
        {"type": "subagent", "phase": "notification", "task_id": "k1",
         "tool_use_id": "A", "status": "completed",
         "usage": {"tool_uses": 3, "total_tokens": 500, "duration_ms": 2000}},
    ]
    idx = subagent_index(events)
    assert len(idx) == 1
    row = idx[0]
    # Stable started-phase description, NOT the transient progress activity.
    assert row["description"] == "explore the codebase"
    assert row["prompt"] == "find all Python files"
    assert "detail" in row


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


# --- hidden lines rendered inside <details> (BUG glob-lines) ---------------
#
# When output exceeds the elision threshold the middle lines must appear inside
# the <details> body so clicking "N lines hidden" reveals them.


def test_elide_output_hidden_lines_field_populated():
    lines = [f"l{i}" for i in range(30)]
    e = elide_output("\n".join(lines))
    assert e.hidden == 15
    assert e.hidden_lines == lines[10:25]


def test_elide_output_hidden_lines_empty_when_not_elided():
    e = elide_output("\n".join(f"l{i}" for i in range(5)))
    assert e.hidden == 0
    assert e.hidden_lines == ()
