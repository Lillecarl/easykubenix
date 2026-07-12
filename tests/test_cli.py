from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ekn.cli import app

runner = CliRunner()

SIMPLE_NIX = """\
{
  default = {
    ConfigMap = {
      my-config = {
        apiVersion = "v1";
        kind = "ConfigMap";
        metadata = { name = "my-config"; namespace = "default"; };
        data = { key = "value"; };
      };
    };
  };
}
"""

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
}


def _seed_branch(nix_file: Path, branch: str = "test-render") -> None:
    result = runner.invoke(app, ["commit", "-f", str(nix_file), "-b", branch, "-m", "first"])
    assert result.exit_code == 0, result.stdout


@pytest.fixture
def nix_file(tmp_path: Path) -> Path:
    f = tmp_path / "config.nix"
    f.write_text(SIMPLE_NIX)
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
    def test_callback_eval(self, nix_file: Path) -> None:
        result = runner.invoke(app, ["-f", str(nix_file)])
        assert result.exit_code == 0, result.stdout
        data = json.loads(result.stdout)
        assert "default" in data

    def test_eval_subcommand(self, nix_file: Path) -> None:
        result = runner.invoke(app, ["eval", "-f", str(nix_file)])
        assert result.exit_code == 0, result.stdout
        data = json.loads(result.stdout)
        assert "default" in data

    def test_eval_with_attr(self, nix_file: Path) -> None:
        result = runner.invoke(app, ["eval", "-f", str(nix_file), "default.ConfigMap"])
        assert result.exit_code == 0, result.stdout
        data = json.loads(result.stdout)
        assert "my-config" in data

    def test_eval_missing_file(self) -> None:
        result = runner.invoke(app, ["eval", "-f", "/nonexistent.nix"])
        assert result.exit_code != 0


class TestDiff:
    def test_diff_empty_branch(self, nix_file: Path, git_repo: Path) -> None:
        os.environ["EKN_REPO"] = str(git_repo)
        try:
            result = runner.invoke(app, ["diff", "-f", str(nix_file), "-b", "test-render"])
            assert result.exit_code == 0, result.stdout
        finally:
            os.environ.pop("EKN_REPO", None)

    def test_diff_no_changes(self, nix_file: Path, git_repo: Path) -> None:
        os.environ["EKN_REPO"] = str(git_repo)
        try:
            _seed_branch(nix_file)
            result = runner.invoke(app, ["diff", "-f", str(nix_file), "-b", "test-render"])
            assert result.exit_code == 0, result.stdout
            assert "no differences" in result.stdout
        finally:
            os.environ.pop("EKN_REPO", None)


class TestCommit:
    def test_first_commit(self, nix_file: Path, git_repo: Path) -> None:
        os.environ["EKN_REPO"] = str(git_repo)
        try:
            result = runner.invoke(app, ["commit", "-f", str(nix_file), "-b", "test-render", "-m", "test"])
            assert result.exit_code == 0, result.stdout
            assert "committed to test-render @" in result.stdout
        finally:
            os.environ.pop("EKN_REPO", None)

    def test_second_commit(self, nix_file: Path, git_repo: Path) -> None:
        os.environ["EKN_REPO"] = str(git_repo)
        try:
            _seed_branch(nix_file)
            result = runner.invoke(app, ["commit", "-f", str(nix_file), "-b", "test-render", "-m", "second"])
            assert result.exit_code == 0, result.stdout
            assert "committed to test-render @" in result.stdout
        finally:
            os.environ.pop("EKN_REPO", None)
