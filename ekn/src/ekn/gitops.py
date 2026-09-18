from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

import structlog
import yaml

from ekn import seeds

if TYPE_CHECKING:
    from collections.abc import Sequence

    from nanopynix.models import JsonValue

    # Type-only for a reason that outlives the annotation: ekn.eval imports
    # `load_raw_manifest` from this module at the top level, so a real import
    # of `ekn.eval` here would be a circular one.
    from ekn.apply import Manifest
    from ekn.eval import GitOpsManifestsResult, GitOpsTargetEntry


_log = structlog.get_logger()


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
    """Turn `kubernetes.deploymentUnits` (already joined by the Nix module) into
    `{GitOpsTarget: [manifest, ...]}`.

    The Nix side (`kubernetes.deploymentUnits`) has already resolved each
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
        # dominated by CRDs' multi-hundred-KB bodies).
        #
        # **`CSafeDumper` is `CEmitter` + the pure-Python `SafeRepresenter`.**
        # Only emission is C; the walk that turns objects into a node tree is
        # not, and it becomes the dominant cost once the emitter goes. Measured
        # over 200 CRD-shaped objects:
        #
        #   SafeDumper    5.483s   emitter.py 67.1%, representer.py  2.0%
        #   CSafeDumper   0.237s   emitter.py absent, representer.py 69.5%
        #
        # So a profile of this path showing `yaml/representer.py` at the top is
        # evidence that libyaml is already in use, not that it is missing.
        # Switching dumper cannot remove that time; only not using PyYAML's
        # representer could. Do not read those frames as a missed 7x.
        #
        # On the text: with these settings the two dumpers agree on plain
        # scalars and disagree on long double-quoted ones, where they break the
        # line in different places and `width` does not reconcile them. Both
        # parse equal. Neither case was measured against a real deploy tree, so
        # read that as "they can differ" rather than as a survey of where.
        #
        # It is recorded because these files are committed. Changing what emits
        # them reflows the tree once, which is a review cost rather than a
        # correctness one, and it is the reason to leave this dumper alone
        # absent a better motive than speed.
        #
        # The representer cost is per node, so it falls on whatever is largest
        # -- CRDs and chart output, which arrived as text from disk and were
        # never touched in Nix. Re-deriving them here walks bytes that were
        # already correct on the way in. That is the output-side half of the
        # argument in #35: data from disk should be parsed once in a sandbox
        # and carried as cached JSON, rather than round-tripped through Nix
        # values at both ends. Replacing this representer treats the symptom.
        #
        # It is not the git half. `pygit2` takes the finished `(path,
        # content)` pairs, and `_resolve_gitops` builds them once for `diff`
        # and `commit` together, so nothing here is serialised twice.
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


def _identity(manifest: Manifest, path: str) -> str:
    metadata = manifest.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    namespace = metadata.get("namespace") or "none"
    return f"{path}: {namespace}/{manifest.get('kind')}/{metadata.get('name')}"


def without_seeded(manifests: list[Manifest], path: str) -> tuple[list[Manifest], list[str]]:
    """The manifests safe to commit, and a description of each one withheld.

    **A seeded object must not reach a branch another applier reads.** It
    carries `$ekn:env:VARNAME` where a credential goes, and only `ekn`
    resolves that. A controller syncing the committed file applies the
    sentinel as the literal value, over the credential `ekn kubeapply` had
    already put there -- and nothing in the file distinguishes the two,
    because staying schema-valid is the whole point of a string sentinel.

    `kubernetes.generatedExportable` states this rule, but
    `kubernetes.deploymentUnits` reads `generated` rather than that, so a
    unit whose own `modules` render a seeded Secret went straight past it.
    The `seededGitOpsObjects` assertion misses the same case: it fires on an
    object *routed* to a unit, and a unit's own modules route nothing.
    Issue #14.

    The field itself keeps the object, deliberately. `ekn kubeapply --target
    <name>` has to apply it -- delivering that credential is the whole
    reason seeds exist. Only what gets written to a branch is filtered.
    """
    committable: list[Manifest] = []
    withheld: list[str] = []
    for manifest in manifests:
        if seeds.is_seeded(manifest):
            withheld.append(_identity(manifest, path))
        else:
            committable.append(manifest)
    return committable, withheld


def file_groups(result: GitOpsManifestsResult) -> list[tuple[str, str]]:
    """Merge every GitOps target's rendered objects into one file list.

    Targets are pure path-routing (`gitOps.targets.<name>.path`) -- there is
    only one `(deployBranch, sourceBranch)` pair per instance (see
    `branches`), so there is nothing left to group by branch here.

    Seeded objects are left out -- see `without_seeded`.
    """
    gitops_targets = result.config.kubernetes.gitops_targets
    # An empty set is not an error here any more. A `tf`-only instance routes
    # no Kubernetes object at all and still has something to commit, so the
    # "nothing to commit" check belongs where both sources are known -- see
    # `_resolve_gitops` in cli.py.
    routed = resolved_targets(gitops_targets)

    files: dict[str, str] = {}
    withheld: list[str] = []
    for target, target_manifests in routed.items():
        committable, target_withheld = without_seeded(target_manifests, target.path)
        withheld.extend(target_withheld)
        for path, content in flatten_manifests(committable, target.path, kustomize=True):
            existing = files.get(path)
            if existing is not None and existing != content:
                raise GitOpsTargetError(f"conflicting generated content for {path}")
            files[path] = content
    if withheld:
        listed = "\n".join(f"  {entry}" for entry in withheld)
        # Said out loud, because the branch is rewritten from this list: an
        # object left out of it is deleted from the branch on this commit,
        # not merely not-added.
        _log.warning(
            "not committing seeded object(s) -- only `ekn kubeapply` can resolve "
            f"`$ekn:env:` references, and a controller syncing the file would apply the "
            f"sentinel over the live credential. They are removed from the branch if "
            f"present.\n{listed}"
        )
    return list(files.items())


__all__ = [
    "GitOpsTarget",
    "GitOpsTargetError",
    "branches",
    "file_groups",
    "flatten_manifests",
    "load_raw_manifest",
    "resolved_targets",
    "without_seeded",
]
