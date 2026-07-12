from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def test_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    os.environ["EKN_REPO"] = str(repo)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test"],
        cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "root"],
        cwd=repo, check=True,
    )
    return repo


@pytest.fixture(autouse=True)
def _clean_env() -> None:
    yield
    os.environ.pop("EKN_REPO", None)
