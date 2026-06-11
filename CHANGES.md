# Audit changes — branch `audit/code-quality-2026-06`

One commit per issue from `.analysis/ISSUES.md` (the full register, with
root causes and verification evidence, lives there). Base: `main` @ 882fcc8
(v0.4.2). Suite grew from 844 to 945 passing tests; coverage from 80% to 88%.

## ISSUE-001 — repair the mutation-testing gate (`a42f80e`)

`uv run mutmut run` could not execute at all: the mutants tree held a broken
partial `theodosia` package, the conftest needed `examples/` on disk, and the
codebase_security example shelled out via cwd-sensitive `uv run`.

    pyproject.toml | examples/codebase_security.py | .gitignore
    3 files changed, 21 insertions(+), 3 deletions(-)

## ISSUE-002 — ledger: 100% coverage, 211/211 mutants killed (`c19321a`)

`verify_ledger` now reports corrupt lines as problems instead of raising;
new `tests/test_ledger_chain.py` pins every branch deterministically; new
keyed-chain/foreign-binding/cache-agreement properties; `pragma: no mutate`
on the five utf-8 `open()` calls (locale-equivalent mutants only); a no-op
setproctitle shim stops mutmut's forked workers segfaulting on macOS.

    src/theodosia/ledger.py | tests/test_ledger{,_chain}.py |
    tests/test_property_ledger.py | scripts/mutmut_macos_shim/ | pyproject.toml
    5 files changed, 466 insertions(+), 14 deletions(-)

## ISSUE-003 — vulnerable transitive deps (`e051b6d`)

pyjwt 2.12.1 → 2.13.0 (PYSEC-2026-175/177/178/179), starlette 1.0.0 → 1.2.1
(PYSEC-2026-161). pip-audit clean.

    uv.lock | 1 file changed, 6 insertions(+), 6 deletions(-)

## ISSUE-006 — bandit B108 (`e5a3641`)

`tempfile.gettempdir()` replaces the hard-coded `/tmp` fallback in the
themed-UI build dir. Zero MEDIUM+ findings, no `nosec` needed.

    src/theodosia/_ui.py | 1 file changed, 2 insertions(+), 1 deletion(-)

## ISSUE-004 — mypy --strict across the package (`0b8d637`)

122 errors in 18 files fixed: Burr generics parameterized
(`Application[Any]`, `State[Any]`), bare `Callable`/`dict`/`list` filled in,
Anthropic content blocks narrowed via `isinstance(ToolUseBlock)`,
`ValidationFailed` re-exported explicitly. `[tool.mypy] strict = true` is
the new repo baseline.

    19 files changed, 98 insertions(+), 83 deletions(-)

## ISSUE-005 — decompose `mount()` (`b767978`)

Cognitive complexity 158 → 17 (then ≤15 in ISSUE-007). The 1,255-line body's
closures moved verbatim into `_register_resources` / `_register_personas` /
`_register_step_tool` / `_register_reset_tool` / `_register_fork_at_tool` /
`_register_fork_from_past_tool`, plus shared helpers (`_with_next_guidance`,
`_handle_unknown_action`, `_fork_target_refusal`, the two
`_load_past_state_*` tiers, `_subruns_index`, `_session_coordinates`).
Wire behavior unchanged; full suite green.

    src/theodosia/adapter.py | 1 file changed, 1024 insertions(+), 812 deletions(-)

## ISSUE-007 / ISSUE-008 — complexity to ≤15 everywhere (`45f1045`)

Focused-helper extractions across doctor's runtime probe, `_read_steps`,
`drive_claude`, the graph summary, the coercion middleware, introspection,
the importer, sessions diff/logs rendering, and report sections.
`complexipy -mx 15`: zero findings. The eleven remaining radon grade-C
functions carry `# COMPLEXITY:` justifications naming their inherent
branching.

    16 files changed, 812 insertions(+), 585 deletions(-)

## ISSUE-015 — doctor + upstream gap fixes (`dedc87f`)

`run_checks` emits an INFO finding for graphs with no terminal action;
`UpstreamError` gains `server`/`tool`/`body` attributes and
`UpstreamManager.call` wraps FastMCP `ToolError` so action code can branch
on the upstream error body. Both covered by new tests.

    src/theodosia/doctor.py | src/theodosia/upstream.py |
    tests/test_doctor.py | tests/test_upstream.py
    4 files changed, 104 insertions(+), 2 deletions(-)

## ISSUE-009 / ISSUE-012 — secrets baseline + vulture whitelist (`6d7ea9d`)

`.secrets.baseline` records all nine detect-secrets findings (vuln_demo
fixtures and the security-audit example) audited as false positives.
`vulture_whitelist.py` registers the one src/ false positive (FastAPI
catch-all path parameter). `[tool.vulture]` and `[tool.bandit]` land in
pyproject.

    3 files changed, 235 insertions(+)

## ISSUE-013 — ruff D (google) + ANN + ERA (`66ca252`)

Docstring completeness and google style enforced over src/ (every public
class/method/`__init__` documented; summaries reflowed to one imperative
line). ANN401 ignored with rationale (the `Any`-at-third-party-boundaries
reality); D/ANN scoped out of tests/examples/bench/demos/scripts and
`_experimental`; ERA enforced on src/ and scoped out of prose-heavy trees.

    21 files changed, 2682 insertions(+), 42 deletions(-)

## ISSUE-014 — CI restructure + local entry point (`7ec50df`, `edfaa7e`)

`ci.yml`: one fast lint-and-type gate (ruff, strict mypy, bandit, vulture,
complexipy, detect-secrets) before the 3.11–3.14 test matrix, pip-audit,
gitleaks, and fresh-install jobs; a weekly scheduled mutation job fails on
any survivor. `scripts/check.sh` mirrors the gates locally; CONTRIBUTING
documents the suite, the code standards, and the PR checklist.
codeql/docs/release/scorecard intentionally stay separate workflows
(trigger/permission isolation; rationale in `.analysis/ISSUES.md`).

    .github/workflows/ci.yml | scripts/check.sh | CONTRIBUTING.md
    3 files changed, 177 insertions(+), 24 deletions(-)

## ISSUE-010 / ISSUE-011 — coverage of untested branches (`06939e6`)

62 new tests in `tests/test_audit_gaps.py` and `tests/test_drive_claude.py`:
tracker resolution order, trace reading, Assembly YAML errors, CLI
resolution failures, tracker-log parsing edge cases, sessions
diff/show/ls/watch/logs/verify/render/report surfaces, `run()` exit codes,
and `drive_claude` end-to-end against a scripted fake Anthropic client.
Every module ≥85% except `cli/_app.py` and `_ui.py` (blocking server-launch
loops; covered by fresh-install CI and the smoke suite) and the
out-of-scope `_experimental/`.

    2 files changed, 806 insertions(+)

## Verification (final pass, 2026-06-11)

| Gate | Result |
|---|---|
| `uv run pytest -q` | 945 passed, 6 skipped (smoke deselected) |
| `uv run ruff check src/ tests/` | clean |
| `uv run ruff format --check src/ tests/` | clean |
| `uv run mypy src/theodosia --strict` | 0 errors / 33 files |
| `uv run bandit -r src/theodosia -ll` | 0 findings |
| `uvx vulture` | 0 unwhitelisted findings |
| `uvx complexipy src/theodosia -mx 15` | 0 findings |
| `radon cc -n C` | 11 functions, each with `# COMPLEXITY:` |
| `uvx pip-audit` (full lock export) | no known vulnerabilities |
| detect-secrets vs baseline | no new findings |
| `mutmut run` + `results` | 231 mutants, 0 survived, 0 uncovered |
| Coverage | ledger.py 100%; total 88%; per-module ≥85% except the two documented launch-loop modules |
