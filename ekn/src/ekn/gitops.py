from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any


class GitOpsTargetError(ValueError):
    """Raised when Nix-produced GitOps routing data is invalid."""


@dataclass(frozen=True)
class GitOpsTarget:
    backend: str
    branch: str
    path: str


def _required_string(route: object, field: str) -> str:
    if not isinstance(route, dict):
        raise GitOpsTargetError("GitOps route must be an attribute set")
    value = route.get(field)
    if not isinstance(value, str) or not value:
        raise GitOpsTargetError(f"GitOps route {field} must be a non-empty string")
    return value


def _manifest_at(
    manifests: dict[str, Any], namespace: str, kind: str, name: str
) -> dict[str, Any]:
    result = manifests.get(namespace, {}).get(kind, {}).get(name)
    if not isinstance(result, dict):
        raise GitOpsTargetError(
            f"routing references missing resource {namespace}/{kind}/{name}"
        )
    return result


def routed_manifests(
    manifests: dict[str, Any], routing: dict[str, Any]
) -> dict[GitOpsTarget, list[dict[str, Any]]]:
    """Group manifests using normalized routes evaluated by the Nix module."""
    result: defaultdict[GitOpsTarget, list[dict[str, Any]]] = defaultdict(list)
    for namespace, kinds in routing.items():
        if not isinstance(kinds, dict):
            continue
        for kind, names in kinds.items():
            if not isinstance(names, dict):
                continue
            for name, routes in names.items():
                manifest = _manifest_at(manifests, namespace, kind, name)
                if not isinstance(routes, list):
                    raise GitOpsTargetError(
                        f"routing for {namespace}/{kind}/{name} must be a list"
                    )
                for route in routes:
                    target = GitOpsTarget(
                        backend=_required_string(route, "backend"),
                        branch=_required_string(route, "branch"),
                        path=_required_string(route, "path"),
                    )
                    result[target].append(manifest)
    return dict(result)


__all__ = ["GitOpsTarget", "GitOpsTargetError", "routed_manifests"]
