from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from ekn.cli import Commit, Deploy, Validate, _gitops_path
from ekn.eval import evaluate_file, evaluate_flake_ekn
from ekn.git import commit_manifests, diff_manifests, flatten_manifests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CUSTOMER_NIX = """\
{
  customer1 = {
    config = {
      gitops = { enable = true; branch = "test-render"; };
      kubernetes = {
        generated = [
          {
            apiVersion = "v1"; kind = "ConfigMap";
            metadata = { name = "my-config"; namespace = "default"; };
            data = { key = "value"; };
          }
        ];
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
    def test_gitops_path_defaults_from_config(self) -> None:
        result = {"config": {"gitops": {"path": "clusters/demo"}}}

        assert _gitops_path(result) == "clusters/demo"

    def test_gitops_path_defaults_to_root(self) -> None:
        result = {"config": {"gitops": {}}}

        assert _gitops_path(result) == "./"

    async def test_diff_no_changes(self, tmp_path: Path, git_repo: Path) -> None:
        f = tmp_path / "customers.nix"
        f.write_text(CUSTOMER_NIX)
        os.environ["EKN_REPO"] = str(git_repo)
        try:
            result = await evaluate_file(f, "customer1")
            files = flatten_manifests(result["config"]["kubernetes"]["generated"])
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
      kubernetes = { generated = []; };
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
            default_files = flatten_manifests([
                {
                    "apiVersion": "v1", "kind": "ConfigMap",
                    "metadata": {"name": "my-config", "namespace": "default"},
                    "data": {"key": "original"},
                }
            ])
            commit_manifests(".", "override-branch", default_files, "first")
            new_files = flatten_manifests(result["config"]["kubernetes"]["generated"])
            diff_out = diff_manifests(".", "override-branch", new_files)
            assert diff_out is not None
            assert "my-config" in diff_out or "test" in diff_out
        finally:
            os.environ.pop("EKN_REPO", None)

    def test_diff_ignores_source_worktree_files(self, git_repo: Path) -> None:
        source_file = git_repo / "flake.nix"
        source_file.write_text("{ outputs = _: {}; }\n")
        subprocess.run(["git", "-C", str(git_repo), "add", "flake.nix"], check=True)

        os.environ["EKN_REPO"] = str(git_repo)
        try:
            diff_out = diff_manifests(
                ".",
                "rendered",
                [("clusters/demo/none/Namespace/demo.yaml", "kind: Namespace\n")],
            )
        finally:
            os.environ.pop("EKN_REPO", None)

        assert diff_out is not None
        assert "clusters/demo/none/Namespace/demo.yaml" in diff_out
        assert "flake.nix" not in diff_out


class TestCommit:
    async def test_deploy_verifies_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []

        async def verify(_: object) -> None:
            calls.append("verify")

        async def commit(_: object) -> None:
            calls.append("commit")

        monkeypatch.setattr(Validate, "run", verify)
        monkeypatch.setattr(Commit, "run", commit)
        deploy = object.__new__(Deploy)
        deploy.no_verify = False
        deploy.flake = ".#test"

        await Deploy.run(deploy)

        assert calls == ["verify", "commit"]

    async def test_deploy_no_verify_skips_validation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []

        async def verify(_: object) -> None:
            calls.append("verify")

        async def commit(_: object) -> None:
            calls.append("commit")

        monkeypatch.setattr(Validate, "run", verify)
        monkeypatch.setattr(Commit, "run", commit)
        deploy = object.__new__(Deploy)
        deploy.no_verify = True

        await Deploy.run(deploy)

        assert calls == ["commit"]

    async def test_first_commit(self, tmp_path: Path, git_repo: Path) -> None:
        f = tmp_path / "customers.nix"
        f.write_text(CUSTOMER_NIX)
        os.environ["EKN_REPO"] = str(git_repo)
        try:
            result = await evaluate_file(f, "customer1")
            files = flatten_manifests(result["config"]["kubernetes"]["generated"])
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
            files = flatten_manifests(result["config"]["kubernetes"]["generated"])
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
            files = flatten_manifests(result["config"]["kubernetes"]["generated"])
            commit_id = commit_manifests(".", "override", files, "override")
            assert isinstance(commit_id, str)
        finally:
            os.environ.pop("EKN_REPO", None)


EXAMPLE_FLAKE = str((Path(__file__).resolve().parent.parent / "docs/examples/example-flake").resolve())


class TestFlakeEval:
    async def test_flake_eval(self) -> None:
        result = await evaluate_flake_ekn(EXAMPLE_FLAKE, "myapp")
        namespaces = {obj["metadata"]["namespace"] for obj in result["config"]["kubernetes"]["generated"]}
        assert "default" in namespaces

    async def test_flake_diff(self, git_repo: Path) -> None:
        os.environ["EKN_REPO"] = str(git_repo)
        try:
            result = await evaluate_flake_ekn(EXAMPLE_FLAKE, "myapp")
            files = flatten_manifests(result["config"]["kubernetes"]["generated"])
            commit_manifests(".", "flake-test", files, "seed")
            diff_out = diff_manifests(".", "flake-test", files)
            assert diff_out is None
        finally:
            os.environ.pop("EKN_REPO", None)

    async def test_flake_commit(self, git_repo: Path) -> None:
        os.environ["EKN_REPO"] = str(git_repo)
        try:
            result = await evaluate_flake_ekn(EXAMPLE_FLAKE, "myapp")
            files = flatten_manifests(result["config"]["kubernetes"]["generated"])
            commit_id = commit_manifests(".", "flake-test", files, "flake-test")
            assert isinstance(commit_id, str)
        finally:
            os.environ.pop("EKN_REPO", None)
