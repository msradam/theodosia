# Contributing to Theodosia

Thanks for your interest. Theodosia is a thin adapter that mounts
[Apache Burr](https://github.com/apache/burr) Applications as
[FastMCP](https://github.com/jlowin/fastmcp) servers, so most contributions are
small and focused.

## Development setup

Theodosia uses [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/msradam/theodosia
cd theodosia
uv sync --all-extras
git config core.hooksPath .githooks
```

## Quality gates (the same ones CI runs)

`scripts/check.sh` runs every gate in CI's order: static checks first, then
the test suite. Pass `--fast` to skip the tests.

```bash
scripts/check.sh
```

The individual gates, if you want one at a time:

```bash
uv run ruff format --check .    # format
uv run ruff check .             # lint, docstring style, annotations
uv run mypy src/theodosia       # type-check (strict)
uv run pytest                   # tests (smoke tests are opt-in: -m smoke)
uv run bandit -r src/theodosia --severity-level medium   # SAST
uvx vulture                     # dead code (whitelist: vulture_whitelist.py)
uvx complexipy src/theodosia -mx 15   # cognitive complexity cap
uvx detect-secrets scan src/ tests/ examples/ --all-files --baseline .secrets.baseline
uvx pip-audit                   # dependency vulnerabilities
```

Mutation testing on the audit ledger is not part of the per-PR gates (it is
slow); CI runs it weekly, and you can run it locally:

```bash
uv run mutmut run     # macOS: PYTHONPATH=scripts/mutmut_macos_shim uv run mutmut run
```

All tool configuration lives in `pyproject.toml`. The commands above take no
flags that are not paths.

## Conventions

- The four-tool STEP surface and the Burr `Application` boundary are the stable
  architecture. New capability should pass through `mount()`, not widen the tool
  surface.
- Tests are required for behavior changes. The suite is hermetic: demos that
  call an LLM or shell out have a monkeypatchable indirection, so the suite runs
  without a model runtime or network.
- Voice: plain declarative prose, no em dashes, no marketing adjectives, no AI
  co-author trailers in commits or PRs.
- Keep comments to the non-obvious "why"; let names carry the "what".

## Code style

Naming follows PEP 8 and the Google Python Style Guide: modules `lowercase`,
classes `CamelCase`, functions and variables `lower_case`, module constants
`UPPER_CASE`, private helpers with a leading underscore. Call things what they
are; name length grows with scope.

Docstrings are Google style (ruff's `D` rules enforce it). Every module opens
with a docstring stating its purpose, not its contents. Every public function
and method gets one whose first line is a single imperative sentence; add
`Args:` and `Returns:` for non-trivial signatures and `Raises:` whenever the
function raises intentionally. An inaccurate docstring is worse than none.

`src/theodosia` passes `mypy --strict`. Use `from __future__ import
annotations`, the `X | Y` union syntax, and parameterized generics
(`Application[Any]`, `Callable[..., Any]`). `Any` is acceptable at the Burr and
FastMCP boundaries, whose stubs are incomplete; a `type: ignore` must name its
error code and the stub gap that forces it.

Functions stay at or under cognitive complexity 15 (complexipy gates CI).
Reserved comment markers, each with a reason next to it:

- `# COMPLEXITY: <reason>` justifies a radon grade-C function.
- `# nosec <check-id>  # <rationale>` suppresses a bandit finding.
- `# pragma: no mutate` marks a line whose only mutations are
  behavior-equivalent.
- `# TODO: (@handle) <what>, <why deferred>` is actionable and owned.

No commented-out code in `src/` (ruff's `ERA` enforces it); delete it and let
git history remember.

## Tests

`tests/` is flat, one file per feature. The coffee-order example is the
canonical test FSM; conftest puts `examples/` on `sys.path`.
`src/theodosia/ledger.py` is mutation-tested: every logical branch needs a
killer in `tests/test_ledger_chain.py` or `tests/test_property_ledger.py`, and
both files must stay free of FastMCP clients (mutmut forks worker processes,
and forking a threaded process segfaults on macOS).

## Pull requests

Describe the change and why. Link an issue if one exists. CI must be green
(every job, including the lint gate's vulture, complexipy, and detect-secrets
steps), coverage for the modules you touched should be at or above 85%,
`ledger.py` stays at 100% with zero surviving mutants, and a maintainer review
is required before merge. By contributing you agree your work is licensed
under Apache 2.0.

## Security

Do not open public issues for vulnerabilities. See [SECURITY.md](SECURITY.md).
