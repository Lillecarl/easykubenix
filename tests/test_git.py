from __future__ import annotations

from pathlib import Path

import pytest

from ekn.git import commit_manifests, diff_manifests, flatten_manifests

SAMPLE_MANIFESTS = {
    "default": {
        "ConfigMap": {
            "my-config": {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "my-config", "namespace": "default"},
                "data": {"key": "value"},
            }
        }
    },
    "kube-system": {
        "ConfigMap": {
            "cluster-config": {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "cluster-config", "namespace": "kube-system"},
                "data": {"setting": "true"},
            }
        }
    },
}


class TestFlattenManifests:
    def test_dict(self) -> None:
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

    def test_not_dict(self) -> None:
        with pytest.raises(TypeError, match="expected dict"):
            flatten_manifests([1, 2, 3])


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

        changed = {
            "default": {
                "ConfigMap": {
                    "my-config": {
                        "apiVersion": "v1",
                        "kind": "ConfigMap",
                        "metadata": {"name": "my-config", "namespace": "default"},
                        "data": {"key": "updated"},
                    }
                }
            },
        }
        new_files = flatten_manifests(changed)
        result = diff_manifests(".", "test-render", new_files)
        assert result is not None
        assert "key: updated" in result
