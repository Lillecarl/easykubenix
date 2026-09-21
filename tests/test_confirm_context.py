# SPDX-License-Identifier: MIT
"""`--confirm-context` refuses the one apply it cannot check.

It reads `kubectl config current-context`, the operator's ambient context, and
runs before the kubeconfig is resolved -- so with `--kubeconfig-from-tofu` it
answers about a cluster the apply will not touch. A confident wrong answer is
worse than no check, and it is the same class of mistake `ekn.clusterUid`
exists to end.

Placed where it is, not moved down: the check also guards `ekn.cacheTo`, which
pushes a closure to a remote store before anything reaches a cluster. Moving
the prompt below that would confirm after a side effect.
"""

from __future__ import annotations

from typing import Any

import pytest

from ekn import cli


def _command(**kwargs: Any) -> cli.KubeApply:
    return cli.KubeApply(file=".", **kwargs)


async def test_the_two_flags_together_refuse(monkeypatch: pytest.MonkeyPatch) -> None:
    async def never(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("nothing should run: the refusal is before every step")

    monkeypatch.setattr(cli, "exec_capture", never)
    monkeypatch.setattr(cli, "evaluate_kubeapply_config", never)

    with pytest.raises(SystemExit) as caught:
        await _command(
            confirm_context="nixlab2",
            kubeconfig_from_tofu="infra:kubeconfig",
        ).run()

    message = str(caught.value)
    assert "ekn.clusterUid" in message, "the refusal has to name the check that does work"
    assert "--kubeconfig-from-tofu" in message


async def test_confirm_context_alone_still_reads_the_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """The positive control. Without it the refusal above passes against a
    `--confirm-context` that refuses unconditionally."""
    seen: list[tuple[str, ...]] = []

    async def capture(*args: str, **_kwargs: Any) -> tuple[int, str, str]:
        seen.append(args)
        return 0, "admin@nixlab2\n", ""

    async def stop(*_args: Any, **_kwargs: Any) -> Any:
        raise SystemExit("evaluation reached")

    monkeypatch.setattr(cli, "exec_capture", capture)
    monkeypatch.setattr(cli, "evaluate_kubeapply_config", stop)

    with pytest.raises(SystemExit, match="evaluation reached"):
        await _command(confirm_context="nixlab2").run()

    assert seen == [("kubectl", "config", "current-context")]
