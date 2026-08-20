from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import anyio
import pytest
from kr8s.asyncio.objects import new_class

from ekn.apply import _wait_established, apply_and_prune


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


class _FakeResponse:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def json(self) -> dict[str, Any]:
        return self._data


class FakeApi:
    """Stands in for kr8s's real Api for `apply_and_prune` tests.

    `async_get` is implemented directly (rather than going through kr8s's
    real discovery/list machinery) so tests can control exactly what kind
    string comes back on listed objects -- this is what reproduces kr8s's
    real quirk (see apply.py's comment on the prune loop): for CRD kinds,
    kr8s's own `async_get_kind` reassigns its `kind` argument via
    `async_lookup_kind`'s `"singular.group/version"` return value, which
    `new_class` then mis-splits on the first ".", so listed objects end up
    with a lowercase `.kind` that differs from the PascalCase Kind used
    when applying.
    """

    namespace = "default"

    def __init__(
        self,
        *,
        namespaced: bool = True,
        listed: list[tuple[str, str, str]] | None = None,
    ) -> None:
        self.namespaced = namespaced
        # (namespace, kind-as-reported-by-list, name) triples "already on
        # the cluster" under the discriminator label before this apply.
        self._listed = listed or []
        self.deleted: list[tuple[str, str, str]] = []
        self.patched: list[tuple[str, str, str]] = []

    @asynccontextmanager
    async def call_api(
        self,
        method: str,
        *,
        version: str | None = None,
        url: str | None = None,
        namespace: str | None = None,
        content: str | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ):
        assert method == "PATCH"
        import json as _json

        body = _json.loads(content or "{}")
        self.patched.append((namespace or "none", body["kind"], body["metadata"]["name"]))
        yield _FakeResponse(body)

    async def async_lookup_kind(self, lookup: str) -> tuple[str, str, bool]:
        kind = lookup.split(".", 1)[0]
        return (kind.lower(), kind.lower() + "s", self.namespaced)

    def async_get(self, kind: str, *, namespace: Any, label_selector: Any):
        async def _gen():
            reported_kind = kind.lower() if kind[0].isupper() else kind
            for ns, listed_kind, name in self._listed:
                if listed_kind.lower() != reported_kind.lower():
                    continue
                cls = new_class(listed_kind, "example.com/v1", namespaced=True)
                obj = cls(
                    {"kind": listed_kind, "metadata": {"name": name, "namespace": ns}},
                    api=self,  # type: ignore[arg-type]
                )

                # `k` bound as a default for the same reason `o` is: both are
                # loop variables, and a closure over either would report the
                # last iteration's value for every object the generator yields.
                async def _delete(o: Any = obj, k: str = listed_kind) -> None:
                    self.deleted.append((o.namespace or "none", k, o.name))

                obj.delete = _delete  # type: ignore[method-assign]
                yield obj

        return _gen()


class TestApplyAndPrune:
    async def test_does_not_prune_object_it_just_applied(self) -> None:
        """Regression test for the exact bug this fixes: kr8s reports a
        just-applied CRD object's kind in a different case when listing it
        back for the prune scan -- that must not cause it to be pruned."""
        spec = {
            "apiVersion": "autoscaling.k8s.io/v1",
            "kind": "VerticalPodAutoscaler",
            "metadata": {"name": "argocd-server", "namespace": "argocd"},
        }
        api = FakeApi(listed=[("argocd", "verticalpodautoscaler", "argocd-server")])

        await apply_and_prune([spec], api=api, discriminator="full")  # type: ignore[arg-type]

        assert api.deleted == []

    async def test_prunes_genuinely_stale_object(self) -> None:
        spec = {
            "apiVersion": "autoscaling.k8s.io/v1",
            "kind": "VerticalPodAutoscaler",
            "metadata": {"name": "argocd-server", "namespace": "argocd"},
        }
        api = FakeApi(
            listed=[
                ("argocd", "verticalpodautoscaler", "argocd-server"),
                ("argocd", "verticalpodautoscaler", "long-gone"),
            ]
        )

        await apply_and_prune([spec], api=api, discriminator="full")  # type: ignore[arg-type]

        assert api.deleted == [("argocd", "verticalpodautoscaler", "long-gone")]


class FakeCrd:
    """A CRD that raises kr8s' race for the first *failures* reads.

    `kr8s.APIObject.wait` reads `.status.conditions` and iterates it. A CRD
    the API server has accepted but has not given a status yet has none, so
    the call raises `TypeError: 'NoneType' object is not iterable`. That is
    the race `_wait_established` retries around.
    """

    def __init__(self, failures: int, *, ever_establishes: bool = True) -> None:
        self.name = "applicationsets.argoproj.io"
        self.failures = failures
        self.ever_establishes = ever_establishes
        self.calls = 0

    async def wait(self, conditions: str) -> None:
        self.calls += 1
        if self.calls <= self.failures:
            raise TypeError("'NoneType' object is not iterable")
        if not self.ever_establishes:
            await anyio.sleep(3600)


class TestWaitEstablished:
    async def test_a_status_that_arrives_late_is_waited_out(self) -> None:
        crd = FakeCrd(failures=2)

        await _wait_established(crd, 5)  # type: ignore[arg-type] -- a stand-in for kr8s' APIObject

        assert crd.calls == 3

    async def test_a_crd_that_never_establishes_fails_with_its_name(self) -> None:
        """The deadline bounds the retries and the watch inside them together.

        `asyncio.timeout` raises a bare `TimeoutError`, so `_wait_established`
        replaces it with one that names the CRD and the deadline.
        """
        crd = FakeCrd(failures=0, ever_establishes=False)

        with pytest.raises(TimeoutError, match=r"applicationsets\.argoproj\.io did not become Established within"):
            await _wait_established(crd, 0.05)  # type: ignore[arg-type] -- see above

    async def test_a_retry_storm_cannot_outlive_the_deadline(self) -> None:
        """A status that never arrives is bounded too, not only a watch that
        never returns. The retries and the sleeps between them run inside the
        one `asyncio.timeout`, so both count against the same deadline.
        """
        crd = FakeCrd(failures=1_000_000)

        with pytest.raises(TimeoutError):
            await _wait_established(crd, 0.05)  # type: ignore[arg-type] -- see above
