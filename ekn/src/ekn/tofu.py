# SPDX-License-Identifier: MIT
"""Run OpenTofu over a `class = "tf"` deployment unit.

A `tf` unit is a whole separate easykubenix instance whose modules declare
OpenTofu configuration instead of Kubernetes objects (see easykubenix's
`tofu.nix`). Nix renders it to `config.tf.json` and pins the providers into a
wrapped `tofu`; this module is what runs that binary over that file.

Three things here are not obvious, and each cost something to learn.

**The store path is read-only, so nothing can run in it.** `tofu init` writes
`.terraform/`, `tofu apply` writes a local state file, and both want the
directory holding the configuration. So each unit gets a working directory
outside the store and `config.tf.json` is copied in.

**The lock file is Nix's enemy.** `tofu init` writes `.terraform.lock.hcl`
pinning each provider's exact version, and a later evaluation that moves the
nixpkgs pin supplies a different one. `init` then fails against a lock nobody
wrote by hand. The lock says nothing here that the store path does not say
better, so it is removed before every `init`.

**A unit's dependencies are separate runs, not one plan.** Kubernetes units
merge their objects into a single apply ordered by `ekn.resourcePriority`;
OpenTofu has no such ordering and brings its own state per unit. So the
closure is walked deepest first and each unit gets its own `init` and its own
command.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path as SyncPath
from typing import TYPE_CHECKING

import structlog
from anyio import Path

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ekn.eval import TofuUnit

_log = structlog.get_logger()

#: Where a unit's working directory goes, relative to the current directory.
#: Committed trees should ignore it -- it holds `.terraform/` and, for a local
#: backend, live state.
WORK_ROOT = ".ekn/tofu"


class TofuError(RuntimeError):
    """A `tofu` invocation exited non-zero."""


async def _run(unit: TofuUnit, workdir: Path, args: Sequence[str]) -> None:
    """Run `tofu` with *args* in *workdir*, or raise.

    The environment is inherited whole and deliberately. Provider credentials
    (`AWS_PROFILE`, `GOOGLE_APPLICATION_CREDENTIALS`, ...) and `TF_VAR_*`
    reach OpenTofu that way by design, and a filtered environment would break
    every one of them.
    """
    _log.info(f"{unit.name}: tofu {' '.join(args)}")
    process = await asyncio.create_subprocess_exec(unit.tofu, *args, cwd=str(workdir))
    code = await process.wait()
    if code != 0:
        raise TofuError(f"{unit.name}: tofu {' '.join(args)} exited {code}")


async def prepare(unit: TofuUnit, root: Path | None = None) -> Path:
    """Lay out *unit*'s working directory and initialise it.

    Returns the directory. Safe to call again: the configuration is replaced
    from the store every time, so an edit to the Nix side reaches the next run
    and a hand-edit of the copy does not survive one.
    """
    workdir = (root or Path(WORK_ROOT)) / unit.name
    await workdir.mkdir(parents=True, exist_ok=True)

    source = Path(unit.config_file) / "config.tf.json"
    await asyncio.to_thread(shutil.copyfile, str(source), str(workdir / "config.tf.json"))
    # Read-only in the store, and a re-copy onto a read-only file fails.
    await (workdir / "config.tf.json").chmod(0o644)

    # See the module docstring: a lock written against an older nixpkgs pin
    # makes `init` fail, and it records nothing the wrapped `tofu` does not
    # already fix.
    lock = workdir / ".terraform.lock.hcl"
    if await lock.exists():
        await lock.unlink()

    await _run(unit, workdir, ["init", "-input=false"])
    return workdir


async def run_chain(
    units: Sequence[TofuUnit],
    args: Sequence[str],
    root: Path | None = None,
) -> None:
    """Run `tofu *args*` over each of *units*, in the order given.

    *units* is a dependency closure, deepest first, with the named unit last.
    A failure stops the chain: the units after it are the ones that needed
    this one, so running them against a half-built dependency is worse than
    not running them.
    """
    for unit in units:
        workdir = await prepare(unit, root)
        await _run(unit, workdir, args)


async def output(unit: TofuUnit, name: str, root: Path | None = None) -> str:
    """One of *unit*'s OpenTofu outputs, as a raw string.

    `-raw` rather than `-json`, because the caller wants the value itself --
    a kubeconfig, a hostname -- and `-json` would wrap it in quotes and
    escapes. It is an error in OpenTofu for a non-primitive output.

    This is the *only* direction a tofu output travels. Nothing here reaches
    Nix: evaluation would then depend on what a previous apply did, which is
    the property the rest of easykubenix is built on. See design/opentofu.md.
    """
    workdir = await prepare(unit, root)
    _log.info(f"{unit.name}: tofu output -raw {name}")
    process = await asyncio.create_subprocess_exec(
        unit.tofu,
        "output",
        "-raw",
        name,
        cwd=str(workdir),
        stdout=asyncio.subprocess.PIPE,
    )
    stdout, _ = await process.communicate()
    if process.returncode != 0:
        raise TofuError(f"{unit.name}: tofu output -raw {name} exited {process.returncode}")
    return stdout.decode()


def config_json(unit: TofuUnit) -> str:
    """*unit*'s rendered `config.tf.json`, read from the store.

    What `ekn commit` writes to the branch. Read rather than re-serialised, so
    the committed bytes are the store's bytes -- `tofu.nix` pretty-prints
    through `jq --sort-keys` precisely so this file reads as a diff.
    """
    return (SyncPath(unit.config_file) / "config.tf.json").read_text()


__all__ = [
    "WORK_ROOT",
    "TofuError",
    "config_json",
    "output",
    "prepare",
    "run_chain",
]
