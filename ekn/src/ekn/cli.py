from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

import typer
from nanopynix import NixError

from ekn.eval import evaluate_file, evaluate_file_multi
from ekn.git import commit_manifests, diff_manifests, flatten_manifests

app = typer.Typer(
    name="ekn",
    help="easykubenix CLI — evaluate Nix and manage GitOps release branches.",
    no_args_is_help=True,
    add_completion=True,
)


def _resolve_branch(file: Path, branch: str | None) -> str:
    if branch is not None:
        return branch
    try:
        results = asyncio.run(evaluate_file_multi(file, "config.gitops.branch"))
    except NixError:
        pass
    else:
        val = results[0]
        if isinstance(val, str) and val:
            return val
    msg = (
        "no branch specified (--branch / -b) and config.gitops.branch "
        "is not set or not available in the evaluated file"
    )
    typer.echo(msg, err=True)
    raise typer.Exit(code=1)


def _evaluate_manifests(file: Path, attr_path: str | None) -> object:
    try:
        return asyncio.run(evaluate_file(file, attr_path))
    except NixError as exc:
        typer.echo(exc.msg_without_ansi, err=True)
        raise typer.Exit(code=1)


@app.callback(invoke_without_command=True)
def callback(
    ctx: typer.Context,
    file: Optional[Path] = typer.Option(
        None,
        "--file",
        "-f",
        help="Nix file to evaluate.",
        exists=True,
        dir_okay=False,
        readable=True,
    ),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    if file is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()
    result = _evaluate_manifests(file, None)
    json.dump(result, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


@app.command()
def eval(
    file: Path = typer.Option(
        ...,
        "--file",
        "-f",
        help="Nix file to evaluate.",
        exists=True,
        dir_okay=False,
        readable=True,
    ),
    attr_path: Optional[str] = typer.Argument(
        None,
        help="Dot-path into the evaluated file (e.g. 'a.b.c'). Omit to deep-force the whole file.",
    ),
) -> None:
    result = _evaluate_manifests(file, attr_path)
    json.dump(result, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


@app.command()
def diff(
    file: Path = typer.Option(
        ...,
        "--file",
        "-f",
        help="Nix file to evaluate.",
        exists=True,
        dir_okay=False,
        readable=True,
    ),
    attr_path: Optional[str] = typer.Argument(
        None,
        help="Dot-path into the evaluated file (e.g. 'a.b.c'). Omit to deep-force the whole file.",
    ),
    branch: Optional[str] = typer.Option(
        None,
        "--branch",
        "-b",
        help="GitOps branch name. Defaults to config.gitops.branch from the evaluated Nix.",
    ),
    subdir: str = typer.Option(
        "./",
        "--subdir",
        "-s",
        help="Subdirectory within the branch. Defaults to config.gitops.path or './'.",
    ),
) -> None:
    manifest_data = _evaluate_manifests(file, attr_path)
    branch = _resolve_branch(file, branch)
    try:
        files = flatten_manifests(manifest_data, subdir)
    except TypeError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)

    try:
        result = diff_manifests(".", branch, files)
    except Exception as exc:
        typer.echo(f"diff failed: {exc}", err=True)
        raise typer.Exit(code=1)

    if result is None:
        typer.echo("no differences")
        return
    typer.echo(result)


@app.command()
def commit(
    file: Path = typer.Option(
        ...,
        "--file",
        "-f",
        help="Nix file to evaluate.",
        exists=True,
        dir_okay=False,
        readable=True,
    ),
    attr_path: Optional[str] = typer.Argument(
        None,
        help="Dot-path into the evaluated file (e.g. 'a.b.c'). Omit to deep-force the whole file.",
    ),
    branch: Optional[str] = typer.Option(
        None,
        "--branch",
        "-b",
        help="GitOps branch name. Defaults to config.gitops.branch from the evaluated Nix.",
    ),
    message: Optional[str] = typer.Option(
        None,
        "--message",
        "-m",
        help="Commit message. Defaults to a reference to the current HEAD.",
    ),
    subdir: str = typer.Option(
        "./",
        "--subdir",
        "-s",
        help="Subdirectory within the branch. Defaults to config.gitops.path or './'.",
    ),
) -> None:
    manifest_data = _evaluate_manifests(file, attr_path)
    branch = _resolve_branch(file, branch)
    try:
        files = flatten_manifests(manifest_data, subdir)
    except TypeError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)

    if not message:
        head_result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        head_sha = head_result.stdout.strip() if head_result.returncode == 0 else "unknown"
        message = f"ekn: render manifests from {attr_path or 'root'} @ {head_sha}"

    try:
        commit_id = commit_manifests(".", branch, files, message)
    except Exception as exc:
        typer.echo(f"commit failed: {exc}", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"committed to {branch} @ {commit_id}")


if __name__ == "__main__":
    app()
