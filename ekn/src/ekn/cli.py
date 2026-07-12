from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from clypi import Command, arg
from nanopynix import NixError

from ekn.eval import evaluate_file, evaluate_flake, evaluate_flake_ekn
from ekn.git import commit_manifests, diff_manifests, flatten_manifests


def _parse_flake(flake_ref: str) -> tuple[str, str | None]:
    if "#" in flake_ref:
        uri, _, customer = flake_ref.partition("#")
    else:
        uri, customer = flake_ref, None
    if not uri or uri == ".":
        uri = str(Path.cwd())
    return uri, customer


async def _evaluate(
    file: Path | None,
    flake: str | None,
    attr_path: str | None,
) -> object:
    try:
        if flake is not None:
            uri, customer = _parse_flake(flake)
            if customer:
                return await evaluate_flake_ekn(uri, customer)
            return await evaluate_flake(uri, attr_path)
        if file is not None:
            return await evaluate_file(file, attr_path)
        raise ValueError("specify --file or --flake")
    except NixError as exc:
        print(exc.msg_without_ansi, file=sys.stderr)
        raise SystemExit(1)


def _dig(data: object, *keys: str) -> Any:
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _check_gitops(result: dict[str, Any]) -> str:
    enabled = _dig(result, "config", "gitops", "enable")
    if enabled is not True:
        print(
            "gitops is not enabled for this config (config.gitops.enable != true)",
            file=sys.stderr,
        )
        raise SystemExit(1)
    branch = _dig(result, "config", "gitops", "branch")
    if not isinstance(branch, str) or not branch:
        print(
            "config.gitops.branch is not set in the evaluated config",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return branch


def _get_manifests(result: dict[str, Any]) -> dict[str, Any]:
    manifests = _dig(result, "config", "kubernetes", "generatedByPath")
    if not isinstance(manifests, dict):
        print(
            "no kubernetes objects found (config.kubernetes.generatedByPath "
            "is empty or not a dict)",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return manifests


class Eval(Command):
    """Evaluate Nix and dump JSON."""
    file: Path | None = arg(None, flag="--file", short="-f", inherited=True)
    flake: str | None = arg(None, flag="--flake", inherited=True)
    attr_path: Optional[str] = arg(None, help="Dot-path into the evaluated file or flake output.")

    async def run(self) -> None:
        result = await _evaluate(self.file, self.flake, self.attr_path)
        json.dump(result, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")


class Diff(Command):
    """Diff rendered manifests against the GitOps branch."""
    file: Path | None = arg(None, flag="--file", short="-f", inherited=True)
    flake: str | None = arg(None, flag="--flake", inherited=True)
    attr_path: Optional[str] = arg(None, help="Dot-path into the evaluated file or flake output.")
    branch: Optional[str] = arg(None, flag="--branch", short="-b", help="Override the branch name from config.gitops.branch.")
    subdir: str = arg("./", flag="--subdir", short="-s", help="Subdirectory within the branch.")

    async def run(self) -> None:
        result = await _evaluate(self.file, self.flake, self.attr_path)
        if not isinstance(result, dict):
            print(f"expected a dict result, got {type(result).__name__}", file=sys.stderr)
            raise SystemExit(1)
        resolved_branch = self.branch or _check_gitops(result)
        manifests = _get_manifests(result)
        try:
            files = flatten_manifests(manifests, self.subdir)
        except TypeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            raise SystemExit(1)
        try:
            diff_output = diff_manifests(".", resolved_branch, files)
        except Exception as exc:
            print(f"diff failed: {exc}", file=sys.stderr)
            raise SystemExit(1)
        if diff_output is None:
            print("no differences")
            return
        print(diff_output)


class Commit(Command):
    """Render manifests and write them to the GitOps branch."""
    file: Path | None = arg(None, flag="--file", short="-f", inherited=True)
    flake: str | None = arg(None, flag="--flake", inherited=True)
    attr_path: Optional[str] = arg(None, help="Dot-path into the evaluated file or flake output.")
    branch: Optional[str] = arg(None, flag="--branch", short="-b", help="Override the branch name from config.gitops.branch.")
    message: Optional[str] = arg(None, flag="--message", short="-m", help="Commit message.")
    subdir: str = arg("./", flag="--subdir", short="-s", help="Subdirectory within the branch.")

    async def run(self) -> None:
        result = await _evaluate(self.file, self.flake, self.attr_path)
        if not isinstance(result, dict):
            print(f"expected a dict result, got {type(result).__name__}", file=sys.stderr)
            raise SystemExit(1)
        resolved_branch = self.branch or _check_gitops(result)
        manifests = _get_manifests(result)
        try:
            files = flatten_manifests(manifests, self.subdir)
        except TypeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            raise SystemExit(1)
        if not self.message:
            head_result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            head_sha = head_result.stdout.strip() if head_result.returncode == 0 else "unknown"
            self.message = f"ekn: render manifests from {self.attr_path or 'root'} @ {head_sha}"
        try:
            commit_id = commit_manifests(".", resolved_branch, files, self.message)
        except Exception as exc:
            print(f"commit failed: {exc}", file=sys.stderr)
            raise SystemExit(1)
        print(f"committed to {resolved_branch} @ {commit_id}")


class Ekn(Command):
    """easykubenix CLI — evaluate Nix and manage GitOps release branches."""
    subcommand: Eval | Diff | Commit | None = None
    file: Path | None = arg(None, flag="--file", short="-f", help="Nix file to evaluate.")
    flake: str | None = arg(None, flag="--flake", help="Flake reference (e.g. '.#myconfig'). Evaluates outputs.eknConfig.<system>.<attr>.")

    async def run(self) -> None:
        if self.file is None and self.flake is None:
            print("Usage: ekn [OPTIONS] COMMAND [ARGS]...")
            print()
            print("  easykubenix CLI — evaluate Nix and manage GitOps release branches.")
            print()
            print("Options:")
            print("  --file, -f    Nix file to evaluate.")
            print("  --flake       Flake reference (e.g. '.#myconfig').")
            print()
            print("Commands:")
            print("  eval          Evaluate Nix and dump JSON.")
            print("  diff          Diff rendered manifests against the GitOps branch.")
            print("  commit        Render manifests and write them to the GitOps branch.")
            raise SystemExit(0)
        result = await _evaluate(self.file, self.flake, None)
        json.dump(result, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")


def main() -> None:
    cli = Ekn.parse()
    cli.start()
