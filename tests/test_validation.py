from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from anyio import Path as AnyioPath

from ekn.validation import prepare_validation_objects

if TYPE_CHECKING:
    from pathlib import Path


def _configmap(name: str, namespace: str = "ns") -> dict[str, object]:
    return {
        "kind": "ConfigMap",
        "apiVersion": "v1",
        "metadata": {"name": name, "namespace": namespace},
    }


def _names(objects: object) -> list[Any]:
    return [cast("dict[str, Any]", obj)["metadata"]["name"] for obj in cast("list[object]", objects)]


async def test_prepare_validation_objects_drops_novalidate_keys(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifests.json"
    await AnyioPath(manifest_path).write_text(json.dumps([_configmap("keep"), _configmap("skip")]))

    objects = await prepare_validation_objects(
        str(manifest_path),
        novalidate_keys={("ConfigMap", "ns", "skip")},
    )

    assert _names(objects) == ["keep"]


async def test_prepare_validation_objects_empty_novalidate_keeps_everything(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifests.json"
    await AnyioPath(manifest_path).write_text(json.dumps([_configmap("a"), _configmap("b")]))

    objects = await prepare_validation_objects(str(manifest_path), novalidate_keys=set())

    assert _names(objects) == ["a", "b"]


async def test_prepare_validation_objects_unwraps_a_list_typed_k8s_result(tmp_path: Path) -> None:
    # internal.manifestJSONFile can come back as a bare `kind: List` wrapper
    # (`{"items": [...]}`) instead of a plain JSON array -- see
    # kubernetes.nix's `internal.nix`.
    manifest_path = tmp_path / "manifests.json"
    await AnyioPath(manifest_path).write_text(json.dumps({"kind": "List", "items": [_configmap("only")]}))

    objects = await prepare_validation_objects(str(manifest_path), novalidate_keys=set())

    assert _names(objects) == ["only"]


async def test_prepare_validation_objects_passes_through_objects_without_a_sops_block(
    tmp_path: Path,
) -> None:
    # maybe_decrypt no-ops (no `sops` subprocess spawned) unless `obj["sops"]`
    # is a dict -- exercising the decrypt step here doesn't need a real
    # `sops` binary as long as no fixture object carries that marker.
    manifest_path = tmp_path / "manifests.json"
    await AnyioPath(manifest_path).write_text(json.dumps([_configmap("plain")]))

    objects = await prepare_validation_objects(str(manifest_path), novalidate_keys=set())

    assert objects == [_configmap("plain")]
