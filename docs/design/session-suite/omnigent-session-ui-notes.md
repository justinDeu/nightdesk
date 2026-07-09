# Omnigent session-UI source analysis (notes for resident-sessions v2)

Source dive 2026-07-09 against the omnigent clone. Key verified facts:

## Chat vs terminal views
- Chat = structured block stream (ChatPage.tsx -> BlockRenderer.tsx), fed by session SSE, never by PTY.
- Terminal = real xterm.js (TerminalSession.ts ~590 lines, plain class outside React), attached over
  WS /v1/sessions/{sid}/resources/terminals/{tid}/attach.
  Wire: server->client binary = raw PTY bytes into term.write(); client->server binary = keystrokes,
  text JSON = {"type":"resize"}. No pyte, no screen-state diffing — tmux holds state, xterm re-renders.
- Each terminal = its OWN private tmux server (isolated socket). Two transports, same browser protocol:
  pty (fork tmux attach on a PTY) and control (tmux -C %output stream; the DEFAULT — gives native
  browser text selection/copy).

## One session, both views
- View is NOT bound to integration mode. Chat is always available. Terminal-first is a presentational
  conversation label (omnigent.ui="terminal"); native-wrapper behavior is a separate label. A Chat/Terminal
  segmented pill toggles renderings of the SAME session.
- SDK sessions get a terminal too (embedded REPL pane). Native-TUI sessions get structured chat via
  transcript TAILING, not screen scraping: claude_native_forwarder.py (~4400 lines) tails Claude Code's
  transcript/deltas/hooks/subagents and posts them as chat blocks. Web->TUI input = tmux send-keys behind
  a lock. (So Omnigent DOES do the send-keys thing — but only for native mode, and it's their most
  expensive subsystem.)

## Approvals
- SDK mode: options.can_use_tool callback -> parked elicitation -> ApprovalCard inline / ApprovePage.
- Native claude: PreToolUse permission hook shells out to omnigent, suppressing the vendor prompt.
- Others: capture-pane mirroring or protocol-native permission events. All funnel into one
  pending-elicitations index.

## Fidelity + sandbox
- User config loading is a per-agent spec choice (skills_filter -> SDK setting_sources; "all" loads
  ~/.claude skills/CLAUDE.md/hooks). claude-native runs the real CLI on the user's real ~/.claude.
- Sandbox is ON by default even for interactive sessions (bwrap/seatbelt/jobobject), opt-out explicit
  (os_env.sandbox.type: "none"). Note: Omnigent is a multi-user/team product; its default posture
  serves that. nightdesk is single-user on the owner's machine — different calculus.

## Cost tiers for a nightdesk terminal surface
1. Read-only PTY mirror: tmux/PTY per session + one WS route + port TerminalSession.ts (near-verbatim
   reference client). Cheap. Reconnect logic keyed on transport-shaped WS close codes (1006/1001/...)
   vs app codes (4404/4405/4500) — mirror close codes end-to-end if the API proxies frames from the
   worker (worker owns PTY, API shuttles; Omnigent's runner-tunnel is the direct analogue of our split).
2. Owner-writable terminal + user shells: add write frames (OWNER-ONLY — raw keystrokes carry no
   identity) + a shell-launch affordance into the session workspace.
3. Native-TUI harness with transcript forwarding: ~4400-line tax; buys only vendor-TUI fidelity.
   NOT worth it — our SDK streaming client already gives structured items + can_use_tool approvals.

Recommended: keep SDK streaming as the structured source of truth; optionally add tier 1->2 as a
parallel surface later. This is exactly Omnigent's dual-surface model for SDK sessions.
