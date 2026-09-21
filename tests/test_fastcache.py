"""The local apply cache: what it skips, what it refuses to remember, and
what it does when its own file is unusable.

Every failure this cache can have points the same way -- it says "unchanged"
about an object that is not, and the apply silently does nothing. So most of
what is here is negative controls.

A skip needs two agreeing answers: this file's record of the bytes, and the
live `resourceVersion` from `livestate.sweep`. Both halves get their own
negative control, because either one passing alone is a bug that only shows
up as an apply that quietly did nothing.
"""

from __future__ import annotations

import json
import stat
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

# The double an apply test needs: discovery, PATCH, a listing for the prune
# and a metadata sweep. Imported rather than copied so both suites describe
# one API server.
from test_apply import FakeApi

from ekn.apply import apply_and_prune
from ekn.directapply import converge_direct
from ekn.fastcache import FORMAT_VERSION, ApplyCache, Entry, cache_root, cluster_id, digest, open_cache
from ekn.livestate import LiveObject

if TYPE_CHECKING:
    from pathlib import Path

    from ekn.apply import Manifest

#: What the fake API server answers with for a freshly applied object, and
#: what a test therefore has to record to be allowed to skip it.
LIVE = "1"


def manifest(name: str = "cm", **data: Any) -> Manifest:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "namespace": "default"},
        "data": data or {"a": "1"},
    }


def live_at(version: str | None = LIVE, key: tuple[str, str, str] = ("default", "ConfigMap", "cm")) -> Any:
    """A sweep result holding one object at *version*."""
    return {key: LiveObject(key=key, resource_version=version)}


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


KEY = ("default", "ConfigMap", "cm")


class TestWhatIsSkipped:
    def test_nothing_without_the_flag(self) -> None:
        """Recording is unconditional and skipping is not. Otherwise the first
        fast run after an ordinary one has nothing to read."""
        store = cache_for_test(assume_unchanged=False, live=live_at())
        store.record(manifest(), field_manager="ekn", resource_version=LIVE)

        assert not store.unchanged(manifest(), field_manager="ekn", key=KEY)

    def test_a_recorded_object_still_at_its_version(self) -> None:
        store = cache_for_test(assume_unchanged=True, live=live_at())
        store.record(manifest(), field_manager="ekn", resource_version=LIVE)

        assert store.unchanged(manifest(), field_manager="ekn", key=KEY)
        assert store.skipped == 1

    def test_a_changed_object_is_not_skipped(self) -> None:
        store = cache_for_test(assume_unchanged=True, live=live_at())
        store.record(manifest(a="1"), field_manager="ekn", resource_version=LIVE)

        assert not store.unchanged(manifest(a="2"), field_manager="ekn", key=KEY)

    def test_a_changed_field_manager_is_not_skipped(self) -> None:
        store = cache_for_test(assume_unchanged=True, live=live_at())
        store.record(manifest(), field_manager="ekn", resource_version=LIVE)

        assert not store.unchanged(manifest(), field_manager="argocd", key=KEY)

    def test_another_environment_is_not_skipped(self, tmp_path: Path) -> None:
        """One cluster holds several environments, and they are different
        objects with the same names."""
        written = ApplyCache(path=tmp_path / "c.json", environment="prod", assume_unchanged=True)
        written.record(manifest(), field_manager="ekn", resource_version=LIVE)

        other = ApplyCache(
            path=tmp_path / "c.json",
            environment="staging",
            entries=written.entries,
            assume_unchanged=True,
            live=live_at(),
        )

        assert not other.unchanged(manifest(), field_manager="ekn", key=KEY)

    def test_an_unknown_object_is_not_skipped(self) -> None:
        assert not cache_for_test(assume_unchanged=True, live=live_at()).unchanged(
            manifest(), field_manager="ekn", key=KEY
        )


class TestTheLiveHalf:
    """The gate `livestate.sweep` feeds. The record alone is a claim about the
    cluster made from a laptop, and every test here is a way it is wrong."""

    def test_a_written_object_is_not_skipped(self) -> None:
        """A `kubectl edit` moves the version and leaves the bytes this
        machine sent exactly as they were."""
        store = cache_for_test(assume_unchanged=True, live=live_at("2"))
        store.record(manifest(), field_manager="ekn", resource_version=LIVE)

        assert not store.unchanged(manifest(), field_manager="ekn", key=KEY)

    def test_a_deleted_object_is_not_skipped(self) -> None:
        """Gone from the cluster is gone from the sweep. Skipping it would
        leave it deleted for ever, which is the failure this gate exists for."""
        store = cache_for_test(assume_unchanged=True, live={})
        store.record(manifest(), field_manager="ekn", resource_version=LIVE)

        assert not store.unchanged(manifest(), field_manager="ekn", key=KEY)

    def test_nothing_is_skipped_without_a_sweep(self) -> None:
        """`live=None` is a caller that did not sweep. It must not fall back
        to trusting the file, which is the behaviour this replaced."""
        store = cache_for_test(assume_unchanged=True)
        store.record(manifest(), field_manager="ekn", resource_version=LIVE)

        assert not store.unchanged(manifest(), field_manager="ekn", key=KEY)

    def test_an_entry_with_no_recorded_version_is_not_skipped(self) -> None:
        """An apply whose response named no version, and every entry of a
        version 1 file."""
        store = cache_for_test(assume_unchanged=True, live=live_at())
        store.record(manifest(), field_manager="ekn", resource_version=None)

        assert not store.unchanged(manifest(), field_manager="ekn", key=KEY)
        assert not store.primed()

    def test_the_live_key_is_the_built_one(self) -> None:
        """A manifest naming no namespace is applied into the API's default
        one, and the sweep sees it there. Keying the lookup off the manifest
        would read `none`, match nothing, and skip nothing while looking
        healthy."""
        spec: Manifest = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "cm"}, "data": {"a": "1"}}
        store = cache_for_test(assume_unchanged=True, live=live_at())
        store.record(spec, field_manager="ekn", resource_version=LIVE)

        assert store.unchanged(spec, field_manager="ekn", key=KEY)

    def test_primed_once_something_can_be_skipped(self) -> None:
        store = cache_for_test(assume_unchanged=True, live=live_at())
        assert not store.primed()

        store.record(manifest(), field_manager="ekn", resource_version=LIVE)

        assert store.primed()


class TestCredentials:
    """A digest of the sent bytes is a brute-force oracle for the one field a
    SOPS-encrypted or seeded object does not keep in git."""

    IDENTITY = ("default", "ConfigMap", "cm")

    def test_a_named_object_is_never_recorded(self) -> None:
        store = cache_for_test(assume_unchanged=True, never_record=frozenset({self.IDENTITY}))

        store.record(manifest(), field_manager="ekn", resource_version=LIVE)

        assert store.recorded == 0
        assert store.entries == {}

    def test_a_named_object_is_never_skipped(self) -> None:
        """Even if an entry reached the file some other way -- an older
        version of this code, a hand-edited cache."""
        store = cache_for_test(assume_unchanged=True)
        store.record(manifest(), field_manager="ekn", resource_version=LIVE)
        protected = ApplyCache(
            path=store.path,
            environment="prod",
            entries=store.entries,
            assume_unchanged=True,
            never_record=frozenset({self.IDENTITY}),
            live=live_at(),
        )

        assert not protected.unchanged(manifest(), field_manager="ekn", key=KEY)


class TestTheFile:
    def test_a_saved_cache_reads_back(self, tmp_path: Path) -> None:
        written = ApplyCache(path=tmp_path / "c.json", environment="prod")
        written.record(manifest(), field_manager="ekn", resource_version=LIVE)
        written.save()

        loaded = ApplyCache(
            path=written.path,
            environment="prod",
            entries=_entries_of(written.path),
            assume_unchanged=True,
            live=live_at(),
        )

        assert loaded.unchanged(manifest(), field_manager="ekn", key=KEY)

    def test_the_version_survives_the_file(self, tmp_path: Path) -> None:
        """Both halves of an entry, or the next run reads a digest it cannot
        check against anything and applies everything for ever."""
        written = ApplyCache(path=tmp_path / "c.json", environment="prod")
        written.record(manifest(), field_manager="ekn", resource_version="4242")
        written.save()

        entry = next(iter(_entries_of(written.path)["prod"].values()))

        assert entry.resource_version == "4242"
        assert entry.digest == digest(manifest(), field_manager="ekn")

    def test_it_is_not_world_readable(self, tmp_path: Path) -> None:
        """It names every object of every environment on a cluster."""
        written = ApplyCache(path=tmp_path / "c.json", environment="prod")
        written.record(manifest(), field_manager="ekn", resource_version=LIVE)
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
        store.record(manifest(), field_manager="ekn", resource_version=LIVE)

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

    def test_an_entry_of_the_wrong_shape_is_dropped(self, tmp_path: Path) -> None:
        """Version 1's bare digest string, in a file claiming version 2. A
        hand-edited cache, or two versions of `ekn` sharing a home."""
        path = tmp_path / "c.json"
        path.write_text(json.dumps({"version": FORMAT_VERSION, "entries": {"prod": {"a/b/c": "sha256:x"}}}))

        assert _entries_of(path) == {"prod": {}}

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

    KEY: ClassVar[tuple[str, str, str]] = ("default", "VerticalPodAutoscaler", "vpa")

    async def test_a_recorded_object_is_not_sent_again(self, tmp_path: Path) -> None:
        """The whole point, end to end: apply, sweep, apply again.

        The second run reads the same API server -- a fresh `FakeApi` would
        be a different cluster, and the cache is keyed by cluster for exactly
        that reason.
        """
        api = FakeApi()
        store = cache(tmp_path, assume_unchanged=True)
        await apply_and_prune([self.SPEC], api=api, environment="prod", prune=False, cache=store)  # type: ignore[arg-type]
        assert len(api.patched) == 1

        api.patched.clear()
        store.live = await sweep_of(api)
        await apply_and_prune([self.SPEC], api=api, environment="prod", prune=False, cache=store)  # type: ignore[arg-type]

        assert api.patched == []
        assert store.skipped == 1

    async def test_an_object_somebody_wrote_is_sent_again(self, tmp_path: Path) -> None:
        """The mutation control for the test above, and the case the gate
        exists for: the bytes this machine sent have not changed, and the
        object on the cluster has."""
        api = FakeApi()
        store = cache(tmp_path, assume_unchanged=True)
        await apply_and_prune([self.SPEC], api=api, environment="prod", prune=False, cache=store)  # type: ignore[arg-type]

        api.patched.clear()
        api.write(self.KEY)
        store.live = await sweep_of(api)
        await apply_and_prune([self.SPEC], api=api, environment="prod", prune=False, cache=store)  # type: ignore[arg-type]

        assert api.patched == [self.KEY]
        assert store.skipped == 0

    async def test_a_skipped_object_is_not_pruned(self, tmp_path: Path) -> None:
        """The trap this whole design turns on. A prune deletes what the
        generation does not contain, and an object skipped because it is
        already correct *is* contained -- so it has to reach the desired set
        without being applied."""
        store = cache(tmp_path, assume_unchanged=True)
        store.record(self.SPEC, field_manager="ekn", resource_version="1")
        api = FakeApi(
            listed=[
                ("default", "VerticalPodAutoscaler", "vpa"),
                ("default", "VerticalPodAutoscaler", "stale"),
            ]
        )
        store.live = await sweep_of(api)

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
        store.record(self.SPEC, field_manager="ekn", resource_version="1")
        api = FakeApi(listed=[("default", "VerticalPodAutoscaler", "vpa")])
        store.live = await sweep_of(api)

        report, desired = await converge_direct([self.SPEC], api=api, environment="prod", cache=store)  # type: ignore[arg-type]

        assert api.patched == []
        assert report.skipped == 1
        assert self.KEY in desired

    async def test_the_converging_apply_sends_what_somebody_wrote(self, tmp_path: Path) -> None:
        """The same mutation control on the other seam."""
        store = cache(tmp_path, assume_unchanged=True)
        store.record(self.SPEC, field_manager="ekn", resource_version="1")
        api = FakeApi(listed=[("default", "VerticalPodAutoscaler", "vpa")])
        api.write(self.KEY)
        store.live = await sweep_of(api)

        report, _desired = await converge_direct([self.SPEC], api=api, environment="prod", cache=store)  # type: ignore[arg-type]

        assert api.patched == [self.KEY]
        assert report.skipped == 0

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


async def sweep_of(api: FakeApi) -> Any:
    """The real sweep against the fake API server.

    The gate is two things agreeing, so a test that hand-built the live half
    would only check the half it wrote.
    """
    from ekn.livestate import sweep

    return await sweep(api, [("VerticalPodAutoscaler", "autoscaling.k8s.io/v1")])  # type: ignore[arg-type]


def _entries_of(path: Path) -> dict[str, dict[str, Entry]]:
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


class TestTheCredentialRuleOnEveryPath:
    """A digest of what was sent is a brute-force oracle for the one field a
    SOPS-encrypted or seeded object does not keep in git. Both apply commands
    have to answer that the same way, and they read the objects at different
    points: `ekn kubeapply` before it decrypts, `_applyManifest` from the
    file, because decryption removes the `sops:` block that says which object
    this is about."""

    ENCRYPTED: ClassVar[Any] = {
        "kind": "Secret",
        "apiVersion": "v1",
        "metadata": {"name": "credential", "namespace": "default"},
        "sops": {"age": []},
    }

    async def test_the_manifest_file_names_them_before_decryption(self, tmp_path: Path) -> None:
        from ekn.cli import _uncacheable_objects
        from ekn.validation import load_manifest_objects

        path = tmp_path / "manifests.json"
        path.write_text(json.dumps([self.ENCRYPTED, manifest()]))

        from_file = _uncacheable_objects(await load_manifest_objects(str(path)))

        assert from_file == {("default", "Secret", "credential")}

    def test_a_decrypted_object_no_longer_names_itself(self) -> None:
        """The negative control, and the reason `_applyManifest` reads the
        file rather than what it is about to send: `sops.maybe_decrypt`
        returns the object without the block that identifies it."""
        from ekn.cli import _uncacheable_objects

        decrypted = {key: value for key, value in self.ENCRYPTED.items() if key != "sops"}

        assert _uncacheable_objects([decrypted]) == set()
