from __future__ import annotations

import pytest

from ekn.cli import _gitops_file_groups
from ekn.gitops import GitOpsTarget, GitOpsTargetError, resolved_targets


def _deployment() -> dict[str, object]:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "api", "namespace": "default"},
    }


def test_resolved_targets_groups_objects_by_target() -> None:
    gitops_targets = {
        "apps": {
            "target": {"branch": "deploy", "path": "clusters/home/apps"},
            "objects": [_deployment()],
        }
    }

    assert resolved_targets(gitops_targets) == {
        GitOpsTarget(branch="deploy", path="clusters/home/apps"): [_deployment()]
    }


def test_resolved_targets_merges_targets_sharing_branch_and_path() -> None:
    config_map = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "cfg", "namespace": "default"},
    }
    gitops_targets = {
        "apps": {
            "target": {"branch": "deploy", "path": "clusters/home/apps"},
            "objects": [_deployment()],
        },
        "apps-mirror": {
            "target": {"branch": "deploy", "path": "clusters/home/apps"},
            "objects": [config_map],
        },
    }

    result = resolved_targets(gitops_targets)

    key = GitOpsTarget(branch="deploy", path="clusters/home/apps")
    assert list(result) == [key]
    assert result[key] == [_deployment(), config_map]


def test_resolved_targets_requires_branch_and_path() -> None:
    gitops_targets = {"apps": {"target": {}, "objects": [_deployment()]}}

    with pytest.raises(GitOpsTargetError, match="branch"):
        resolved_targets(gitops_targets)


def test_file_groups_use_target_branch_and_path() -> None:
    result = {
        "config": {
            "kubernetes": {
                "gitopsTargets": {
                    "apps": {
                        "target": {"branch": "deploy", "path": "clusters/home/apps"},
                        "objects": [_deployment()],
                    }
                },
            }
        }
    }

    groups = _gitops_file_groups(result)

    assert list(groups) == ["deploy"]
    assert [path for path, _ in groups["deploy"]] == [
        "clusters/home/apps/default/Deployment/api.yaml"
    ]
