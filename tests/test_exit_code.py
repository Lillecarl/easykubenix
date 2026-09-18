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

import os
import pathlib
import shutil
import subprocess

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent

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


def test_a_failed_git_push_is_not_success(tmp_path: pathlib.Path) -> None:
    """The case reported from a live repository, where `ekn deploy --push`
    was said to fail with `rc=128` and still exit 0.

    It does not, and this pins that. The remote cannot be resolved, so
    `git push` exits 128 and `_git_push` turns it into `SystemExit(1)`.
    The commit has already happened by then, which is the shape that makes
    a wrong exit code dangerous: the branch moved locally, nothing reached
    the remote, and a caller reading only the status would believe the
    deploy landed.

    `--no-verify` and `--no-cache-push` keep this off an apiserver and off
    the network except for the push itself.
    """
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for key, value in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "root"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "https://gitlab.invalid/nope/nope.git"],
        check=True,
    )

    instance = tmp_path / "instance.nix"
    instance.write_text(f"""
        let
          sources = import {_PROJECT_ROOT}/nix/sources.nix;
          pkgs = import sources.nixpkgs {{ }};
        in
        import {_PROJECT_ROOT} {{
          inherit pkgs;
          modules = [
            {{
              ekn.environment = "pushprobe";
              deployment.deployBranch = "probe-deploy";
              deployment.units.app = {{
                path = "app/";
                modules = [ {{ kubernetes.objects.default.ConfigMap.c.data.k = "v"; }} ];
              }};
            }}
          ];
        }}
    """)

    env = {**os.environ, "EKN_REPO": str(repo), "GIT_TERMINAL_PROMPT": "0"}
    exe = shutil.which("ekn")
    assert exe is not None
    result = subprocess.run(
        [exe, "--file", str(instance), "deploy", "--no-verify", "--no-cache-push", "--push", "-m", "probe"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
        check=False,
    )

    assert result.returncode == 1, result.stderr
    assert "rc=128" in result.stderr, "the git exit code is reported, not swallowed"
