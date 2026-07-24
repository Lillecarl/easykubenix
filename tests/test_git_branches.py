from __future__ import annotations

from typing import TYPE_CHECKING

import pygit2
import pytest

from ekn.git import (
    ConcurrentDeployError,
    commit_manifests,
    finalize_branches,
    prepare_deploy_and_source_commits,
    rollback_branches,
    snapshot_source_tree,
)

if TYPE_CHECKING:
    from pathlib import Path


def _init_repo(path: Path) -> pygit2.Repository:
    repo = pygit2.init_repository(str(path))
    repo.config["user.name"] = "Test"
    repo.config["user.email"] = "test@example.com"
    return repo


def _write_blob_tree(repo: pygit2.Repository, path: str, content: str) -> pygit2.Oid:
    index = pygit2.Index()
    blob_id = repo.create_blob(content.encode())
    index.add(pygit2.IndexEntry(path, blob_id, pygit2.GIT_FILEMODE_BLOB))  # pyright: ignore[reportArgumentType] -- pygit2 GIT_FILEMODE_BLOB is an int but IndexEntry.mode's stub wants its own enum
    return index.write_tree(repo)


def test_prepare_finalize_two_deploys_with_source(tmp_path: Path) -> None:
    _init_repo(tmp_path)

    prepared1 = prepare_deploy_and_source_commits(
        str(tmp_path),
        "deploy",
        "source",
        [("app.yaml", "v1")],
        "deploy 1",
    )
    finalize_branches(str(tmp_path), "deploy", "source", prepared1)

    repo = pygit2.Repository(str(tmp_path))
    assert prepared1.source_oid is not None
    deploy1 = repo[prepared1.deploy_oid]
    source1 = repo[prepared1.source_oid]
    assert isinstance(deploy1, pygit2.Commit)
    assert isinstance(source1, pygit2.Commit)
    assert len(deploy1.parents) == 1
    assert deploy1.parents[0].id == source1.id
    assert len(source1.parents) == 0

    prepared2 = prepare_deploy_and_source_commits(
        str(tmp_path),
        "deploy",
        "source",
        [("app.yaml", "v2")],
        "deploy 2",
    )
    finalize_branches(str(tmp_path), "deploy", "source", prepared2)

    assert prepared2.source_oid is not None
    deploy2 = repo[prepared2.deploy_oid]
    source2 = repo[prepared2.source_oid]
    assert isinstance(deploy2, pygit2.Commit)
    assert isinstance(source2, pygit2.Commit)
    assert len(deploy2.parents) == 2
    assert deploy2.parents[0].id == deploy1.id
    assert deploy2.parents[1].id == source2.id
    assert len(source2.parents) == 1
    assert source2.parents[0].id == source1.id
    assert f"ekn-source: {source1.id}" in deploy1.message
    assert f"ekn-source: {source2.id}" in deploy2.message


def test_source_branch_none_falls_back_to_single_parent_commit(tmp_path: Path) -> None:
    _init_repo(tmp_path)

    commit_id = commit_manifests(str(tmp_path), "deploy", [("app.yaml", "v1")], "msg")
    repo = pygit2.Repository(str(tmp_path))
    commit = repo[commit_id]
    assert isinstance(commit, pygit2.Commit)
    assert len(commit.parents) == 0


def test_finalize_detects_concurrent_deploy(tmp_path: Path) -> None:
    _init_repo(tmp_path)

    prepared_a = prepare_deploy_and_source_commits(
        str(tmp_path),
        "deploy",
        "source",
        [("a.yaml", "a")],
        "a",
    )
    # A concurrent deploy finishes (prepare + finalize) before `prepared_a`
    # gets its turn to finalize.
    prepared_b = prepare_deploy_and_source_commits(
        str(tmp_path),
        "deploy",
        "source",
        [("b.yaml", "b")],
        "b",
    )
    finalize_branches(str(tmp_path), "deploy", "source", prepared_b)

    with pytest.raises(ConcurrentDeployError):
        finalize_branches(str(tmp_path), "deploy", "source", prepared_a)


def test_rollback_creates_new_commit_without_altering_history(tmp_path: Path) -> None:
    _init_repo(tmp_path)

    prepared1 = prepare_deploy_and_source_commits(
        str(tmp_path),
        "deploy",
        "source",
        [("a.yaml", "v1")],
        "deploy 1",
    )
    finalize_branches(str(tmp_path), "deploy", "source", prepared1)
    prepared2 = prepare_deploy_and_source_commits(
        str(tmp_path),
        "deploy",
        "source",
        [("a.yaml", "v2")],
        "deploy 2",
    )
    finalize_branches(str(tmp_path), "deploy", "source", prepared2)

    repo = pygit2.Repository(str(tmp_path))

    rolled_back = rollback_branches(str(tmp_path), "deploy", "source", steps_back=1)
    finalize_branches(str(tmp_path), "deploy", "source", rolled_back)

    # The old commits are untouched -- rollback only ever appends.
    old_deploy1 = repo[prepared1.deploy_oid]
    old_deploy2 = repo[prepared2.deploy_oid]
    assert isinstance(old_deploy1, pygit2.Commit)
    assert isinstance(old_deploy2, pygit2.Commit)
    assert old_deploy1.id == prepared1.deploy_oid
    assert old_deploy2.id == prepared2.deploy_oid

    assert rolled_back.source_oid is not None
    assert prepared1.source_oid is not None
    new_deploy = repo[rolled_back.deploy_oid]
    new_source = repo[rolled_back.source_oid]
    old_source1 = repo[prepared1.source_oid]
    assert isinstance(new_deploy, pygit2.Commit)
    assert isinstance(new_source, pygit2.Commit)
    assert isinstance(old_source1, pygit2.Commit)
    # Restored tree matches the first deploy's tree, not the second's.
    assert new_deploy.tree_id == old_deploy1.tree_id
    assert new_deploy.tree_id != old_deploy2.tree_id
    assert new_source.tree_id == old_source1.tree_id
    # Forward-only: the new commit is a fresh child of the current tip, not
    # a reset onto the restored commit.
    assert new_deploy.parents[0].id == prepared2.deploy_oid


def test_rollback_prefers_source_trailer_over_parent_order(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)

    prepared1 = prepare_deploy_and_source_commits(
        str(tmp_path),
        "deploy",
        "source",
        [("a.yaml", "v1")],
        "deploy 1",
    )
    finalize_branches(str(tmp_path), "deploy", "source", prepared1)

    author = repo.default_signature
    committer = repo.default_signature

    # A hand-crafted deploy commit whose parent *order* deliberately
    # contradicts prepare_deploy_and_source_commits' convention (source is
    # the FIRST parent here, not the last) -- proves rollback_branches reads
    # the explicit ekn-source trailer rather than assuming source is always
    # target_commit.parents[-1].
    swapped_source_tree = _write_blob_tree(repo, "a.yaml", "swapped-source")
    swapped_source_oid = repo.create_commit(
        None,
        author,
        committer,
        "swapped source",
        swapped_source_tree,
        [],
    )
    swapped_deploy_tree = _write_blob_tree(repo, "a.yaml", "swapped-deploy")
    swapped_deploy_oid = repo.create_commit(
        None,
        author,
        committer,
        f"swapped deploy\n\nekn-source: {swapped_source_oid}\n",
        swapped_deploy_tree,
        [swapped_source_oid, prepared1.deploy_oid],
    )
    repo.branches["deploy"].set_target(swapped_deploy_oid)

    rolled_back = rollback_branches(str(tmp_path), "deploy", "source", to=str(swapped_deploy_oid))

    assert rolled_back.source_oid is not None
    new_source = repo[rolled_back.source_oid]
    assert isinstance(new_source, pygit2.Commit)
    # Had rollback trusted parents[-1] instead, it would have recovered
    # prepared1's (wrong) source tree instead of the trailer-named one.
    assert new_source.tree_id == swapped_source_tree


def test_rollback_falls_back_to_last_parent_when_trailer_is_missing(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    author = repo.default_signature
    committer = repo.default_signature

    # A hand-crafted deploy commit mimicking the pre-trailer format: the
    # source commit is simply the deploy commit's last (only) parent, and
    # the message carries no ekn-source trailer at all -- proves
    # rollback_branches still recovers deploy history made before this
    # feature existed.
    historic_source_tree = _write_blob_tree(repo, "a.yaml", "historic-source")
    historic_source_oid = repo.create_commit(
        None,
        author,
        committer,
        "historic source",
        historic_source_tree,
        [],
    )
    historic_deploy_tree = _write_blob_tree(repo, "a.yaml", "historic-deploy")
    historic_deploy_oid = repo.create_commit(
        None,
        author,
        committer,
        "historic deploy",
        historic_deploy_tree,
        [historic_source_oid],
    )
    historic_deploy_commit = repo[historic_deploy_oid]
    assert isinstance(historic_deploy_commit, pygit2.Commit)
    repo.create_branch("deploy", historic_deploy_commit)

    rolled_back = rollback_branches(str(tmp_path), "deploy", "source", to=str(historic_deploy_oid))

    assert rolled_back.source_oid is not None
    new_source = repo[rolled_back.source_oid]
    assert isinstance(new_source, pygit2.Commit)
    assert new_source.tree_id == historic_source_tree


def test_snapshot_source_tree_respects_gitignore_and_untracked(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("secret.txt\n")
    (tmp_path / "secret.txt").write_text("shh")
    (tmp_path / "tracked.txt").write_text("hello")

    tree_id = snapshot_source_tree(str(tmp_path))
    tree = repo[tree_id]
    assert isinstance(tree, pygit2.Tree)
    names = {entry.name for entry in tree}
    assert "tracked.txt" in names
    assert ".gitignore" in names
    assert "secret.txt" not in names
