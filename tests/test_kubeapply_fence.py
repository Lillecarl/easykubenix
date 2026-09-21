# SPDX-License-Identifier: MIT
"""Where the cluster fence sits inside `ekn kubeapply`.

Not whether it refuses -- `test_clusterfence.py` covers that -- but whether it
refuses *in time*. `ensure_age_identities` creates a Namespace and a Secret,
and `_hold_the_engine` scales Deployments and StatefulSets down. Both run
before a single manifest is applied, so a fence below either of them is a
fence after the damage.

Nothing here reaches Nix or a cluster: the evaluation, the API and every step
around the fence are replaced, and what is asserted is the order of calls.
"""

from __future__ import annotations

from typing import Any

import pytest

from ekn import cli
from ekn.eval import KubeApplyConfigResult

LIVE = "3b2a1f0e-9d8c-4b7a-8e6f-5d4c3b2a1f0e"
OTHER = "00000000-1111-2222-3333-444444444444"


def _config(cluster_uid: str | None) -> KubeApplyConfigResult:
    return KubeApplyConfigResult.model_validate(
        {
            "groups": [{"unit": None, "field_manager": "ekn", "objects": []}],
            "environment": "test",
            "resource_priority": {},
            # Non-empty, so `ensure_age_identities` is reached at all -- the
            # call is guarded by this list being truthy.
            "sops_age_identities": [
                {"namespace": "argocd", "secretName": "sops-age-key"},
            ],
            "handAppliedUnits": [],
            "declaredUnits": [],
            "clusterUid": cluster_uid,
        }
    )


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch):
    """Replace everything around the fence, and record what gets called."""
    calls: list[str] = []

    def use(cluster_uid: str | None, *, live: str = LIVE) -> list[str]:
        async def evaluate(*_args: Any, **_kwargs: Any) -> KubeApplyConfigResult:
            return _config(cluster_uid)

        async def api(*_args: Any, **_kwargs: Any) -> object:
            return object()

        async def identities(*_args: Any, **_kwargs: Any) -> None:
            calls.append("ensure_age_identities")

        async def hold(*_args: Any, **_kwargs: Any) -> set[tuple[str, str, str]]:
            calls.append("hold_the_engine")
            return set()

        async def apply_groups(*_args: Any, **_kwargs: Any) -> None:
            calls.append("apply_groups")

        async def read_cluster_id(*_args: Any, **_kwargs: Any) -> str:
            calls.append("fence")
            return live

        async def nothing(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(cli, "evaluate_kubeapply_config", evaluate)
        monkeypatch.setattr(cli.kr8s.asyncio, "api", api)
        monkeypatch.setattr(cli, "ensure_age_identities", identities)
        monkeypatch.setattr(cli, "_hold_the_engine", hold)
        monkeypatch.setattr(cli, "_apply_groups", apply_groups)
        monkeypatch.setattr(cli, "_run_pre_apply", nothing)
        monkeypatch.setattr(cli.clusterfence, "read_cluster_id", read_cluster_id)
        monkeypatch.setattr(cli.KubeApply, "push_cache", nothing)
        return calls

    return use


def _command(**kwargs: Any) -> cli.KubeApply:
    return cli.KubeApply(file=".", **kwargs)


class TestTheFenceComesFirst:
    async def test_a_mismatch_stops_before_anything_is_created(self, wired) -> None:
        """`ensure_age_identities` is the first write in this command. On the
        wrong cluster it creates a Namespace and a Secret, and it runs before
        any manifest -- so this is the assertion that pins the ordering."""
        calls = wired(OTHER)

        with pytest.raises(SystemExit):
            await _command().run()

        assert calls == ["fence"]

    async def test_a_mismatch_stops_before_the_engine_is_paused(self, wired) -> None:
        """The other pre-apply write. Pausing an engine on the wrong cluster
        scales somebody's real workloads to zero."""
        calls = wired(OTHER)

        with pytest.raises(SystemExit):
            await _command(pause_engine=True).run()

        assert "hold_the_engine" not in calls

    async def test_an_undeclared_cluster_stops_there_too(self, wired) -> None:
        calls = wired(None)

        with pytest.raises(SystemExit):
            await _command().run()

        assert calls == ["fence"]

    async def test_a_match_runs_the_whole_apply(self, wired) -> None:
        """The positive control. Without it every test above passes against a
        fence that refuses unconditionally."""
        calls = wired(LIVE)

        await _command().run()

        assert calls == ["fence", "ensure_age_identities", "apply_groups"]

    async def test_the_flag_gets_past_an_undeclared_cluster(self, wired) -> None:
        calls = wired(None)

        await _command(i_dont_know_which_cluster_this_is=True).run()

        assert calls == ["fence", "ensure_age_identities", "apply_groups"]

    async def test_the_flag_does_not_get_past_a_mismatch(self, wired) -> None:
        """The same rule `test_clusterfence` states, asserted through the
        command -- so wiring the flag to the wrong argument cannot pass."""
        calls = wired(OTHER)

        with pytest.raises(SystemExit):
            await _command(i_dont_know_which_cluster_this_is=True).run()

        assert calls == ["fence"]
