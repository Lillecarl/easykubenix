"""`ekn` exits non-zero when it fails.

Issue #21: `ekn` printed a traceback and exited 0, so a `&&` chain carried
on, a CI run went green, and a deploy that did nothing read as success. It
is silent in the dangerous direction -- it claims a state-changing
operation happened when it did not.

Measured on 2026-09-18: every failure path already exits non-zero. Nothing
pinned that, which is why this file exists. An exit code is the one thing
every non-interactive caller reads and the one thing no other test looks
at, so a regression here is invisible until a deploy silently does
nothing.

**The real program, in a subprocess.** Calling `main()` in-process would
not see the failure this guards: the way to reintroduce it is to wrap
`main`'s body in a `try/except` that logs and returns, and an in-process
call has no exit code to be wrong.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pathlib

#: Long enough for a nanopynix session to start and fail.
_TIMEOUT_SECONDS = 180


def _ekn(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the installed `ekn`, whatever it does, and hand back the result.

    Not `sys.executable -c "...main()"`: the console script is
    `sys.exit(main())`, and this has to exercise the same shape a deploy
    does. `check=False` because a non-zero exit is what most of these
    assert.
    """
    exe = shutil.which("ekn")
    # Fails rather than skips. A skipped guard guards nothing, and every
    # path that runs this suite is a dev shell that has `ekn`.
    assert exe is not None, "`ekn` is not on PATH -- run this from `nix develop --file ./shell.nix`"
    return subprocess.run(
        [exe, *args],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
        check=False,
    )


def test_success_is_zero() -> None:
    """The control. Without it every assertion below passes on an `ekn`
    that cannot start at all."""
    assert _ekn("--help").returncode == 0


def test_an_unhandled_exception_is_not_success() -> None:
    """Issue #21's first case, verbatim in shape: `-A` without `-f`, which
    reaches `_resolve_proxy` and raises `ValueError`. The message was
    already right; only the exit code was wrong."""
    result = _ekn("-A", "someattr", "tofu", "destroy", "--target", "x", "--auto-approve")

    assert result.returncode != 0
    assert "specify --file or --flake" in result.stderr


def test_a_reported_failure_is_not_success(tmp_path: pathlib.Path) -> None:
    """The `SystemExit(1)` route rather than the traceback route.

    A configuration that will not evaluate reaches `_report_nix_error`,
    which logs and exits. It stops before anything opens a connection to a
    cluster, which is what makes this safe to run anywhere.
    """
    broken = tmp_path / "broken.nix"
    broken.write_text("{ this is not a nix expression\n")

    result = _ekn("--file", str(broken), "kubeapply")

    assert result.returncode == 1


def test_a_usage_error_is_not_success() -> None:
    """argparse's own exit, kept here so the three ways `ekn` can refuse to
    do something are pinned in one place."""
    assert _ekn("pushcache", "--to", "file:///nowhere").returncode != 0
