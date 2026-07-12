from __future__ import annotations

import os
from pathlib import Path

import pytest

from ekn.eval import evaluate_file, evaluate_flake_ekn
from ekn.git import commit_manifests, flatten_manifests, diff_manifests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CUSTOMER_NIX = """\
{
  customer1 = {
    config = {
      gitops = { enable = true; branch = "test-render"; };
      kubernetes = {
        generatedByPath = {
          default = {
            ConfigMap = {
              "my-config" = {
                apiVersion = "v1"; kind = "ConfigMap";
                metadata = { name = "my-config"; namespace = "default"; };
                data = { key = "value"; };
              };
            };
          };
        };
      };
    };
  };
}
"""


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    import subprocess
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"],
                   capture_output=True, check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"],
                   capture_output=True, check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "--allow-empty", "-q", "-m", "root"],
                   capture_output=True, check=True)
    return repo


class TestEval:
    async def test_eval_callback(self, tmp_path: Path) -> None:
        f = tmp_path / "customers.nix"
        f.write_text(CUSTOMER_NIX)
        result = await evaluate_file(f, None)
        assert isinstance(result, dict)
        assert "customer1" in result

    async def test_eval_with_attr(self, tmp_path: Path) -> None:
        f = tmp_path / "customers.nix"
        f.write_text(CUSTOMER_NIX)
        result = await evaluate_file(f, "customer1")
        assert isinstance(result, dict)
        assert "config" in result


class TestDiff:
    async def test_diff_no_changes(self, tmp_path: Path, git_repo: Path) -> None:
        f = tmp_path / "customers.nix"
        f.write_text(CUSTOMER_NIX)
        os.environ["EKN_REPO"] = str(git_repo)
        try:
            result = await evaluate_file(f, "customer1")
            files = flatten_manifests(result["config"]["kubernetes"]["generatedByPath"])
            commit_manifests(".", result["config"]["gitops"]["branch"], files, "seed")
            diff_out = diff_manifests(".", result["config"]["gitops"]["branch"], files)
            assert diff_out is None
        finally:
            os.environ.pop("EKN_REPO", None)

    async def test_diff_without_gitops_errors(self, tmp_path: Path) -> None:
        f = tmp_path / "no.nix"
        f.write_text("""\
{ app = {
    config = {
      gitops = { enable = false; };
      kubernetes = { generatedByPath = {}; };
    };
  };
}
""")
        result = await evaluate_file(f, "app")
        assert result["config"]["gitops"]["enable"] is False

    async def test_diff_branch_override(self, tmp_path: Path, git_repo: Path) -> None:
        f = tmp_path / "customers.nix"
        f.write_text(CUSTOMER_NIX)
        os.environ["EKN_REPO"] = str(git_repo)
        try:
            result = await evaluate_file(f, "customer1")
            default_files = flatten_manifests({
                "default": {
                    "ConfigMap": {
                        "my-config": {
                            "apiVersion": "v1", "kind": "ConfigMap",
                            "metadata": {"name": "my-config", "namespace": "default"},
                            "data": {"key": "original"},
                        }
                    }
                },
            })
            commit_manifests(".", "override-branch", default_files, "first")
            new_files = flatten_manifests(result["config"]["kubernetes"]["generatedByPath"])
            diff_out = diff_manifests(".", "override-branch", new_files)
            assert diff_out is not None
            assert "my-config" in diff_out or "test" in diff_out
        finally:
            os.environ.pop("EKN_REPO", None)


class TestCommit:
    async def test_first_commit(self, tmp_path: Path, git_repo: Path) -> None:
        f = tmp_path / "customers.nix"
        f.write_text(CUSTOMER_NIX)
        os.environ["EKN_REPO"] = str(git_repo)
        try:
            result = await evaluate_file(f, "customer1")
            files = flatten_manifests(result["config"]["kubernetes"]["generatedByPath"])
            commit_id = commit_manifests(".", result["config"]["gitops"]["branch"], files, "test")
            assert isinstance(commit_id, str)
            assert len(commit_id) > 0
        finally:
            os.environ.pop("EKN_REPO", None)

    async def test_second_commit(self, tmp_path: Path, git_repo: Path) -> None:
        f = tmp_path / "customers.nix"
        f.write_text(CUSTOMER_NIX)
        os.environ["EKN_REPO"] = str(git_repo)
        try:
            result = await evaluate_file(f, "customer1")
            files = flatten_manifests(result["config"]["kubernetes"]["generatedByPath"])
            commit_manifests(".", result["config"]["gitops"]["branch"], files, "first")
            commit_id = commit_manifests(".", result["config"]["gitops"]["branch"], files, "second")
            assert isinstance(commit_id, str)
        finally:
            os.environ.pop("EKN_REPO", None)

    async def test_commit_branch_override(self, tmp_path: Path, git_repo: Path) -> None:
        f = tmp_path / "customers.nix"
        f.write_text(CUSTOMER_NIX)
        os.environ["EKN_REPO"] = str(git_repo)
        try:
            result = await evaluate_file(f, "customer1")
            files = flatten_manifests(result["config"]["kubernetes"]["generatedByPath"])
            commit_id = commit_manifests(".", "override", files, "override")
            assert isinstance(commit_id, str)
        finally:
            os.environ.pop("EKN_REPO", None)


EXAMPLE_FLAKE = str((Path(__file__).resolve().parent.parent / "docs/examples/example-flake").resolve())


class TestFlakeEval:
    async def test_flake_eval(self) -> None:
        result = await evaluate_flake_ekn(EXAMPLE_FLAKE, "myapp")
        assert result["config"]["gitops"]["branch"] == "flake-branch"
        assert "default" in result["config"]["kubernetes"]["generatedByPath"]

    async def test_flake_diff(self, git_repo: Path) -> None:
        os.environ["EKN_REPO"] = str(git_repo)
        try:
            result = await evaluate_flake_ekn(EXAMPLE_FLAKE, "myapp")
            files = flatten_manifests(result["config"]["kubernetes"]["generatedByPath"])
            commit_manifests(".", result["config"]["gitops"]["branch"], files, "seed")
            diff_out = diff_manifests(".", result["config"]["gitops"]["branch"], files)
            assert diff_out is None
        finally:
            os.environ.pop("EKN_REPO", None)

    async def test_flake_commit(self, git_repo: Path) -> None:
        os.environ["EKN_REPO"] = str(git_repo)
        try:
            result = await evaluate_flake_ekn(EXAMPLE_FLAKE, "myapp")
            files = flatten_manifests(result["config"]["kubernetes"]["generatedByPath"])
            commit_id = commit_manifests(".", result["config"]["gitops"]["branch"], files, "flake-test")
            assert isinstance(commit_id, str)
        finally:
            os.environ.pop("EKN_REPO", None)
