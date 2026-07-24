from __future__ import annotations

import asyncio
import io
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pygit2
from anyio import Path as AsyncPath

_SOURCE_TRAILER_KEY = "ekn-source"
_SOURCE_TRAILER_RE = re.compile(rf"^{re.escape(_SOURCE_TRAILER_KEY)}: (?P<oid>[0-9a-f]{{4,40}})$", re.MULTILINE)


def _repo_path(path: str | None = None) -> str:
    return os.environ.get("EKN_REPO") or path or "."


def _with_source_trailer(message: str, source_oid: pygit2.Oid) -> str:
    """Append an `ekn-source: <oid>` trailer to a deploy commit's message,
    recording its paired source commit explicitly rather than leaving
    `rollback_branches` to reverse-engineer it from parent order (a
    contract that was previously enforced only by a comment on
    `prepare_deploy_and_source_commits`)."""
    return f"{message.rstrip()}\n\n{_SOURCE_TRAILER_KEY}: {source_oid}\n"


def _read_source_trailer(commit: pygit2.Commit) -> pygit2.Oid | None:
    match = _SOURCE_TRAILER_RE.search(commit.message)
    if match is None:
        return None
    return pygit2.Oid(hex=match.group("oid"))


def _build_tree(repo: Any, files: list[tuple[str, str]]) -> Any:
    index = pygit2.Index()
    for rel_path, content in files:
        blob_id = repo.create_blob(content.encode("utf-8"))
        entry = pygit2.IndexEntry(rel_path, blob_id, pygit2.GIT_FILEMODE_BLOB)  # pyright: ignore[reportArgumentType] -- pygit2 types IndexEntry.mode as FileMode but exposes GIT_FILEMODE_BLOB as a plain int
        index.add(entry)
    return index.write_tree(repo)


def snapshot_source_tree(repo_path: str) -> pygit2.Oid:
    """Build a git tree from the current working directory: tracked files'
    working-copy content *and* untracked files, filtered only by
    `.gitignore`/`path_is_ignored` -- this mirrors exactly what Nix itself
    reads off disk, so the snapshot is never misleadingly incomplete just
    because a file wasn't `jj file track`ed/`git add`ed yet. Not
    `Index.add_all()`/`repo.status()` -- those reflect git's own index,
    which is stale/unreliable to rely on on top of a jj-managed working
    copy.

    SECRETS-SCANNING HOOK: this is where a future secrets scanner should
    inspect each file's path/content before it becomes a blob -- the
    natural place to reject a snapshot outright before any git object is
    created from it.
    """
    repo = pygit2.Repository(_repo_path(repo_path))
    workdir = repo.workdir
    if workdir is None:  # pyright: ignore[reportUnnecessaryComparison] -- pygit2's stub types workdir as always str, but a bare repo returns None at runtime
        raise ValueError("repository has no working directory")

    workdir_path = Path(workdir)
    index = pygit2.Index()
    for root, dirnames, filenames in os.walk(workdir):
        root_path = Path(root)
        kept_dirnames: list[str] = []
        for dirname in dirnames:
            if dirname in (".git", ".jj"):
                continue
            rel_dir = str((root_path / dirname).relative_to(workdir_path))
            if repo.path_is_ignored(rel_dir):
                continue
            kept_dirnames.append(dirname)
        dirnames[:] = kept_dirnames

        for filename in filenames:
            abs_path = root_path / filename
            rel_path = str(abs_path.relative_to(workdir_path))
            if repo.path_is_ignored(rel_path):
                continue
            if abs_path.is_symlink():
                blob_id = repo.create_blob(str(abs_path.readlink()).encode("utf-8"))
                mode = pygit2.GIT_FILEMODE_LINK
            else:
                blob_id = repo.create_blob_fromworkdir(rel_path)
                mode = pygit2.GIT_FILEMODE_BLOB_EXECUTABLE if os.access(abs_path, os.X_OK) else pygit2.GIT_FILEMODE_BLOB
            entry = pygit2.IndexEntry(rel_path, blob_id, mode)  # pyright: ignore[reportArgumentType] -- pygit2 GIT_FILEMODE_* are ints but IndexEntry.mode's stub wants its own enum, same as _build_tree below
            index.add(entry)
    return index.write_tree(repo)


def commit_manifests(
    repo_path: str,
    branch_name: str,
    files: list[tuple[str, str]],
    message: str,
) -> str:
    repo = pygit2.Repository(_repo_path(repo_path))
    tree_id = _build_tree(repo, files)

    try:
        branch = repo.branches[branch_name]
        parent_commits = [branch.target]
    except KeyError:
        parent_commits = []

    author = repo.default_signature
    committer = repo.default_signature
    commit_id = repo.create_commit(
        None,
        author,
        committer,
        message,
        tree_id,
        parent_commits,
    )

    commit = repo[commit_id]
    if not isinstance(commit, pygit2.Commit):  # pyright: ignore[reportUnnecessaryIsInstance] -- pygit2-stubs models Object/Commit as unrelated siblings of an invariant generic _ObjectBase[T] (different type args), so pyright thinks this can never match, even though pygit2.Commit.__mro__ shows Commit genuinely subclasses Object at runtime
        msg = f"expected Commit, got {type(commit).__name__}"
        raise TypeError(msg)
    repo.create_branch(branch_name, commit, True)
    return str(commit_id)


def diff_manifests(
    repo_path: str,
    branch_name: str,
    new_files: list[tuple[str, str]],
) -> str | None:
    repo = pygit2.Repository(_repo_path(repo_path))
    new_tree_id = _build_tree(repo, new_files)

    try:
        branch = repo.branches[branch_name]
        old_tree_id = branch.peel(pygit2.Commit).tree_id
    except KeyError:
        old_tree_id = repo.TreeBuilder().write()

    buf = io.StringIO()
    for patch in repo.diff(old_tree_id, new_tree_id):
        if patch is not None and patch.text is not None:
            buf.write(patch.text)

    result = buf.getvalue()
    return result or None


class ConcurrentDeployError(RuntimeError):
    """Raised when a branch's tip moved between prepare and finalize."""


@dataclass(frozen=True)
class PreparedCommits:
    """Commits built with `update_ref=None` -- not yet reachable from any
    branch. `finalize_branches` moves the refs once verification succeeds;
    a caller that never finalizes just leaves these as unreferenced loose
    objects, eventually GC'd."""

    deploy_oid: pygit2.Oid
    source_oid: pygit2.Oid | None
    expected_deploy_parent: pygit2.Oid | None
    expected_source_parent: pygit2.Oid | None


def _branch_tip(repo: pygit2.Repository, branch_name: str) -> pygit2.Oid | None:
    try:
        target = repo.branches[branch_name].target
    except KeyError:
        return None
    if isinstance(target, str):
        raise TypeError(f"{branch_name} is a symbolic reference, not a direct branch")
    return target


def prepare_deploy_and_source_commits(
    repo_path: str,
    deploy_branch: str,
    source_branch: str,
    manifest_files: list[tuple[str, str]],
    message: str,
) -> PreparedCommits:
    """Build the paired deploy+source commits in-memory (no ref moved).

    The source commit's only parent is the previous commit on
    `source_branch` (root commit if this is the first deploy to this
    branch). The deploy commit's parents are the previous commit on
    `deploy_branch` (if any) followed by the just-built source commit --
    every deploy commit points at the exact source state that produced it.

    The deploy commit's message also carries an explicit `ekn-source: <oid>`
    trailer (see `_with_source_trailer`) recording the same link --
    `rollback_branches` reads this trailer rather than assuming the source
    commit is always the deploy commit's *last* parent.
    """
    repo = pygit2.Repository(_repo_path(repo_path))
    author = repo.default_signature
    committer = repo.default_signature

    expected_source_parent = _branch_tip(repo, source_branch)
    source_parents = [expected_source_parent] if expected_source_parent is not None else []
    source_tree_id = snapshot_source_tree(repo_path)
    source_oid = repo.create_commit(
        None,
        author,
        committer,
        message,
        source_tree_id,
        source_parents,
    )

    expected_deploy_parent = _branch_tip(repo, deploy_branch)
    deploy_parents = [expected_deploy_parent, source_oid] if expected_deploy_parent is not None else [source_oid]
    deploy_tree_id = _build_tree(repo, manifest_files)
    deploy_oid = repo.create_commit(
        None,
        author,
        committer,
        _with_source_trailer(message, source_oid),
        deploy_tree_id,
        deploy_parents,
    )

    return PreparedCommits(
        deploy_oid=deploy_oid,
        source_oid=source_oid,
        expected_deploy_parent=expected_deploy_parent,
        expected_source_parent=expected_source_parent,
    )


def finalize_branches(
    repo_path: str,
    deploy_branch: str,
    source_branch: str | None,
    prepared: PreparedCommits,
) -> None:
    """Move `deploy_branch`/`source_branch` to their prepared commits --
    fast-forward only. Raises `ConcurrentDeployError` instead of silently
    overwriting if either branch's tip moved since `prepared` was built
    (Validate alone has measured ~104s against a real cluster, plenty of
    time for a concurrent deploy to race this one).

    SECRETS-SCANNING HOOK: last gate before these commits become reachable
    from a branch ref (and thus eligible to be pushed) -- a future secrets
    scanner could run here as a final check.
    """
    repo = pygit2.Repository(_repo_path(repo_path))

    if source_branch is not None and _branch_tip(repo, source_branch) != prepared.expected_source_parent:
        raise ConcurrentDeployError(
            f"{source_branch} moved since prepare -- concurrent deploy detected, retry",
        )
    if _branch_tip(repo, deploy_branch) != prepared.expected_deploy_parent:
        raise ConcurrentDeployError(
            f"{deploy_branch} moved since prepare -- concurrent deploy detected, retry",
        )

    pairs: list[tuple[str, pygit2.Oid]] = []
    if source_branch is not None and prepared.source_oid is not None:
        pairs.append((source_branch, prepared.source_oid))
    pairs.append((deploy_branch, prepared.deploy_oid))

    for branch_name, oid in pairs:
        commit = repo[oid]
        if not isinstance(commit, pygit2.Commit):  # pyright: ignore[reportUnnecessaryIsInstance] -- pygit2-stubs models Object/Commit as unrelated siblings of an invariant generic _ObjectBase[T] (different type args), so pyright thinks this can never match, even though pygit2.Commit.__mro__ shows Commit genuinely subclasses Object at runtime
            msg = f"expected Commit, got {type(commit).__name__}"
            raise TypeError(msg)
        if branch_name in repo.branches.local:
            repo.branches[branch_name].set_target(oid)
        else:
            repo.create_branch(branch_name, commit)


def rollback_branches(
    repo_path: str,
    deploy_branch: str,
    source_branch: str | None,
    *,
    steps_back: int = 1,
    to: str | None = None,
) -> PreparedCommits:
    """Forward-only rollback: replay an older (deploy-tree, source-tree)
    pair as a *new* commit pair at the current tips -- never resets or
    force-pushes anything. Without `to`, walks `deploy_branch`'s
    first-parent history back `steps_back` commits from its current tip.
    """
    repo = pygit2.Repository(_repo_path(repo_path))
    author = repo.default_signature
    committer = repo.default_signature

    deploy_tip = _branch_tip(repo, deploy_branch)
    if deploy_tip is None:
        raise ValueError(f"deploy branch {deploy_branch!r} does not exist")

    if to is not None:
        target_commit = repo.revparse_single(to).peel(pygit2.Commit)
    else:
        target_commit = repo[deploy_tip]
        if not isinstance(target_commit, pygit2.Commit):  # pyright: ignore[reportUnnecessaryIsInstance] -- pygit2-stubs models Object/Commit as unrelated siblings of an invariant generic _ObjectBase[T] (different type args), so pyright thinks this can never match, even though pygit2.Commit.__mro__ shows Commit genuinely subclasses Object at runtime
            msg = f"expected Commit, got {type(target_commit).__name__}"
            raise TypeError(msg)
        for _ in range(steps_back):
            if not target_commit.parents:
                raise ValueError(f"{deploy_branch} has no ancestor {steps_back} steps back")
            target_commit = target_commit.parents[0]

    message = f"rollback: replay {target_commit.short_id} onto {deploy_branch}"

    expected_deploy_parent = deploy_tip
    deploy_parents = [deploy_tip]

    source_oid: pygit2.Oid | None = None
    expected_source_parent: pygit2.Oid | None = None
    deploy_message = message
    if source_branch is not None:
        expected_source_parent = _branch_tip(repo, source_branch)
        source_parents = [expected_source_parent] if expected_source_parent is not None else []

        # Prefer target_commit's explicit ekn-source trailer over its
        # parent order -- see _with_source_trailer/prepare_deploy_and_source_
        # commits. Falls back to the old *last*-parent convention for deploy
        # commits made before this trailer existed, and finally to the
        # deploy commit's own tree only if it has no parents at all (e.g.
        # `--to` pointed at a commit made entirely outside this tooling).
        target_source_oid = _read_source_trailer(target_commit)
        if target_source_oid is not None:
            target_source_commit = repo[target_source_oid]
            if not isinstance(target_source_commit, pygit2.Commit):  # pyright: ignore[reportUnnecessaryIsInstance] -- pygit2-stubs models Object/Commit as unrelated siblings of an invariant generic _ObjectBase[T] (different type args), so pyright thinks this can never match, even though pygit2.Commit.__mro__ shows Commit genuinely subclasses Object at runtime
                msg = f"expected Commit, got {type(target_source_commit).__name__}"
                raise TypeError(msg)
            source_tree_id = target_source_commit.tree_id
        elif target_commit.parents:
            source_tree_id = target_commit.parents[-1].tree_id
        else:
            source_tree_id = target_commit.tree_id

        source_oid = repo.create_commit(
            None,
            author,
            committer,
            message,
            source_tree_id,
            source_parents,
        )
        deploy_parents.append(source_oid)
        deploy_message = _with_source_trailer(message, source_oid)

    deploy_oid = repo.create_commit(
        None,
        author,
        committer,
        deploy_message,
        target_commit.tree_id,
        deploy_parents,
    )

    return PreparedCommits(
        deploy_oid=deploy_oid,
        source_oid=source_oid,
        expected_deploy_parent=expected_deploy_parent,
        expected_source_parent=expected_source_parent,
    )


async def try_jj_status(repo_path: str | None = None) -> None:
    root = _repo_path(repo_path)
    if not await AsyncPath(root, ".jj").is_dir():
        return
    if shutil.which("jj") is None:
        return
    proc = await asyncio.create_subprocess_exec(
        "jj",
        "st",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=root,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode == 0 and stdout:
        sys.stdout.write(stdout.decode())
