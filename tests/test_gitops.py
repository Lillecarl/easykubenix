from __future__ import annotations

import pytest
from pydantic import ValidationError

from ekn.eval import GitOpsManifestsResult, GitOpsTargetEntry
from ekn.gitops import GitOpsTarget, branches, file_groups, resolved_targets


def _deployment() -> dict[str, object]:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "api", "namespace": "default"},
    }


def _entries(raw: dict[str, object]) -> dict[str, GitOpsTargetEntry]:
    """Validate raw Nix-shaped target entries, as `evaluate_gitops_manifests` does."""
    return {name: GitOpsTargetEntry.model_validate(entry) for name, entry in raw.items()}


def test_resolved_targets_groups_objects_by_target() -> None:
    gitops_targets = _entries(
        {
            "apps": {
                "target": {"path": "clusters/home/apps"},
                "objects": [_deployment()],
            }
        }
    )

    assert resolved_targets(gitops_targets) == {GitOpsTarget(path="clusters/home/apps"): [_deployment()]}


def test_resolved_targets_merges_targets_sharing_a_path() -> None:
    config_map = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "cfg", "namespace": "default"},
    }
    gitops_targets = _entries(
        {
            "apps": {
                "target": {"path": "clusters/home/apps"},
                "objects": [_deployment()],
            },
            "apps-mirror": {
                "target": {"path": "clusters/home/apps"},
                "objects": [config_map],
            },
        }
    )

    result = resolved_targets(gitops_targets)

    key = GitOpsTarget(path="clusters/home/apps")
    assert list(result) == [key]
    assert result[key] == [_deployment(), config_map]


def test_target_entry_requires_a_path() -> None:
    # A target without a path is rejected at the Nix boundary by the model,
    # not later by resolved_targets.
    with pytest.raises(ValidationError, match="path"):
        GitOpsTargetEntry.model_validate({"target": {}, "objects": [_deployment()]})


def _result(**gitops: object) -> GitOpsManifestsResult:
    return GitOpsManifestsResult.model_validate(
        {
            "config": {
                "deployment": {"deployBranch": "deploy", **gitops},
                "kubernetes": {
                    "deploymentUnits": {
                        "apps": {
                            "target": {"path": "clusters/home/apps"},
                            "objects": [_deployment()],
                        }
                    },
                },
            }
        }
    )


def test_file_groups_use_the_target_path() -> None:
    groups = file_groups(_result())

    assert [path for path, _ in groups] == [
        "clusters/home/apps/default/Deployment/api.yaml",
        "clusters/home/apps/kustomization.yaml",
    ]


def test_branches_are_instance_wide() -> None:
    # One pair per easykubenix instance -- targets do not carry a branch.
    assert branches(_result()) == ("deploy", None)
    assert branches(_result(sourceBranch="src")) == ("deploy", "src")
