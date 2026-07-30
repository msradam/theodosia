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

# Pinned: complexipy 6.0 rescored the same code higher (the 5.x cap of 15
# corresponds to 18 under 6.x). Unpinned, CI flips red on tool releases with
# zero code change. Bump the pin and recalibrate the cap together.
step "complexipy (cognitive complexity <= 18 under 6.x scoring)"
uvx complexipy@6.2.0 src/theodosia -mx 18

step "detect-secrets (vs audited baseline)"
uvx detect-secrets scan src/ tests/ examples/ --all-files --baseline .secrets.baseline

step "pip-audit (dependency vulnerabilities)"
# --quiet was removed from pip-audit; bare invocation matches ci.yml.
uvx pip-audit || { echo "pip-audit found vulnerable dependencies"; exit 1; }

if [[ "${1:-}" != "--fast" ]]; then
  step "pytest (full suite with coverage)"
  uv run pytest -q --cov=theodosia --cov-report=term-missing
fi

printf '\n\033[1;32mAll gates green.\033[0m\n'
