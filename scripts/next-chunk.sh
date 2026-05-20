#!/usr/bin/env bash
# next-chunk.sh — verify the current chunk, commit, launch Claude on the next.
#
# Designed for the chunked implementation plan at
# ~/.claude/plans/yeah-and-lets-really-zazzy-aurora.md. Each invocation
# closes out the current chunk (install + test + commit) and opens a fresh
# Claude session focused on the next one.

set -euo pipefail

print_usage() {
    cat <<'EOF'
Usage: scripts/next-chunk.sh <next_chunk_num> [--ralph] [--commit-msg "..."] [--no-commit]

Steps:
  1. pip install -e ".[dev]" in the project venv
  2. pytest against $TEST_PATTERN (default: tests/test_validation.py)
  3. git commit any pending changes (interactive prompt unless --commit-msg given)
  4. exec `claude`, printing a kickoff prompt for the next chunk

Env vars (override defaults):
  PLAN_FILE      $HOME/.claude/plans/yeah-and-lets-really-zazzy-aurora.md
  TEST_PATTERN   tests/test_validation.py
  VENV           .venv

Flags:
  --ralph              kick off the next chunk via /ralph-loop instead of a plain prompt
  --commit-msg "..."   commit message (skip the interactive prompt; use 'skip' to skip commit)
  --no-commit          leave working tree alone (no commit step)

Examples:
  scripts/next-chunk.sh 2 --commit-msg "refactor: restructure to src/ layout"
  scripts/next-chunk.sh 4 --ralph
EOF
}

NEXT_CHUNK=""
USE_RALPH=0
COMMIT_MSG=""
NO_COMMIT=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --ralph) USE_RALPH=1; shift ;;
        --commit-msg) COMMIT_MSG="$2"; shift 2 ;;
        --no-commit) NO_COMMIT=1; shift ;;
        -h|--help) print_usage; exit 0 ;;
        -*) echo "unknown flag: $1" >&2; print_usage >&2; exit 1 ;;
        *) NEXT_CHUNK="$1"; shift ;;
    esac
done

if [[ -z "$NEXT_CHUNK" ]]; then
    print_usage >&2
    exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLAN_FILE="${PLAN_FILE:-$HOME/.claude/plans/yeah-and-lets-really-zazzy-aurora.md}"
TEST_PATTERN="${TEST_PATTERN:-tests/test_validation.py}"
VENV="${VENV:-.venv}"

cd "$ROOT"

if [[ ! -x "$VENV/bin/pip" ]]; then
    echo "[FAIL] $VENV/bin/pip not found — is the venv at $VENV/?" >&2
    exit 1
fi

echo "[1/4] pip install -e \".[dev]\""
"$VENV/bin/pip" install -q -e ".[dev]"

echo "[2/4] pytest $TEST_PATTERN"
read -ra TEST_ARGS <<< "$TEST_PATTERN"
if ! "$VENV/bin/pytest" -q "${TEST_ARGS[@]}"; then
    echo "[FAIL] tests failed — fix before continuing" >&2
    exit 2
fi
echo "[OK]   tests pass"

echo "[3/4] commit"
if [[ $NO_COMMIT -eq 1 ]]; then
    echo "       skipped (--no-commit)"
elif [[ -z "$(git status --porcelain)" ]]; then
    echo "       tree clean, nothing to commit"
else
    git status --short | sed 's/^/       /'
    if [[ -z "$COMMIT_MSG" ]]; then
        read -r -p "       commit message (or 'skip'): " COMMIT_MSG
    fi
    if [[ "$COMMIT_MSG" != "skip" && -n "$COMMIT_MSG" ]]; then
        git add -A
        git commit -m "$COMMIT_MSG"
        echo "[OK]   committed"
    else
        echo "       skipped"
    fi
fi

if [[ ! -f "$PLAN_FILE" ]]; then
    echo "[FAIL] plan file not found at $PLAN_FILE — set PLAN_FILE env var" >&2
    exit 1
fi

echo "[4/4] launching Claude on chunk $NEXT_CHUNK"

if [[ $USE_RALPH -eq 1 ]]; then
    PROMPT="/ralph-loop \"Implement chunk $NEXT_CHUNK from $PLAN_FILE. Output <promise>CHUNK $NEXT_CHUNK DONE</promise> when pytest -q passes.\" --completion-promise \"CHUNK $NEXT_CHUNK DONE\" --max-iterations 12"
else
    PROMPT="Implement chunk $NEXT_CHUNK from $PLAN_FILE. Read the plan first, locate that chunk, do only that chunk. Run pytest -q when relevant tests exist. Report back when done."
fi

cat <<EOF

       paste this prompt in Claude:
       ─────────────────────────────────────────
       $PROMPT
       ─────────────────────────────────────────

EOF

exec claude
