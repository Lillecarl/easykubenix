from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ekn.cli import app

runner = CliRunner()

CUSTOMER_NIX = """\
{
  customer1 = {
    config = {
      gitops = {
        enable = true;
        branch = "test-render";
      };
      kubernetes = {
        generatedByPath = {
          default = {
            ConfigMap = {
              "my-config" = {
                apiVersion = "v1";
                kind = "ConfigMap";
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

NO_GITOPS_NIX = """\
{
  app = {
    config = {
      gitops = {
        enable = false;
      };
      kubernetes = {
        generatedByPath = { };
      };
    };
  };
}
"""


@pytest.fixture
def customer_file(tmp_path: Path) -> Path:
    f = tmp_path / "customers.nix"
    f.write_text(CUSTOMER_NIX)
    return f


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
    def test_callback_eval(self, customer_file: Path) -> None:
        result = runner.invoke(app, ["-f", str(customer_file)])
        assert result.exit_code == 0, result.stdout
        data = json.loads(result.stdout)
        assert "customer1" in data

    def test_eval_subcommand(self, customer_file: Path) -> None:
        result = runner.invoke(app, ["eval", "-f", str(customer_file)])
        assert result.exit_code == 0, result.stdout
        data = json.loads(result.stdout)
        assert "customer1" in data

    def test_eval_with_attr(self, customer_file: Path) -> None:
        result = runner.invoke(app, ["eval", "-f", str(customer_file), "customer1"])
        assert result.exit_code == 0, result.stdout
        data = json.loads(result.stdout)
        assert "config" in data

    def test_eval_missing_file(self) -> None:
        result = runner.invoke(app, ["eval", "-f", "/nonexistent.nix"])
        assert result.exit_code != 0


class TestDiff:
    def test_diff_no_changes(self, customer_file: Path, git_repo: Path) -> None:
        os.environ["EKN_REPO"] = str(git_repo)
        try:
            _seed_branch(customer_file)
            result = runner.invoke(app, ["diff", "-f", str(customer_file), "customer1"])
            assert result.exit_code == 0, result.stdout
            assert "no differences" in result.stdout
        finally:
            os.environ.pop("EKN_REPO", None)

    def test_diff_without_gitops_errors(self, tmp_path: Path, git_repo: Path) -> None:
        f = tmp_path / "no.nix"
        f.write_text(NO_GITOPS_NIX)
        os.environ["EKN_REPO"] = str(git_repo)
        try:
            result = runner.invoke(app, ["diff", "-f", str(f), "app"])
            assert result.exit_code != 0
            assert "gitops is not enabled" in result.stderr
        finally:
            os.environ.pop("EKN_REPO", None)

    def test_diff_branch_override(self, customer_file: Path, git_repo: Path) -> None:
        from ekn.git import commit_manifests, flatten_manifests
        os.environ["EKN_REPO"] = str(git_repo)
        try:
            files = flatten_manifests({
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
            commit_manifests(".", "override-branch", files, "first")
            result = runner.invoke(app, [
                "diff", "-f", str(customer_file), "customer1", "-b", "override-branch",
            ])
            assert result.exit_code == 0, result.stdout
        finally:
            os.environ.pop("EKN_REPO", None)


class TestCommit:
    def test_first_commit(self, customer_file: Path, git_repo: Path) -> None:
        os.environ["EKN_REPO"] = str(git_repo)
        try:
            result = runner.invoke(app, ["commit", "-f", str(customer_file), "customer1", "-m", "test"])
            assert result.exit_code == 0, result.stdout
            assert "committed to test-render @" in result.stdout
        finally:
            os.environ.pop("EKN_REPO", None)

    def test_second_commit(self, customer_file: Path, git_repo: Path) -> None:
        os.environ["EKN_REPO"] = str(git_repo)
        try:
            _seed_branch(customer_file)
            result = runner.invoke(app, ["commit", "-f", str(customer_file), "customer1", "-m", "second"])
            assert result.exit_code == 0, result.stdout
            assert "committed to test-render @" in result.stdout
        finally:
            os.environ.pop("EKN_REPO", None)

    def test_commit_branch_override(self, customer_file: Path, git_repo: Path) -> None:
        os.environ["EKN_REPO"] = str(git_repo)
        try:
            result = runner.invoke(app, [
                "commit", "-f", str(customer_file), "customer1",
                "-b", "override", "-m", "override",
            ])
            assert result.exit_code == 0, result.stdout
            assert "committed to override @" in result.stdout
        finally:
            os.environ.pop("EKN_REPO", None)


def _seed_branch(customer_file: Path) -> None:
    result = runner.invoke(app, ["commit", "-f", str(customer_file), "customer1", "-m", "first"])
    assert result.exit_code == 0, result.stdout
