#!/usr/bin/env bash
# Fail if the tree contains private infrastructure details that must not be
# published. Rules: CONTRIBUTING.md ("Public repo hygiene").
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

status=0

check() {
  local pattern="$1" reason="$2" matches rc=0
  matches=$(git grep -nIE "$pattern" -- . ':!scripts/check-public-hygiene.sh') || rc=$?
  if [[ "$rc" -eq 0 ]]; then
    printf '%s\n' "$matches"
    printf '  -> %s\n' "$reason"
    status=1
  elif [[ "$rc" -ne 1 ]]; then
    echo "public hygiene: git grep failed (exit $rc) for pattern: $pattern" >&2
    exit 2
  fi
}

# No \b: POSIX ERE (macOS git grep) treats it literally; spell the boundary out.
check 'jspanos/(hle|hle-lab|galaxy)([^A-Za-z0-9_.-]|$)' \
  "private repository reference; write 'tracked privately' instead"
check 'hle\.orchistrator' \
  "private repository reference"
check '192\.168\.2\.[0-9]' \
  "maintainer LAN address; use 192.0.2.x (RFC 5737) or 192.168.1.x examples"
check 'claude\.ai/code/session_' \
  "session URL"

if [[ "$status" -eq 0 ]]; then
  echo "public hygiene: OK"
fi
exit "$status"
