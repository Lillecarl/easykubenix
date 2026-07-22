from __future__ import annotations

from pathlib import Path

import pytest

from ekn.git import commit_manifests, diff_manifests, flatten_manifests

SAMPLE_MANIFESTS = [
    {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "my-config", "namespace": "default"},
        "data": {"key": "value"},
    },
    {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "cluster-config", "namespace": "kube-system"},
        "data": {"setting": "true"},
    },
]


class TestFlattenManifests:
    def test_list(self) -> None:
        files = flatten_manifests(SAMPLE_MANIFESTS)
        paths = [p for p, _ in files]
        assert "default/ConfigMap/my-config.yaml" in paths
        assert "kube-system/ConfigMap/cluster-config.yaml" in paths

    def test_content_yaml(self) -> None:
        files = flatten_manifests(SAMPLE_MANIFESTS)
        _, content = files[0]
        assert "apiVersion: v1" in content
        assert "kind: ConfigMap" in content

    def test_subdir(self) -> None:
        files = flatten_manifests(SAMPLE_MANIFESTS, subdir="./clusters/prod")
        paths = [p for p, _ in files]
        assert "clusters/prod/default/ConfigMap/my-config.yaml" in paths

    def test_not_list(self) -> None:
        with pytest.raises(TypeError, match="expected list"):
            flatten_manifests({"default": {}})


SOPS_MANIFESTS = [
    *SAMPLE_MANIFESTS,
    {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "eso-creds", "namespace": "default"},
        "sops": {"age": [], "version": "3.9.0"},
        "data": {"token": "ENC[...]"},
    },
]


class TestFlattenManifestsKustomize:
    def test_plain_only_kustomization(self) -> None:
        files = flatten_manifests(SAMPLE_MANIFESTS, kustomize=True)
        paths = dict(files)
        assert "kustomization.yaml" in paths
        kustomization = paths["kustomization.yaml"]
        assert "default/ConfigMap/my-config.yaml" in kustomization
        assert "kube-system/ConfigMap/cluster-config.yaml" in kustomization
        assert "generators" not in kustomization

    def test_sops_object_gets_ksops_generator(self) -> None:
        files = flatten_manifests(SOPS_MANIFESTS, kustomize=True)
        paths = dict(files)
        assert "default/Secret/eso-creds.ksops-generator.yaml" in paths
        generator = paths["default/Secret/eso-creds.ksops-generator.yaml"]
        assert "kind: ksops" in generator
        assert "eso-creds.yaml" in generator

        kustomization = paths["kustomization.yaml"]
        assert "default/Secret/eso-creds.yaml" not in kustomization
        assert "default/Secret/eso-creds.ksops-generator.yaml" in kustomization

    def test_subdir_relative_paths(self) -> None:
        files = flatten_manifests(SOPS_MANIFESTS, subdir="./bootstrap", kustomize=True)
        paths = dict(files)
        kustomization = paths["bootstrap/kustomization.yaml"]
        assert "default/ConfigMap/my-config.yaml" in kustomization
        assert "bootstrap/" not in kustomization


class TestCommit:
    def test_first_commit(self, test_repo: Path) -> None:
        files = flatten_manifests(SAMPLE_MANIFESTS)
        sha = commit_manifests(".", "test-render", files, "first commit")
        assert sha
        parents = (
            test_repo / ".git" / "refs" / "heads" / "test-render"
        ).read_text().strip()
        assert len(parents) == 40

    def test_second_commit(self, test_repo: Path) -> None:
        files = flatten_manifests(SAMPLE_MANIFESTS)
        commit_manifests(".", "test-render", files, "first")
        sha2 = commit_manifests(".", "test-render", files, "second")
        assert sha2

    def test_diff_none(self, test_repo: Path) -> None:
        files = flatten_manifests(SAMPLE_MANIFESTS)
        commit_manifests(".", "test-render", files, "first")
        result = diff_manifests(".", "test-render", files)
        assert result is None

    def test_diff_change(self, test_repo: Path) -> None:
        files = flatten_manifests(SAMPLE_MANIFESTS)
        commit_manifests(".", "test-render", files, "first")

        changed = [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "my-config", "namespace": "default"},
                "data": {"key": "updated"},
            },
        ]
        new_files = flatten_manifests(changed)
        result = diff_manifests(".", "test-render", new_files)
        assert result is not None
        assert "key: updated" in result
