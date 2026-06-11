#!/usr/bin/env bash
# Run the full local quality suite — the same gates CI runs, in the same
# order (fast static checks first, then tests). Exits nonzero on the first
# failing gate. Mutation testing is NOT included (slow; run
# `PYTHONPATH=scripts/mutmut_macos_shim uv run mutmut run` separately, or
# let the weekly CI job do it).
#
# Usage:  scripts/check.sh [--fast]
#   --fast   static checks only; skip the test suite.

set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

step() { printf '\n\033[1m── %s\033[0m\n' "$1"; }

step "ruff format --check"
uv run ruff format --check .

step "ruff check"
uv run ruff check .

step "mypy (strict baseline from pyproject)"
uv run mypy src/theodosia

step "bandit (medium+)"
uv run bandit -r src/theodosia --severity-level medium -q

step "vulture (dead code)"
uvx vulture

step "complexipy (cognitive complexity <= 15)"
uvx complexipy src/theodosia -mx 15

step "detect-secrets (vs audited baseline)"
uvx detect-secrets scan src/ tests/ examples/ --all-files --baseline .secrets.baseline

step "pip-audit (dependency vulnerabilities)"
uvx pip-audit --quiet || { echo "pip-audit found vulnerable dependencies"; exit 1; }

if [[ "${1:-}" != "--fast" ]]; then
  step "pytest (full suite with coverage)"
  uv run pytest -q --cov=theodosia --cov-report=term-missing
fi

printf '\n\033[1;32mAll gates green.\033[0m\n'
