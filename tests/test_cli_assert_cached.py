"""`ekn assert-cached` — the standalone form nixkube's CI uses.

The apply path checks what it is about to send; this checks a manifest file
before anything has a cluster to send it to. nixkube's `build-manifests` job
runs it over the rendered index, which is what consumers pull.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ekn.cli import AssertCached

CACHED = "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-cacheEnv"
ABSENT = "/nix/store/cccccccccccccccccccccccccccccccc-never-pushed"


def _manifest(tmp_path: Path, path: str, suffix: str = ".json") -> Path:
    written = tmp_path / f"manifest{suffix}"
    written.write_text(
        json.dumps([{"kind": "Pod", "spec": {"volumes": [{"csi": {"volumeAttributes": {"x86_64-linux": path}}}]}}])
    )
    return written


class TestAssertCachedCommand:
    async def test_a_substituter_is_required(self, tmp_path: Path) -> None:
        """Asking nothing passes everything, which reads as a healthy
        cluster. The CI wrapper always names the node list, so reaching here
        without one is a caller's mistake rather than "checking is off"."""
        command = AssertCached(manifests=[_manifest(tmp_path, CACHED)], substituter=[])

        with pytest.raises(SystemExit):
            await command.run()

    async def test_a_manifest_naming_no_store_path_needs_no_substituter(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.json"
        empty.write_text(json.dumps([{"kind": "ConfigMap", "data": {"a": "b"}}]))

        await AssertCached(manifests=[empty], substituter=[]).run()

    async def test_it_reads_yaml_as_readily_as_json(self, tmp_path: Path) -> None:
        """The text is scanned for store paths, so the manifest's format does
        not matter. `nix-csi-deployment.yaml` is the other CI call site."""
        from ekn import storecheck

        written = tmp_path / "deployment.yaml"
        written.write_text(f"apiVersion: v1\nkind: Pod\nspec:\n  containers:\n    - image: {CACHED}\n")

        assert storecheck.store_paths_in_text(written.read_text()) == {CACHED.removeprefix("/nix/store/")}

    async def test_an_unfetchable_path_exits_nonzero(self, tmp_path: Path, monkeypatch) -> None:
        """The whole point: a path no substituter serves stops the pipeline
        rather than reaching a node that cannot build it."""
        from ekn import storecheck

        async def refuse(roots, **_):
            raise storecheck.StorePathsUnavailableError(f"{len(roots)} path(s) on no substituter")

        monkeypatch.setattr(storecheck, "assert_fetchable", refuse)
        command = AssertCached(manifests=[_manifest(tmp_path, ABSENT)], substituter=["https://cache.example"])

        with pytest.raises(SystemExit, match="on no substituter"):
            await command.run()
