#!/usr/bin/env bash
set -euo pipefail

input="$(cat)"
file_path="$(printf '%s' "$input" | jq -r '.tool_input.file_path // .tool_response.filePath // empty')"

if ! printf '%s' "$file_path" | grep -Eq '(^|/)src/djlib/.*\.py$|(^|/)tests/.*\.py$'; then
  exit 0
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

if [ -x .venv/bin/pytest ]; then
  pytest_bin=.venv/bin/pytest
else
  pytest_bin=pytest
fi

if output="$("$pytest_bin" -q 2>&1)"; then
  jq -n --arg msg "pytest -q passed after editing $file_path" \
    '{suppressOutput: true, systemMessage: $msg}'
else
  tail_output="$(printf '%s' "$output" | tail -c 4000)"
  jq -n --arg file "$file_path" --arg out "$tail_output" \
    '{decision: "block", reason: ("pytest -q FAILED after editing " + $file + ":\n" + $out), hookSpecificOutput: {hookEventName: "PostToolUse", additionalContext: ("pytest -q FAILED after editing " + $file + ":\n" + $out)}}'
fi
