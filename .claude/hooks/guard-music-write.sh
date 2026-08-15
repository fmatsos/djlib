#!/usr/bin/env bash
set -euo pipefail

input="$(cat)"
cmd="$(printf '%s' "$input" | jq -r '.tool_input.command // empty')"

if ! printf '%s' "$cmd" | grep -q '/music'; then
  exit 0
fi

if printf '%s' "$cmd" | grep -Eq '(^|[^0-9])>{1,2}[^&]|\brm\b|\bmv\b|\bcp\b|\btee\b|\bsed\b[^|]*-i|\btouch\b|\bchmod\b|\bchown\b|\bdd\b|\bshred\b|\bmkdir\b|\brmdir\b|\btruncate\b|\bexiftool\b[^|]*-[A-Za-z0-9]+='; then
  jq -n --arg cmd "$cmd" \
    '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "deny", permissionDecisionReason: ("djlib guard: this command looks like it would write under /music, the read-only DJ source archive. djlib must never mutate the source. Blocked command: " + $cmd)}}'
  exit 0
fi

exit 0
