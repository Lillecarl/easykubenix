# Useful commands
- `direnv exec . pytest`
- `direnv exec . pyright ekn`
- `direnv exec . ruff check --fix`
- `direnv exec . ruff check --config ruff-strict.toml`
- `direnv exec . ruff format --check`
- `direnv exec . nixfmt $(jj file list | grep '\.nix$')`
- `nix build --file ./checks.nix all` — the doc-example gates plus this
  repository's own (`ekn-sandbox`, `nixfmt`), the same thing CI builds. Nothing
  in this repository is gated behind a flake command; `nix build --file`,
  `nix run --file` and `nix-shell --run` must always work.

Every `.nix` file is nixfmt'd, and `checks.nixfmt` enforces it over the whole
tree — it filters the repository down to `*.nix` by dropping dot-directories,
`result` symlinks and `__pycache__`, so a new directory is covered without
anyone extending a list. Use the `nixfmt` from the dev shell rather than one
off your PATH: it is the version the gate runs, and nixfmt's output moves
between releases.

`ekn/` is this repository's alone. nanopynix has no ekn dependency at all: no
copy of the source, no `pynix ekn` subcommand, no gate over it. It is an
ordinary third-party dependency of ours, imported through its public API only.
Do not reintroduce an import of anything private from it (`nanopynix._*`).

`ekn` does not opt into beartype, so nothing here resolves an annotation at
runtime except pydantic. That is why `TC001`/`TC002` are selected and why
`ruff-strict.toml` sets `runtime-evaluated-base-classes` to
`["pydantic.BaseModel"]` — without it those rules will happily move a live
model field type into an `if TYPE_CHECKING:` block and break imports.

# Version control

This repository uses Jujutsu (`jj`) for version control. Prefer `jj` commands
for status, diffs, history, and commit/change inspection. Do not assume a Git
workflow or run Git porcelain commands such as `git status`, `git diff`,
`git commit`, `git checkout`, or `git reset` unless the user explicitly asks for
Git or a tool requires Git-specific plumbing.

Run pytest commands so the complete output is preserved. Do not pipe pytest
directly into `tail`, `head`, `grep`, or similar filters. If you need a short
live summary, use `tee` first, for example:

- `direnv exec . pytest 2>&1 | tee /tmp/pytest.log | tail -n 80`

The saved log is the source of truth. Use the short live summary only to decide
what to inspect next, then query `/tmp/pytest.log` for the full failure context.

# Python coding conventions

- Use `from __future__ import annotations` in Python modules that define or use
  type annotations.
- Do not use string type hints such as `"Store"`. Use future annotations and
  `if TYPE_CHECKING:` imports instead.
- Keep imports at the top of the file. Lazy imports inside functions or methods
  are forbidden unless they are absolutely necessary to break a circular import
  cycle; prefer moving shared types to a neutral module over lazy imports.
- Import ordering:
  1. `from __future__ import annotations`
  2. standard library imports
  3. third-party imports
  4. local `ekn` imports
  5. `if TYPE_CHECKING:` block containing only type-only imports
  6. module constants
  7. code
- When re-exporting a name from another module, use the explicit re-export
  pattern `from module import Name as Name`. Consolidate related re-exports into
  one multi-line import block.
- Do not use `assert` statements outside `tests/`. For runtime validation, use
  explicit `if ...: raise ...`. To satisfy type checkers, prefer local variable
  aliasing or explicit `if value is None: raise ...` checks.
- Do not use `asyncio.get_event_loop()`. Use `asyncio.get_running_loop()` inside
  async code. For timestamps, use `time.monotonic()`.
- Keep a strong reference to background tasks created with
  `asyncio.create_task()`, for example in an instance `set` or `list`.
- Do not hide unexpected failures with `except Exception: pass`. Log unexpected
  exceptions. Use `contextlib.suppress(...)` only for expected ignored
  exceptions, with a comment explaining why they are safe to ignore.
- Use `rich.traceback.install(show_locals=True)` in CLI entry points for
  readable tracebacks.
- Use `structlog` for all logging. Use `_log.info()` for progress messages,
  `_log.error()` for errors. Configure at module level with `_log = structlog.get_logger()`.

# Test Failure Discipline

Do not assume failing tests are unrelated, flaky, or pre-existing.

When a test fails after your changes, your default assumption must be:

> "My change caused or exposed this failure."

You may only call something a pre-existing issue after proving it with evidence.

## Required procedure for failing tests

When any test fails:

1. Re-run the exact failing test command to confirm the failure.
2. Inspect the failure carefully before making claims.
3. Check whether your recent changes could plausibly affect the failing behavior.
4. Use `jj diff --git` or `jj show --git` to review every file you changed.
5. If you believe the failure is unrelated, verify that claim by either:
   - reverting your changes and showing the test still fails, or
   - running the same test on a clean baseline branch/commit, or
   - finding an existing failing CI/test record predating your work.

Without one of those checks, do not say:
- "This is pre-existing"
- "The tests are broken"
- "This is unrelated"
- "This is likely flaky"
- "The failure is outside the scope"

Instead say:

> "I have not proven this is unrelated yet. I will continue debugging under the assumption my change caused it."

## Pytest output discipline

Do not pipe pytest output directly through `head`, `tail`, `grep`, `sed`, `awk`,
or similar filters. Pytest failure output is evidence. Truncating or filtering
it often hides the traceback, captured logs, fixture setup errors, warnings,
parametrization IDs, or the first failure that explains the rest.

`tail` is especially risky. The last lines of pytest output are often only the
short summary, not the failure cause. Do not use `tail` as the only record of a
pytest run.

Forbidden default patterns include:

- `pytest ... | head`
- `pytest ... | tail`
- `pytest ... | grep ...`
- `pytest ... 2>&1 | tail -n ...`

Allowed pattern:

- `pytest ... 2>&1 | tee /tmp/pytest.log | tail -n 80`

This is allowed because `tee` preserves the complete output before `tail`
shortens the live display. After this command, inspect `/tmp/pytest.log`; do not
debug or report from the tailed output alone.

Only filter pytest output after the complete output has already been preserved.
You must state the specific reason before or alongside the command. Valid reasons
include finding which test failed, searching a previously captured full log,
extracting one known failure from a very large log after the full failure has
already been inspected, or checking for one exact warning/error string after the
underlying failure is understood.

If pytest output is too large to read comfortably:

- Prefer running the smallest relevant test directly with `pytest path::test`.
- Prefer pytest's own controls such as `-x`, `--maxfail=1`, or increased
  verbosity when they preserve the relevant failure context.
- If you need post-processing, first preserve the complete output with `tee`,
  then inspect or search the saved log.

If you accidentally truncated or filtered a failing pytest run, especially with
`tail`, and did not save the full output with `tee`, do not draw conclusions
from that output. Re-run the failing command and preserve the full output before
debugging or reporting the failure.

## Never paper over failures

Do not modify tests just to match broken behavior.

Only update tests when:
- the intended behavior changed,
- the old test expectation is demonstrably obsolete,
- and the reason is explained clearly.

Do not weaken assertions, skip tests, delete coverage, or loosen error handling to make tests pass unless explicitly justified.

## Debugging expectations

Prefer small, evidence-driven steps:

- reproduce the failure
- isolate the smallest failing test
- inspect the relevant code path
- add temporary logging only if it helps identify the issue
- remove temporary logging before finishing
- make the smallest fix that addresses the root cause
- re-run the failing test
- then run the relevant broader test set

## Reporting failures

When reporting a test failure, include:

- the exact command run
- the exact failing test name
- the error or assertion message
- whether the failure was reproduced after your change
- why your fix addresses the root cause

If you believe a failure is pre-existing, include the proof.
A suspicion is not proof.
