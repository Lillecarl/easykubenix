from __future__ import annotations

import asyncio
import io
import os
import shutil
from pathlib import PurePosixPath
from typing import Any

from anyio import Path


def _repo_path(path: str | None = None) -> str:
    return os.environ.get("EKN_REPO") or path or "."


def flatten_manifests(data: object, subdir: str = "./") -> list[tuple[str, str]]:
    import yaml

    if not isinstance(data, dict):
        raise TypeError(f"expected dict, got {type(data).__name__}")

    base = PurePosixPath(subdir)
    files: list[tuple[str, str]] = []

    for namespace, kinds in data.items():
        if not isinstance(kinds, dict):
            continue
        for kind, names in kinds.items():
            if not isinstance(names, dict):
                continue
            for name, manifest in names.items():
                if not isinstance(manifest, dict):
                    continue
                path = base / namespace / kind / f"{name}.yaml"
                yaml_content = yaml.dump(
                    manifest, default_flow_style=False, sort_keys=False
                )
                files.append((str(path), yaml_content))

    return files


def _build_tree(repo: Any, files: list[tuple[str, str]]) -> Any:
    import pygit2

    index = repo.index
    index.read()
    for rel_path, content in files:
        blob_id = repo.create_blob(content.encode("utf-8"))
        entry = pygit2.IndexEntry(rel_path, blob_id, pygit2.GIT_FILEMODE_BLOB)  # pyright: ignore — pygit2 GIT_FILEMODE_BLOB is an int but IndexEntry.mode accepts it at runtime
        index.add(entry)
    return index.write_tree()


def commit_manifests(
    repo_path: str,
    branch_name: str,
    files: list[tuple[str, str]],
    message: str,
) -> str:
    import pygit2

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
    if not isinstance(commit, pygit2.Commit):
        msg = f"expected Commit, got {type(commit).__name__}"
        raise TypeError(msg)
    repo.create_branch(branch_name, commit, True)
    return str(commit_id)


def diff_manifests(
    repo_path: str,
    branch_name: str,
    new_files: list[tuple[str, str]],
) -> str | None:
    import pygit2

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


async def try_jj_status(repo_path: str | None = None) -> None:
    root = _repo_path(repo_path)
    if not Path(root, ".jj").is_dir():
        return
    if shutil.which("jj") is None:
        return
    proc = await asyncio.create_subprocess_exec(
        "jj", "st",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=root,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode == 0 and stdout:
        print(stdout.decode(), end="")
