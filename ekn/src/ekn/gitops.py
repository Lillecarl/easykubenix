from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from collections.abc import Sequence

    from nanopynix.models import JsonValue

    # Type-only for a reason that outlives the annotation: ekn.eval imports
    # `load_raw_manifest` from this module at the top level, so a real import
    # of `ekn.eval` here would be a circular one.
    from ekn.apply import Manifest
    from ekn.eval import GitOpsManifestsResult, GitOpsTargetEntry


class GitOpsTargetError(ValueError):
    """Raised when Nix-produced GitOps routing data is invalid."""


@dataclass(frozen=True)
class GitOpsTarget:
    path: str


def load_raw_manifest(path: str) -> Manifest:
    """Read a `kubernetes.rawFiles` entry from disk and parse it -- in
    Python, not Nix. `yaml.safe_load` parses JSON too (JSON is a YAML
    subset) and, unlike `builtins.toJSON`, never reorders keys: Python
    dicts are insertion-ordered, and the loader inserts each key in the
    order it appears in the source document. That's the entire point of
    `rawFiles` -- see its description in easykubenix's kubernetes.nix.
    """
    data: JsonValue = yaml.load(Path(path).read_text(), Loader=yaml.CSafeLoader)
    if not isinstance(data, dict):
        raise GitOpsTargetError(f"raw manifest {path!r} must be a JSON/YAML object")
    return data


def resolved_targets(gitops_targets: dict[str, GitOpsTargetEntry]) -> dict[GitOpsTarget, list[Manifest]]:
    """Turn `kubernetes.gitOpsTargets` (already joined by the Nix module) into
    `{GitOpsTarget: [manifest, ...]}`.

    The Nix side (`kubernetes.gitOpsTargets`) has already resolved each
    object's `ekn.gitOpsTarget` name against `gitOps.targets` and grouped
    objects by target name -- there is no index/lookup left to build here,
    just a merge for the (unusual but valid) case of two named targets
    sharing the same path. Each entry is already validated (see
    `ekn.eval.GitOpsTargetEntry`) by the time it reaches here. The branch
    these all land on is instance-wide
    (`gitOps.deployBranch`/`gitOps.sourceBranch`), not part of a target --
    targets are pure path-routing.

    Each target's `rawFiles` (paths only, per easykubenix) are read and
    parsed here and appended to the same manifest list `objects` populates
    -- from here on a raw-file-sourced manifest is just a dict like any
    other; `flatten_manifests` writes it via the same `yaml.dump(...,
    sort_keys=False)` call either way, which is what actually preserves
    its order (parsed-from-file dicts already have the right order;
    Nix-evaluated ones don't, but weren't order-sensitive to begin with).
    """
    result: defaultdict[GitOpsTarget, list[Manifest]] = defaultdict(list)
    for entry in gitops_targets.values():
        target = GitOpsTarget(path=entry.target.path)
        result[target].extend(entry.objects)
        result[target].extend(load_raw_manifest(p) for p in entry.raw_files)
    return dict(result)


def flatten_manifests(  # noqa: C901 -- tracked complexity/arg-count debt, see TODO.md
    data: Sequence[JsonValue],
    subdir: str = "./",
    kustomize: bool = False,
) -> list[tuple[str, str]]:
    """Render a list of manifests to `(path, yaml_content)` pairs, optionally
    alongside a kustomize `kustomization.yaml` (plus a ksops generator for any
    manifest carrying a `sops` key). Pure YAML/kustomize rendering -- no git
    involvement, which is why this lives here rather than in git.py."""
    if not isinstance(data, list):
        raise TypeError(f"expected list, got {type(data).__name__}")

    base = PurePosixPath(subdir)
    files: list[tuple[str, str]] = []
    resources: list[str] = []
    generators: list[str] = []

    for manifest in data:
        if not isinstance(manifest, dict):
            continue
        metadata = manifest.get("metadata")
        kind = manifest.get("kind")
        if not isinstance(metadata, dict) or not isinstance(kind, str):
            continue
        namespace = metadata.get("namespace", "none")
        name = metadata.get("name")
        if not isinstance(namespace, str) or not isinstance(name, str):
            continue
        path = base / namespace / kind / f"{name}.yaml"
        # CSafeDumper (libyaml-backed) instead of the pure-Python Dumper:
        # benchmarked ~7.5x faster (5.0s -> 0.67s for a 371-object render,
        # dominated by CRDs' multi-hundred-KB bodies) with identical output
        # for plain JSON-like data.
        yaml_content = yaml.dump(
            manifest,
            default_flow_style=False,
            sort_keys=False,
            Dumper=yaml.CSafeDumper,
        )
        files.append((str(path), yaml_content))

        if not kustomize:
            continue
        rel_path = str(path.relative_to(base))
        if isinstance(manifest.get("sops"), dict):
            generator_path = base / namespace / kind / f"{name}.ksops-generator.yaml"
            generator_name = f"{namespace}-{kind.lower()}-{name}-ksops"
            generator_content = yaml.dump(
                {
                    "apiVersion": "viaduct.ai/v1",
                    "kind": "ksops",
                    "metadata": {
                        "name": generator_name,
                        "annotations": {
                            "config.kubernetes.io/function": "exec:\n  path: ksops\n",
                        },
                    },
                    # Relative to the kustomization root (where kustomize
                    # invokes the ksops KRM function from), not to the
                    # generator file's own directory -- a bare "{name}.yaml"
                    # here fails at kustomize-build time with "no such file
                    # or directory" once the generator and plain file live
                    # in a subdirectory (namespace/kind/), which is always.
                    "files": [rel_path],
                },
                default_flow_style=False,
                sort_keys=False,
                Dumper=yaml.CSafeDumper,
            )
            files.append((str(generator_path), generator_content))
            generators.append(str(generator_path.relative_to(base)))
        else:
            resources.append(rel_path)

    if kustomize:
        kustomization: dict[str, Any] = {
            "apiVersion": "kustomize.config.k8s.io/v1beta1",
            "kind": "Kustomization",
        }
        if resources:
            kustomization["resources"] = resources
        if generators:
            kustomization["generators"] = generators
        kustomization_content = yaml.dump(
            kustomization,
            default_flow_style=False,
            sort_keys=False,
            Dumper=yaml.CSafeDumper,
        )
        files.append((str(base / "kustomization.yaml"), kustomization_content))

    return files


def branches(result: GitOpsManifestsResult) -> tuple[str, str | None]:
    """Read the instance-wide `(deployBranch, sourceBranch)` pair.

    One pair per easykubenix instance -- an "environment" is just whichever
    `--file`/`--flake`+`--attr` entrypoint you evaluate, so there is no
    separate environment concept here. `sourceBranch` is `None` when the
    dual-commit source-snapshot feature is disabled for this instance.
    Validated by `GitOpsBranches` (see eval.py) at evaluation time, so
    there's nothing left to check here.
    """
    branch_config = result.config.git_ops
    return branch_config.deploy_branch, branch_config.source_branch


def file_groups(result: GitOpsManifestsResult) -> list[tuple[str, str]]:
    """Merge every GitOps target's rendered objects into one file list.

    Targets are pure path-routing (`gitOps.targets.<name>.path`) -- there is
    only one `(deployBranch, sourceBranch)` pair per instance (see
    `branches`), so there is nothing left to group by branch here.
    """
    gitops_targets = result.config.kubernetes.gitops_targets
    if not gitops_targets:
        raise GitOpsTargetError("no GitOps-routed Kubernetes objects found")

    routed = resolved_targets(gitops_targets)

    files: dict[str, str] = {}
    for target, target_manifests in routed.items():
        for path, content in flatten_manifests(target_manifests, target.path, kustomize=True):
            existing = files.get(path)
            if existing is not None and existing != content:
                raise GitOpsTargetError(f"conflicting generated content for {path}")
            files[path] = content
    return list(files.items())


__all__ = [
    "GitOpsTarget",
    "GitOpsTargetError",
    "branches",
    "file_groups",
    "flatten_manifests",
    "load_raw_manifest",
    "resolved_targets",
]
