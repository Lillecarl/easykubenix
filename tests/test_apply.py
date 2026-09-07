from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import anyio
import pytest
from kr8s.asyncio.objects import new_class

from ekn.apply import _wait_established, apply_and_prune, discover


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

    Two pieces of surface, and both are there to reproduce something real.

    `async_api_resources` is what `ekn.apply.discover` reads, so a test can
    describe a kind exactly as an API server would -- including one whose
    singular is not the lowercased Kind, which is the case kr8s' own lookup
    cannot resolve at all.

    `async_get` is implemented directly rather than going through kr8s's list
    machinery, so a test controls exactly what kind string comes back on a
    listed object. That reproduces kr8s' other quirk (see apply.py's comment
    on the prune loop): asked for a kind *by name*, `async_get_kind` reassigns
    its argument to `async_lookup_kind`'s `"singular.group/version"` string,
    which `new_class` mis-splits on the first ".", so listed objects report a
    lowercase `.kind` that differs from the PascalCase Kind used when
    applying. `apply_and_prune` passes the class instead, which is why the
    double accepts either.
    """

    namespace = "default"

    def __init__(
        self,
        *,
        namespaced: bool = True,
        listed: list[tuple[str, str, str]] | None = None,
        resources: list[dict[str, Any]] | None = None,
    ) -> None:
        self.namespaced = namespaced
        # (namespace, kind-as-reported-by-list, name) triples "already on
        # the cluster" under the discriminator label before this apply.
        self._listed = listed or []
        # What the API server serves, in the shape `ekn.apply.discover` reads:
        # one entry per (Kind, groupVersion), carrying the plural and whether
        # the kind is namespaced. The default answers for the kind these tests
        # apply; a test about a hyphenated singular passes its own.
        self._resources = (
            resources
            if resources is not None
            else [
                {
                    "version": "autoscaling.k8s.io/v1",
                    "kind": "VerticalPodAutoscaler",
                    "name": "verticalpodautoscalers",
                    "singularName": "verticalpodautoscaler",
                    "namespaced": namespaced,
                }
            ]
        )
        self.uncached_reads = 0
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

    async def async_api_resources(self) -> list[dict[str, Any]]:
        return self._resources

    async def async_api_resources_uncached(self) -> list[dict[str, Any]]:
        self.uncached_reads += 1
        return self._resources

    def async_get(self, kind: str | type, *, namespace: Any, label_selector: Any):
        # `apply_and_prune` passes the class it applied through, so that kr8s
        # never has to look a kind up by name. A `prune_kinds` entry has no
        # class and arrives as a string.
        wanted = kind.kind if isinstance(kind, type) else kind

        async def _gen():
            reported_kind = wanted.lower() if wanted[0].isupper() else wanted
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


class TestDiscover:
    """Resolving a custom kind against the API server's discovery document.

    The whole reason `ekn.apply.discover` exists rather than calling kr8s'
    `Api.async_lookup_kind`: that lowercases the Kind and then compares it to a
    resource's plural, Kind, singular and short names, so a CRD whose singular
    is not the lowercased Kind matches nothing at all.
    """

    # The multus CRD, which is where this was found. Its singular carries
    # hyphens, so the lowercased Kind -- "networkattachmentdefinition" --
    # equals neither the plural, nor the Kind, nor the singular.
    MULTUS = {
        "version": "k8s.cni.cncf.io/v1",
        "kind": "NetworkAttachmentDefinition",
        "name": "network-attachment-definitions",
        "singularName": "network-attachment-definition",
        "namespaced": True,
    }

    # KubeVirt's, for contrast. Its singular *is* the lowercased Kind, so it
    # resolved under the old lookup and still has to resolve under this one.
    # The pair is the point: the fix identifies a kind exactly, rather than
    # widening a match until the broken case slips through.
    KUBEVIRT = {
        "version": "kubevirt.io/v1",
        "kind": "VirtualMachineInstance",
        "name": "virtualmachineinstances",
        "singularName": "virtualmachineinstance",
        "namespaced": True,
    }

    async def test_resolves_a_kind_whose_singular_is_not_the_lowercased_kind(self) -> None:
        api = FakeApi(resources=[self.MULTUS, self.KUBEVIRT])

        plural, namespaced = await discover(api, "NetworkAttachmentDefinition", "k8s.cni.cncf.io/v1")  # type: ignore[arg-type]

        assert plural == "network-attachment-definitions"
        assert namespaced is True

    async def test_resolves_a_kind_whose_singular_is_the_lowercased_kind(self) -> None:
        api = FakeApi(resources=[self.MULTUS, self.KUBEVIRT])

        plural, namespaced = await discover(api, "VirtualMachineInstance", "kubevirt.io/v1")  # type: ignore[arg-type]

        assert plural == "virtualmachineinstances"
        assert namespaced is True

    async def test_matches_on_the_group_version_too(self) -> None:
        """Two groups can serve the same Kind. The manifest names both fields,
        so both have to agree."""
        api = FakeApi(resources=[self.MULTUS])

        with pytest.raises(ValueError, match="k8s.cni.cncf.io/v1alpha1"):
            await discover(api, "NetworkAttachmentDefinition", "k8s.cni.cncf.io/v1alpha1")  # type: ignore[arg-type]

    async def test_reads_discovery_again_when_the_cache_does_not_have_the_kind(self) -> None:
        """A CRD an earlier barrier created is not in a cache filled before it
        existed, so a miss has to reach the API server before giving up."""
        api = FakeApi(resources=[self.MULTUS])

        await discover(api, "NetworkAttachmentDefinition", "k8s.cni.cncf.io/v1")  # type: ignore[arg-type]
        assert api.uncached_reads == 0

        with pytest.raises(ValueError, match="Missing"):
            await discover(api, "Missing", "example.com/v1")  # type: ignore[arg-type]
        assert api.uncached_reads == 1

    async def test_applies_a_custom_resource_of_a_hyphenated_kind(self) -> None:
        """End to end through `apply_and_prune`, which is where it failed:
        the apply died on the CR seconds after waiting for its own CRD."""
        spec = {
            "apiVersion": "k8s.cni.cncf.io/v1",
            "kind": "NetworkAttachmentDefinition",
            "metadata": {"name": "dynhetz", "namespace": "kube-system"},
        }
        api = FakeApi(resources=[self.MULTUS])

        await apply_and_prune([spec], api=api, discriminator="full", prune=False)  # type: ignore[arg-type]

        assert api.patched == [("kube-system", "NetworkAttachmentDefinition", "dynhetz")]


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
