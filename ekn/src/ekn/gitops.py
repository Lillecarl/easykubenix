from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any


class GitOpsTargetError(ValueError):
    """Raised when Nix-produced GitOps routing data is invalid."""


@dataclass(frozen=True)
class GitOpsTarget:
    branch: str
    path: str


def _required_string(route: object, field: str) -> str:
    if not isinstance(route, dict):
        raise GitOpsTargetError("GitOps route must be an attribute set")
    value = route.get(field)
    if not isinstance(value, str) or not value:
        raise GitOpsTargetError(f"GitOps route {field} must be a non-empty string")
    return value


def _index_by_path(generated: list[Any]) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Build a (namespace, kind, name) -> manifest lookup from a flat manifest list.

    `kubernetes.generatedByPath` used to provide this pre-grouped shape
    directly from Nix, but building it there costs an O(n) chain of
    `lib.recursiveUpdate` calls over every generated object just to support
    this one lookup -- cheaper to build the same index here in Python from
    the flat `kubernetes.generated` list, which Nix produces as a plain
    `map`/`++` pipeline with no extra merge step.
    """
    index: dict[tuple[str, str, str], dict[str, Any]] = {}
    for manifest in generated:
        if not isinstance(manifest, dict):
            continue
        metadata = manifest.get("metadata")
        kind = manifest.get("kind")
        if not isinstance(metadata, dict) or not isinstance(kind, str):
            continue
        namespace = metadata.get("namespace", "none")
        name = metadata.get("name")
        if isinstance(namespace, str) and isinstance(name, str):
            index[(namespace, kind, name)] = manifest
    return index


def routed_manifests(
    generated: list[Any], routing: dict[str, Any]
) -> dict[GitOpsTarget, list[dict[str, Any]]]:
    """Group manifests using normalized routes evaluated by the Nix module."""
    index = _index_by_path(generated)
    result: defaultdict[GitOpsTarget, list[dict[str, Any]]] = defaultdict(list)
    for namespace, kinds in routing.items():
        if not isinstance(kinds, dict):
            continue
        for kind, names in kinds.items():
            if not isinstance(names, dict):
                continue
            for name, routes in names.items():
                manifest = index.get((namespace, kind, name))
                if manifest is None:
                    raise GitOpsTargetError(
                        f"routing references missing resource {namespace}/{kind}/{name}"
                    )
                if not isinstance(routes, list):
                    raise GitOpsTargetError(
                        f"routing for {namespace}/{kind}/{name} must be a list"
                    )
                for route in routes:
                    target = GitOpsTarget(
                        branch=_required_string(route, "branch"),
                        path=_required_string(route, "path"),
                    )
                    result[target].append(manifest)
    return dict(result)


__all__ = ["GitOpsTarget", "GitOpsTargetError", "routed_manifests"]
