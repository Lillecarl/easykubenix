"""The local apply cache: what it skips, what it refuses to remember, and
what it does when its own file is unusable.

Every failure this cache can have points the same way -- it says "unchanged"
about an object that is not, and the apply silently does nothing. So most of
what is here is negative controls.
"""

from __future__ import annotations

import json
import stat
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

# The double an apply test needs: discovery, PATCH and a listing for the
# prune. Imported rather than copied so both suites describe one API server.
from test_apply import FakeApi

from ekn.apply import apply_and_prune
from ekn.directapply import converge_direct
from ekn.fastcache import FORMAT_VERSION, ApplyCache, cache_root, cluster_id, digest, open_cache

if TYPE_CHECKING:
    from pathlib import Path

    from ekn.apply import Manifest


def manifest(name: str = "cm", **data: Any) -> Manifest:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "namespace": "default"},
        "data": data or {"a": "1"},
    }


def cache(tmp_path: Path, **kwargs: Any) -> ApplyCache:
    return ApplyCache(path=tmp_path / "cluster.json", environment="prod", **kwargs)


class TestDigest:
    def test_the_field_manager_is_part_of_it(self) -> None:
        """A manager change is an ownership change, so it must not look like
        the same apply. `deployment.fieldManager` makes this reachable from
        one line of configuration."""
        spec = manifest()

        assert digest(spec, field_manager="ekn") != digest(spec, field_manager="argocd")

    def test_key_order_does_not_change_it(self) -> None:
        """Nix and the YAML reader do not agree on key order, and the render
        that produced an object is not the one that checks it."""
        first: Manifest = {"kind": "ConfigMap", "apiVersion": "v1", "metadata": {"name": "cm"}}
        second: Manifest = {"apiVersion": "v1", "metadata": {"name": "cm"}, "kind": "ConfigMap"}

        assert digest(first, field_manager="ekn") == digest(second, field_manager="ekn")

    def test_a_changed_value_changes_it(self) -> None:
        assert digest(manifest(a="1"), field_manager="ekn") != digest(manifest(a="2"), field_manager="ekn")


class TestWhatIsSkipped:
    def test_nothing_without_the_flag(self) -> None:
        """Recording is unconditional and skipping is not. Otherwise the first
        fast run after an ordinary one has nothing to read."""
        store = cache_for_test(assume_unchanged=False)
        store.record(manifest(), field_manager="ekn")

        assert not store.unchanged(manifest(), field_manager="ekn")

    def test_a_recorded_object_with_the_flag(self) -> None:
        store = cache_for_test(assume_unchanged=True)
        store.record(manifest(), field_manager="ekn")

        assert store.unchanged(manifest(), field_manager="ekn")
        assert store.skipped == 1

    def test_a_changed_object_is_not_skipped(self) -> None:
        store = cache_for_test(assume_unchanged=True)
        store.record(manifest(a="1"), field_manager="ekn")

        assert not store.unchanged(manifest(a="2"), field_manager="ekn")

    def test_a_changed_field_manager_is_not_skipped(self) -> None:
        store = cache_for_test(assume_unchanged=True)
        store.record(manifest(), field_manager="ekn")

        assert not store.unchanged(manifest(), field_manager="argocd")

    def test_another_environment_is_not_skipped(self, tmp_path: Path) -> None:
        """One cluster holds several environments, and they are different
        objects with the same names."""
        written = ApplyCache(path=tmp_path / "c.json", environment="prod", assume_unchanged=True)
        written.record(manifest(), field_manager="ekn")

        other = ApplyCache(
            path=tmp_path / "c.json",
            environment="staging",
            entries=written.entries,
            assume_unchanged=True,
        )

        assert not other.unchanged(manifest(), field_manager="ekn")

    def test_an_unknown_object_is_not_skipped(self) -> None:
        assert not cache_for_test(assume_unchanged=True).unchanged(manifest(), field_manager="ekn")


class TestCredentials:
    """A digest of the sent bytes is a brute-force oracle for the one field a
    SOPS-encrypted or seeded object does not keep in git."""

    IDENTITY = ("default", "ConfigMap", "cm")

    def test_a_named_object_is_never_recorded(self) -> None:
        store = cache_for_test(assume_unchanged=True, never_record=frozenset({self.IDENTITY}))

        store.record(manifest(), field_manager="ekn")

        assert store.recorded == 0
        assert store.entries == {}

    def test_a_named_object_is_never_skipped(self) -> None:
        """Even if an entry reached the file some other way -- an older
        version of this code, a hand-edited cache."""
        store = cache_for_test(assume_unchanged=True)
        store.record(manifest(), field_manager="ekn")
        protected = ApplyCache(
            path=store.path,
            environment="prod",
            entries=store.entries,
            assume_unchanged=True,
            never_record=frozenset({self.IDENTITY}),
        )

        assert not protected.unchanged(manifest(), field_manager="ekn")


class TestTheFile:
    def test_a_saved_cache_reads_back(self, tmp_path: Path) -> None:
        written = ApplyCache(path=tmp_path / "c.json", environment="prod")
        written.record(manifest(), field_manager="ekn")
        written.save()

        loaded = ApplyCache(
            path=written.path,
            environment="prod",
            entries=_entries_of(written.path),
            assume_unchanged=True,
        )

        assert loaded.unchanged(manifest(), field_manager="ekn")

    def test_it_is_not_world_readable(self, tmp_path: Path) -> None:
        """It names every object of every environment on a cluster."""
        written = ApplyCache(path=tmp_path / "c.json", environment="prod")
        written.record(manifest(), field_manager="ekn")
        written.save()

        assert stat.S_IMODE(written.path.stat().st_mode) == 0o600

    def test_nothing_is_written_when_nothing_changed(self, tmp_path: Path) -> None:
        store = ApplyCache(path=tmp_path / "c.json", environment="prod")

        store.save()

        assert not store.path.exists()

    def test_a_save_that_cannot_write_is_not_an_error(self, tmp_path: Path) -> None:
        """The apply already happened. Losing the record costs the next run
        some requests; raising here would report a successful apply as a
        failure -- and the build sandboxes this runs in have no writable
        home."""
        blocked = tmp_path / "file"
        blocked.write_text("not a directory")
        store = ApplyCache(path=blocked / "c.json", environment="prod")
        store.record(manifest(), field_manager="ekn")

        store.save()

    def test_a_corrupt_file_reads_as_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "c.json"
        path.write_text("{ this is not json")

        assert _entries_of(path) == {}

    def test_an_older_format_reads_as_empty(self, tmp_path: Path) -> None:
        """A digest whose meaning moved answers "unchanged" for an object that
        is not, which is the one failure this must not have."""
        path = tmp_path / "c.json"
        path.write_text(json.dumps({"version": FORMAT_VERSION - 1, "entries": {"prod": {"a/b/c": "sha256:x"}}}))

        assert _entries_of(path) == {}

    def test_a_missing_file_reads_as_empty(self, tmp_path: Path) -> None:
        assert _entries_of(tmp_path / "absent.json") == {}


class TestCacheRoot:
    def test_the_override_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EKN_APPLY_CACHE_DIR", str(tmp_path))

        assert cache_root() == tmp_path

    def test_it_follows_xdg_state_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("EKN_APPLY_CACHE_DIR", raising=False)
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

        assert cache_root() == tmp_path / "ekn" / "apply-cache"


class TestClusterIdentity:
    """Keyed by the cluster, never by the kubeconfig context name: `ekn
    validate` boots a fresh API server under a fixed context, and a
    name-keyed cache would call every object unchanged against an empty
    cluster."""

    async def test_the_kube_system_uid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_namespace(monkeypatch, {"metadata": {"uid": "abc-123"}})

        assert await cluster_id(FakeApi()) == "abc-123"  # type: ignore[arg-type]

    async def test_an_unreadable_namespace_has_no_identity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_namespace(monkeypatch, None)

        assert await cluster_id(FakeApi()) is None  # type: ignore[arg-type]

    async def test_no_identity_means_no_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Rather than a cache shared between clusters, which would skip
        objects on a cluster that has never seen them."""
        _install_namespace(monkeypatch, None)

        assert await open_cache(FakeApi(), environment="prod") is None  # type: ignore[arg-type]

    async def test_the_path_carries_the_uid(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EKN_APPLY_CACHE_DIR", str(tmp_path))
        _install_namespace(monkeypatch, {"metadata": {"uid": "abc-123"}})

        store = await open_cache(FakeApi(), environment="prod")  # type: ignore[arg-type]

        assert store is not None
        assert store.path == tmp_path / "abc-123.json"


class TestAgainstAnApply:
    """The two things the cache changes about `apply_and_prune`."""

    SPEC: ClassVar[Any] = {
        "apiVersion": "autoscaling.k8s.io/v1",
        "kind": "VerticalPodAutoscaler",
        "metadata": {"name": "vpa", "namespace": "default"},
    }

    async def test_a_recorded_object_is_not_sent_again(self, tmp_path: Path) -> None:
        api = FakeApi()
        store = cache(tmp_path, assume_unchanged=True)
        await apply_and_prune([self.SPEC], api=api, environment="prod", prune=False, cache=store)  # type: ignore[arg-type]
        assert len(api.patched) == 1

        second = FakeApi()
        await apply_and_prune([self.SPEC], api=second, environment="prod", prune=False, cache=store)  # type: ignore[arg-type]

        assert second.patched == []
        assert store.skipped == 1

    async def test_a_skipped_object_is_not_pruned(self, tmp_path: Path) -> None:
        """The trap this whole design turns on. A prune deletes what the
        generation does not contain, and an object skipped because it is
        already correct *is* contained -- so it has to reach the desired set
        without being applied."""
        store = cache(tmp_path, assume_unchanged=True)
        store.record(self.SPEC, field_manager="ekn")
        api = FakeApi(
            listed=[
                ("default", "VerticalPodAutoscaler", "vpa"),
                ("default", "VerticalPodAutoscaler", "stale"),
            ]
        )

        await apply_and_prune(
            [self.SPEC],
            api=api,  # type: ignore[arg-type]
            environment="prod",
            prune=True,
            # So the kind is scanned whether or not the desired set names it.
            # Without this the scan covers only the kinds `desired` holds, and
            # a skip that forgot to record one would leave this test passing
            # by scanning nothing at all.
            prune_kinds={"VerticalPodAutoscaler": "autoscaling.k8s.io/v1"},
            cache=store,
        )

        assert api.patched == []
        assert api.deleted == [("default", "VerticalPodAutoscaler", "stale")]

    async def test_the_converging_apply_skips_and_still_desires(self, tmp_path: Path) -> None:
        """`--converge` reaches the cache through a different seam than the
        barrier apply, and the desired set is built in a different place. Both
        have to answer the same."""
        store = cache(tmp_path, assume_unchanged=True)
        store.record(self.SPEC, field_manager="ekn")
        api = FakeApi()

        report, desired = await converge_direct([self.SPEC], api=api, environment="prod", cache=store)  # type: ignore[arg-type]

        assert api.patched == []
        assert report.skipped == 1
        assert ("default", "VerticalPodAutoscaler", "vpa") in desired

    async def test_a_failed_apply_records_nothing(self, tmp_path: Path) -> None:
        """Otherwise the next run skips an object that was never applied."""
        store = cache(tmp_path)
        # A kind this API server does not serve. `kr8s.new_class` registers
        # every class it builds process-wide, so a kind another test applied
        # resolves here from that registry and never reaches discovery.
        unserved: Any = {
            "apiVersion": "fastcache.test/v1",
            "kind": "NeverServed",
            "metadata": {"name": "x", "namespace": "default"},
        }
        api = FakeApi(resources=[])

        with pytest.raises(ValueError, match="serves no"):
            await apply_and_prune([unserved], api=api, environment="prod", prune=False, cache=store)  # type: ignore[arg-type]

        assert store.recorded == 0


def cache_for_test(**kwargs: Any) -> ApplyCache:
    from pathlib import Path as _Path

    return ApplyCache(path=_Path("/nonexistent/c.json"), environment="prod", **kwargs)


def _entries_of(path: Path) -> dict[str, dict[str, str]]:
    from ekn.fastcache import _read

    return _read(path)


def _install_namespace(monkeypatch: pytest.MonkeyPatch, raw: dict[str, Any] | None) -> None:
    """Stand in for the one GET this cache makes. `raw=None` raises, which is
    what a cluster that will not answer looks like."""

    class _Namespace:
        def __init__(self, data: dict[str, Any]) -> None:
            self.raw = data

        @classmethod
        async def async_get(cls, _name: str, *, api: Any) -> _Namespace:
            _ = api
            if raw is None:
                msg = "forbidden"
                raise RuntimeError(msg)
            return cls(raw)

    monkeypatch.setattr("ekn.fastcache.Namespace", _Namespace)
