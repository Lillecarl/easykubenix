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
        raise GitOpsTargetError("GitOps target must be an attribute set")
    value = route.get(field)
    if not isinstance(value, str) or not value:
        raise GitOpsTargetError(f"GitOps target {field} must be a non-empty string")
    return value


def resolved_targets(gitops_targets: dict[str, Any]) -> dict[GitOpsTarget, list[dict[str, Any]]]:
    """Turn `kubernetes.gitopsTargets` (already joined by the Nix module) into
    `{GitOpsTarget: [manifest, ...]}`.

    The Nix side (`kubernetes.gitopsTargets`) has already resolved each
    object's `ekn.gitOpsTarget` name against `gitops.targets` and grouped
    objects by target name -- there is no index/lookup left to build here,
    just validation and a merge for the (unusual but valid) case of two
    named targets sharing the same branch+path.
    """
    result: defaultdict[GitOpsTarget, list[dict[str, Any]]] = defaultdict(list)
    for name, entry in gitops_targets.items():
        if not isinstance(entry, dict):
            raise GitOpsTargetError(f"GitOps target {name!r} entry must be an attribute set")
        objects = entry.get("objects")
        if not isinstance(objects, list):
            raise GitOpsTargetError(f"GitOps target {name!r} objects must be a list")
        target = GitOpsTarget(
            branch=_required_string(entry.get("target"), "branch"),
            path=_required_string(entry.get("target"), "path"),
        )
        result[target].extend(objects)
    return dict(result)


__all__ = ["GitOpsTarget", "GitOpsTargetError", "resolved_targets"]
