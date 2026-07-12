from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import typer
from nanopynix import NixError

from ekn.eval import evaluate_file
from ekn.git import commit_manifests, diff_manifests, flatten_manifests

app = typer.Typer(
    name="ekn",
    help="easykubenix CLI — evaluate Nix and manage GitOps release branches.",
    no_args_is_help=True,
    add_completion=True,
)


def _evaluate(file: Path, attr_path: str | None) -> object:
    try:
        return asyncio.run(evaluate_file(file, attr_path))
    except NixError as exc:
        typer.echo(exc.msg_without_ansi, err=True)
        raise typer.Exit(code=1)


def _dig(data: object, *keys: str) -> Any:
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _check_gitops(result: dict[str, Any]) -> str:
    enabled = _dig(result, "config", "gitops", "enable")
    if enabled is not True:
        typer.echo(
            "gitops is not enabled for this config (config.gitops.enable != true)",
            err=True,
        )
        raise typer.Exit(code=1)
    branch = _dig(result, "config", "gitops", "branch")
    if not isinstance(branch, str) or not branch:
        typer.echo(
            "config.gitops.branch is not set in the evaluated config",
            err=True,
        )
        raise typer.Exit(code=1)
    return branch


def _get_manifests(result: dict[str, Any]) -> dict[str, Any]:
    manifests = _dig(result, "config", "kubernetes", "generatedByPath")
    if not isinstance(manifests, dict):
        typer.echo(
            "no kubernetes objects found (config.kubernetes.generatedByPath "
            "is empty or not a dict)",
            err=True,
        )
        raise typer.Exit(code=1)
    return manifests


@app.callback(invoke_without_command=True)
def callback(
    ctx: typer.Context,
    file: Optional[Path] = typer.Option(
        None,
        "--file",
        "-f",
        help="Nix file or easykubenix root to evaluate.",
        exists=True,
        readable=True,
    ),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    if file is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()
    result = _evaluate(file, None)
    json.dump(result, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


@app.command()
def eval(
    file: Path = typer.Option(
        ...,
        "--file",
        "-f",
        help="Nix file or easykubenix root to evaluate.",
        exists=True,
        readable=True,
    ),
    attr_path: Optional[str] = typer.Argument(
        None,
        help="Dot-path into the evaluated file (e.g. 'a.b.c'). Omit to deep-force the whole file.",
    ),
) -> None:
    result = _evaluate(file, attr_path)
    json.dump(result, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


@app.command()
def diff(
    file: Path = typer.Option(
        ...,
        "--file",
        "-f",
        help="Nix file or easykubenix root to evaluate.",
        exists=True,
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
        help="Override the branch name from config.gitops.branch.",
    ),
    subdir: str = typer.Option(
        "./",
        "--subdir",
        "-s",
        help="Subdirectory within the branch.",
    ),
) -> None:
    result = _evaluate(file, attr_path)
    if not isinstance(result, dict):
        typer.echo(f"expected a dict result at '{attr_path}', got {type(result).__name__}", err=True)
        raise typer.Exit(code=1)
    resolved_branch = branch or _check_gitops(result)
    manifests = _get_manifests(result)
    try:
        files = flatten_manifests(manifests, subdir)
    except TypeError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    try:
        diff_output = diff_manifests(".", resolved_branch, files)
    except Exception as exc:
        typer.echo(f"diff failed: {exc}", err=True)
        raise typer.Exit(code=1)
    if diff_output is None:
        typer.echo("no differences")
        return
    typer.echo(diff_output)


@app.command()
def commit(
    file: Path = typer.Option(
        ...,
        "--file",
        "-f",
        help="Nix file or easykubenix root to evaluate.",
        exists=True,
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
        help="Override the branch name from config.gitops.branch.",
    ),
    message: Optional[str] = typer.Option(
        None,
        "--message",
        "-m",
        help="Commit message.",
    ),
    subdir: str = typer.Option(
        "./",
        "--subdir",
        "-s",
        help="Subdirectory within the branch.",
    ),
) -> None:
    result = _evaluate(file, attr_path)
    if not isinstance(result, dict):
        typer.echo(f"expected a dict result at '{attr_path}', got {type(result).__name__}", err=True)
        raise typer.Exit(code=1)
    resolved_branch = branch or _check_gitops(result)
    manifests = _get_manifests(result)
    try:
        files = flatten_manifests(manifests, subdir)
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
        commit_id = commit_manifests(".", resolved_branch, files, message)
    except Exception as exc:
        typer.echo(f"commit failed: {exc}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"committed to {resolved_branch} @ {commit_id}")


if __name__ == "__main__":
    app()
