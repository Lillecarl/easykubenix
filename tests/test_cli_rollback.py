from __future__ import annotations

from typing import TYPE_CHECKING

import pygit2
import pytest

from ekn.cli import Rollback
from ekn.git import commit_manifests, finalize_branches, prepare_deploy_and_source_commits

if TYPE_CHECKING:
    from pathlib import Path


def _init_repo(path: Path) -> pygit2.Repository:
    repo = pygit2.init_repository(str(path))
    repo.config["user.name"] = "Test"
    repo.config["user.email"] = "test@example.com"
    return repo


async def test_rollback_reports_clean_error_for_an_unresolvable_to_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--to bogus-ref` must produce a clean `SystemExit(1)`, not an uncaught
    `KeyError` -- `pygit2.Repository.revparse_single` (called by
    `rollback_branches`) raises `KeyError`, not `ValueError`/`TypeError`,
    for a revision spec it can't resolve.
    """
    _init_repo(tmp_path)
    commit_manifests(str(tmp_path), "deploy", [("app.yaml", "v1")], "deploy 1")

    monkeypatch.chdir(tmp_path)
    rollback = Rollback(
        deploy_branch="deploy",
        source_branch=None,
        file=None,
        flake=None,
        attr=None,
        to="bogus-ref-that-does-not-exist",
        steps_back=1,
        push=False,
        remote="origin",
        verify=False,
    )

    with pytest.raises(SystemExit) as exc_info:
        await rollback.run()
    assert exc_info.value.code == 1


async def test_rollback_to_a_real_commit_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init_repo(tmp_path)
    prepared1 = prepare_deploy_and_source_commits(
        str(tmp_path),
        "deploy",
        "source",
        [("a.yaml", "v1")],
        "deploy 1",
    )
    finalize_branches(str(tmp_path), "deploy", "source", prepared1)
    prepared2 = prepare_deploy_and_source_commits(str(tmp_path), "deploy", "source", [("a.yaml", "v2")], "deploy 2")
    finalize_branches(str(tmp_path), "deploy", "source", prepared2)

    monkeypatch.chdir(tmp_path)
    rollback = Rollback(
        deploy_branch="deploy",
        source_branch="source",
        file=None,
        flake=None,
        attr=None,
        to=str(prepared1.deploy_oid),
        steps_back=1,
        push=False,
        remote="origin",
        verify=False,
    )

    await rollback.run()

    repo = pygit2.Repository(str(tmp_path))
    tip = repo.branches["deploy"].peel(pygit2.Commit)
    old_deploy1 = repo[prepared1.deploy_oid]
    old_deploy2 = repo[prepared2.deploy_oid]
    assert isinstance(old_deploy1, pygit2.Commit)
    assert isinstance(old_deploy2, pygit2.Commit)
    # Rollback restores prepared1's tree as a *new* commit on top of the
    # current tip -- it never resets/force-pushes -- so the new tip's tree
    # matches prepared1's, not prepared2's.
    assert tip.tree_id == old_deploy1.tree_id
    assert tip.tree_id != old_deploy2.tree_id
    assert tip.parents[0].id == prepared2.deploy_oid
