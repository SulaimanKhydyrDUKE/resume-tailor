#!/usr/bin/env bash
# Stop hook for the agent driver (see .claude/skills/chrome-apply/SKILL.md).
#
# While the sentinel file .claude/keep-going exists, Claude's turn is not
# allowed to end: the hook answers the Stop event with a block and the
# reason tells the agent to call assist_next and work the next batch. The
# sentinel is created by the agent when it starts a driving session and
# deleted by it when assist_next returns nothing or the user says stop.
# A counter caps the blocks (KEEP_GOING_MAX_BLOCKS, default 30) so a
# forgotten sentinel can never pin a session forever; at the cap the hook
# removes the sentinel itself and lets the turn end.
#
# Stdin is the Stop event's JSON; it is not needed. Stdout is JSON for
# Claude Code: {"decision":"block","reason":...} to keep going, or nothing
# to let the turn end. Exit 0 always — a failing hook would be reported as
# an error, not as a block.
set -u
ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
SENTINEL="$ROOT/.claude/keep-going"
COUNT="$ROOT/.claude/keep-going.count"
MAX="${KEEP_GOING_MAX_BLOCKS:-30}"

if [ ! -f "$SENTINEL" ]; then
  rm -f "$COUNT"
  exit 0
fi

n=$(cat "$COUNT" 2>/dev/null || echo 0)
case "$n" in ''|*[!0-9]*) n=0 ;; esac
n=$((n + 1))
printf '%s\n' "$n" > "$COUNT"

if [ "$n" -gt "$MAX" ]; then
  rm -f "$SENTINEL" "$COUNT"
  printf '{"systemMessage":"keep-going: %s blocks reached; the sentinel was removed and this turn may end. Start again with /chrome-apply when ready."}\n' "$MAX"
  exit 0
fi

reason="The driver queue is not finished (keep-going block $n of $MAX). Do not stop. Open the next posting (tier 1 first, then newest; assist_next for the held queue) and keep applying in Chrome per .claude/skills/chrome-apply. Log any unanswerable question with note_pending and move on; never wait for approval. Delete .claude/keep-going only when assist_next returns no postings or the user has told you to stop; then end the turn with a short report."
jq -cn --arg r "$reason" '{decision: "block", reason: $r}'
exit 0
