# SPDX-License-Identifier: MIT
"""`--confirm-context` is removed, and says so.

Declared and refused rather than deleted. Deleting it gives argparse's
`unrecognized arguments` -- loud and non-zero, and pinned by
`test_exit_code.py`, but silent about what replaced it. The operator most
likely to type this flag is the one who typed it yesterday.

Why it went: it compared the operator's current kubectl context against a
name. That cannot see a kubeconfig supplied by `--kubeconfig-from-tofu`, and
it cannot see a `KUBECONFIG` that was never set for `ekn` at all -- which is
how an environment reached the wrong cluster while every `kubectl` in the same
session was pointed correctly. `ekn.clusterUid` asks the cluster which one it
is.
"""

from __future__ import annotations

from typing import Any

import pytest

from ekn import cli


def _command(**kwargs: Any) -> cli.KubeApply:
    return cli.KubeApply(file=".", **kwargs)


async def test_passing_it_refuses_and_names_the_replacement(monkeypatch: pytest.MonkeyPatch) -> None:
    async def never(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("nothing should run: the refusal is before every step")

    monkeypatch.setattr(cli, "evaluate_kubeapply_config", never)

    with pytest.raises(SystemExit) as caught:
        await _command(confirm_context="nixlab2").run()

    message = str(caught.value)
    assert "ekn.clusterUid" in message, "a removal notice that does not name the replacement is a dead end"
    assert "removed" in message


async def test_not_passing_it_runs_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The positive control. Without it the refusal above passes against a
    `run` that refuses unconditionally."""

    async def stop(*_args: Any, **_kwargs: Any) -> Any:
        raise SystemExit("evaluation reached")

    monkeypatch.setattr(cli, "evaluate_kubeapply_config", stop)

    with pytest.raises(SystemExit, match="evaluation reached"):
        await _command().run()


def test_it_is_still_declared() -> None:
    """Deleting the declaration is the change this file exists to catch: it
    turns a sentence naming `ekn.clusterUid` into `unrecognized arguments`."""
    assert "confirm_context" in cli.KubeApply.specs
