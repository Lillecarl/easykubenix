# Useful commands
- `direnv exec . pytest`
- `direnv exec . pyright ekn`
- `direnv exec . ruff check --fix`
- `direnv exec . ruff check --config ruff-strict.toml`
- `direnv exec . ruff format --check`
- `direnv exec . nixfmt $(jj file list | grep '\.nix$')`
- `nix build --file ./checks.nix all` — the doc-example gates plus this
  repository's own (`ekn-sandbox`, `nixfmt`, `validation-e2e`,
  `bootstrap-validation-e2e`), the same thing CI builds. Nothing
  in this repository is gated behind a flake command; `nix build --file`,
  `nix run --file` and `nix-shell --run` must always work.
- `nix build --file ./checks.nix validation-e2e` — the validation gate: a
  real etcd and kube-apiserver on 127.0.0.1, applying the full manifest set,
  inside the Nix build sandbox (issue #16). In `checks.all`; it costs ~10s.
  `nix run --file ./nix packages.validationScript` reaches the same harness
  for a debug run — set `validation.debug = true` in the module to see the
  control plane's output.
- `nix build --file ./checks.nix bootstrap-validation-e2e` — the same harness
  over `docs/examples/bootstrap`'s nested instance (ArgoCD's CRDs, then the
  `Application` that needs them). A separate script because
  `kubernetes.generated` excludes a GitOps target's submodule objects by
  design, so the gate above can never cover them. Also in `checks.all`.
- `nix build --file ./checks.nix tofu-render` — the OpenTofu gate. Renders a
  `class = "tf"` deployment unit, diffs `config.tf.json` against a literal,
  then runs `tofu init` and `tofu validate` over it inside the build sandbox.
  The sandbox is the point: no network there means `tofu.providers` has to
  pin the provider through the store. In `checks.all`; see `nix/tofu`.
- `nix build --file ./checks.nix tofu-registry` — providers from the OpenTofu
  registry rather than nixpkgs, `tofu init` in the sandbox. Outside
  `checks.all` because it fetches a ~424M index; run it when you touch
  `easykubenix/lib/tofuRegistry.nix`. See `nix/tofu/registry.nix`.
- `nix build --file ./checks.nix kubeapply` — the apply gate. It boots a
  single-node kubeadm cluster under User-Mode Linux and runs
  `ekn _applyManifest` inside it, so it answers what the two gates above
  cannot: whether a CRD becomes Established, whether a workload runs, and
  what a prune deletes. Also outside `checks.all`, because a control plane is
  minutes of CPU. Run it when you touch `ekn.apply`. See `nix/kubeapply`.

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

# Profiling

Three instruments, three blind spots. Pick by what you are asking.

- `EKN_PROFILE=pyinstrument ekn kubeapply ...` — wall clock, sampled,
  async-aware. The only one that says what a command is *waiting for*: it
  keeps an `await` in the frame that issued it. Use for `kubeapply`, `deploy`,
  anything network-bound. `EKN_PROFILE_FILE` (default `ekn.profile.html`),
  `EKN_PROFILE_INTERVAL` (default 0.001 — raise it for a long run).
- `EKN_PROFILE=1` — cProfile, deterministic, `pstats`. Call counts for
  CPU-bound Python. It has one frame for every wait: measured `epoll.poll` at
  8.108s of an 11.039s render, which is honest and useless for a deploy.
- `NIX_COUNT_CALLS=1 NIX_SHOW_STATS=1 NIX_SHOW_STATS_PATH=x.json nix eval ...`
  — per-function evaluator call counts. Deterministic under load, where a
  wall clock is not.

**`pyinstrument` is not in the release build**, by design — a profiler
belongs where you measure from, not in a closure a cluster fetches. A
consumer repository whose shell takes `easykubenix.passthru.ekn` gets the
"needs the `profile` extra" line; take `easykubenix.passthru.eknDevEnv`
instead for a shell that can profile.

**Start it inside the event loop.** pyinstrument records the async context it
started in, so a profiler started around `anyio.run` charges the whole wait
to the loop's selector and nothing to the frame that awaited — measured 72%
of a real `clusterdiff` in one `selectors.py:select`. `main` wraps
`command.run()` for that reason; `tests/test_profile.py` holds both arms.

**A call count is not a time.** Measured: removing ~30% of a render's
evaluator calls bought 3.1% of the stage and 2.2% of the wall clock. State a
saving in seconds or say "calls".

**Neither Python profiler sees the evaluator.** nanopynix runs it in a worker
process, so time inside a `to_python` call is marshalling and waiting. About
2.8s of an 8.2s stage is attributed by nothing — primops included, YAML
parsing among them.

Measure on a warm store, or a first import-from-derivation build lands inside
the number and reads as evaluation.

# Issues

Issues go to the GitHub issue tracker, `gh issue create --repo
Lillecarl/easykubenix`. Every nixidae project works this way.

Do not use `git-bug` here. It is solid-kubernetes' tracker, for a repository
that has no GitHub one, and reaching for it in a nixidae project puts an issue
somewhere nobody else looks: `git-bug` keeps entities in `refs/bugs/*`, which
no clone fetches by default and no ordinary `git push` publishes. An issue
filed there is invisible to everyone including the person who asked for it.

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
- Concurrency goes through `anyio`, not `asyncio`: `anyio.run_process` and
  `anyio.open_process`, `anyio.sleep`, `anyio.Lock`, `anyio.Event`,
  `anyio.fail_after`/`move_on_after`, `anyio.to_thread.run_sync(...,
  abandon_on_cancel=True)`, `anyio.get_cancelled_exc_class()`. `ruff-anyio.toml`
  bans each replaced name and says what to use. `kr8s.asyncio` is a module path
  and not one of them.
- Background work belongs to a task group. There is no detached task to keep a
  reference to, and a group cancels every sibling when a child raises — so
  per-request work that may fail needs a wrapper that keeps `Exception` inside.
- For a duration, use `time.monotonic()`. For a value a cancel scope is measured
  against, use `anyio.current_time()`, which is the event loop's own clock.
- Ending a process you hold across method calls needs
  `with anyio.CancelScope(shield=True):` around the whole teardown, bounded by
  `move_on_after`. A cancel scope re-delivers cancellation at every checkpoint,
  so an unshielded `await process.wait()` in a `finally` never finishes and the
  child survives. See `ekn.validation.terminate_process`.
- Do not hide unexpected failures with `except Exception: pass`. Log unexpected
  exceptions. Use `contextlib.suppress(...)` only for expected ignored
  exceptions, with a comment explaining why they are safe to ignore.
- Use `rich.traceback.install(show_locals=True)` in CLI entry points for
  readable tracebacks.
- Use `structlog` for all logging. Use `_log.info()` for progress messages,
  `_log.error()` for errors. Configure at module level with `_log = structlog.get_logger()`.

# Call the library, not its CLI

Before writing `subprocess`, `shellous` or a `runCommand` that shells out,
check what the project already depends on. A binding exists for most of what
these repositories drive.

- **Nix: nanopynix.** Never shell out to the `nix` CLI from Python.
  `Session()`, `session.store(uri=...)`, `store.is_valid_path`,
  `store.query_path_info`, `store.copy_closure`, `session.settings()`. See
  `eval.py`'s `push_closure_to_store` and `storecheck.py` for the shape.
- **Kubernetes: kr8s.** No raw HTTP. It is the umbrella's fork, which takes
  only changes meant for upstreaming (issue #29). Nothing here runs kr8s' own
  suite -- it needs a pip venv and a kind cluster -- so run it before landing
  a change to the fork. A change that passes these 568 tests can still be
  wrong: server-side apply was.

A CLI answers in exit codes and text you have to parse, loses the detail that
makes an error actionable, and needs the binary on `PATH` at run time. A
binding keeps distinct answers distinct: `is_valid_path` tells absent from
unreachable, an exit code does not.

Grep the dependency list before concluding there is no binding.

# Do not declare an option for what the tool can read

A Nix option that restates something `ekn` can find at run time is a second
copy that drifts. Ask: could the program find this itself when it runs? Then
it should.

**Unless what it can read is not what you mean.** `ekn.assertCached` is a
`listOf str`, not a `bool` over the substituters `ekn` could read from Nix,
and the reason is the point of the rule rather than an exception to it: the
machine that deploys is not the machine that fetches. CI's `nix.conf` names
three caches; a node carries one. Reading the deployer's list asks a superset
and passes a path no node can fetch -- the exact failure the check exists to
catch, and it fails open.

So: the tool reads what describes *itself*. Anything describing another
machine is data, and data belongs in an option.

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
