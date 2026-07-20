---
# Machine policy — nightdesk-pm parses these every shift. All required unless noted.
project_id: ""                # nightdesk Project UUID
project_slug: ""
api_base: "http://127.0.0.1:8765"
token_hint: "~/.config/nightdesk/tokens/<slug>.token"  # path to the token file, NEVER the token itself
source_path: ""               # absolute repo root ticket runs work in
merge_target: "main"          # branch merges land on; note in prose whether it is ever pushed
default_profile_id: ""        # execution profile UUID for tickets
allowed_profile_ids: []       # optional; empty = default only. Skill-enforced, not server-enforced.
workspace_mode: "git_worktree"
max_parallel_tickets: 2       # skill-enforced per-project cap; never exceed server max_parallel
queued_buffer: 2              # tickets kept queued beyond busy slots
default_priority: 3
bug_priority: 4
gitlab_repo_link_ids: []      # optional; non-empty enables the GitLab intake section
---

# Project profile

Filled per project at setup. nightdesk-pm reads the sections below by heading name; keep the headings, replace the example comments with project truth.

## Identity & design sources

<!-- example: Repo is Godot 4 / GDScript; merge target is local main, never pushed.
     Design canon: _ideation/README.md then 10-master-vision.md §5 roadmap.
     Hazard: the player's live desktop is on display :1 — NEVER launch or click there. -->

## Warm-up

<!-- The mandatory command before ANY test in a fresh or freshly-merged tree.
     example: godot --headless --path . --import   (builds the class cache) -->

## Verify commands (the PM gate)

<!-- Boot/smoke, full suite, and the duplicate-definition grep, as runnable commands.
     example boot: timeout 90 godot --headless --path . --quit-after 40
     example suite: export XDG_DATA_HOME=$(mktemp -d); bash tools/run_tests.sh
     example dup grep: grep -oP '(?<=^func )\w+' scripts/main.gd | sort | uniq -d -->

## Sacred invariants

<!-- Things to grep-verify after every merge that goes near them.
     example: the payout line multiplies each factor exactly once; the reset seam
     requires every persistent feature to declare wipe/keep. -->

## Hot files & review escalation

<!-- Files where semantic conflicts live, and which changes get the strong review model.
     example: computer.gd, main.gd, CLAUDE.md; strong model for anything touching
     currency math or the reset seam; batch small reviews in pairs/triples. -->

## Ticket prompt conventions block

<!-- Verbatim text included in EVERY ticket prompt: read-this-first pointers,
     code conventions, "leave work uncommitted", and the Verification section shape. -->

## Manual verify / playtest loop

<!-- How the PM live-verifies between waves, and any harness for it.
     example: QA bridge on a unique derived port, isolated save dir, screenshot one
     thing after significant merges. Caveat: agents are slow at manual phases —
     never draw pacing conclusions from agent click-speed. -->

## Cadences

<!-- Typical run/review durations and wave sizing, so the PM plans batches.
     example: tickets 20-90 min, reviews 5-15 min, a 5-ticket wave ≈ 3h end to end. -->
