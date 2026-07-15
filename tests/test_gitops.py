from __future__ import annotations

import pytest
from ekn.cli import _gitops_file_groups
from ekn.gitops import GitOpsTarget, GitOpsTargetError, routed_manifests


def _deployment() -> dict[str, object]:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "api", "namespace": "default"},
    }


def test_argo_target_routes_payload() -> None:
    application = {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {"name": "apps", "namespace": "argocd"},
        "spec": {
            "source": {
                "repoURL": "ssh://git@example.test/platform.git",
                "targetRevision": "deploy",
                "path": "clusters/home/apps",
            }
        },
    }
    manifests = {
        "default": {"Deployment": {"api": _deployment()}},
        "argocd": {"Application": {"apps": application}},
    }
    routing = {
        "default": {
            "Deployment": {
                "api": [{"backend": "argo", "branch": "deploy", "path": "clusters/home/apps"}]
            }
        }
    }

    assert routed_manifests(manifests, routing) == {
        GitOpsTarget(
            backend="argo",
            branch="deploy",
            path="clusters/home/apps",
        ): [_deployment()]
    }


def test_flux_target_routes_payload() -> None:
    source = {
        "apiVersion": "source.toolkit.fluxcd.io/v1",
        "kind": "GitRepository",
        "metadata": {"name": "platform", "namespace": "flux-system"},
        "spec": {
            "url": "ssh://git@example.test/platform.git",
            "ref": {"branch": "deploy"},
        },
    }
    kustomization = {
        "apiVersion": "kustomize.toolkit.fluxcd.io/v1",
        "kind": "Kustomization",
        "metadata": {"name": "apps", "namespace": "flux-system"},
        "spec": {
            "path": "./clusters/home/apps",
            "sourceRef": {"kind": "GitRepository", "name": "platform"},
        },
    }
    manifests = {
        "default": {"Deployment": {"api": _deployment()}},
        "flux-system": {
            "GitRepository": {"platform": source},
            "Kustomization": {"apps": kustomization},
        },
    }
    routing = {
        "default": {
            "Deployment": {
                "api": [{"backend": "flux", "branch": "deploy", "path": "./clusters/home/apps"}]
            }
        }
    }

    assert routed_manifests(manifests, routing) == {
        GitOpsTarget(
            backend="flux",
            branch="deploy",
            path="./clusters/home/apps",
        ): [_deployment()]
    }


def test_route_requires_fields() -> None:
    manifests = {"default": {"Deployment": {"api": _deployment()}}}
    routing = {"default": {"Deployment": {"api": [{"backend": "argo"}]}}}

    with pytest.raises(GitOpsTargetError, match="branch"):
        routed_manifests(manifests, routing)


def test_file_groups_use_target_branch_and_path() -> None:
    application = {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {"name": "apps", "namespace": "argocd"},
        "spec": {
            "source": {
                "repoURL": "ssh://git@example.test/platform.git",
                "targetRevision": "deploy",
                "path": "clusters/home/apps",
            }
        },
    }
    result = {
        "config": {
            "kubernetes": {
                "generatedByPath": {
                    "default": {"Deployment": {"api": _deployment()}},
                    "argocd": {"Application": {"apps": application}},
                },
                "eknByPath": {
                    "default": {
                        "Deployment": {
                            "api": [{"backend": "argo", "branch": "deploy", "path": "clusters/home/apps"}]
                        }
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
