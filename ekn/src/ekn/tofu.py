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

import contextlib
import json
import os
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path as SyncPath, PurePosixPath
from typing import TYPE_CHECKING, NamedTuple

import anyio
import anyio.to_thread
import structlog
from anyio import Path

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    from ekn.eval import TofuUnit

_log = structlog.get_logger()

#: Where a unit's working directory goes, relative to the current directory.
#: Committed trees should ignore it -- it holds `.terraform/` and, for a local
#: backend, live state.
WORK_ROOT = ".ekn/tofu"


class TofuError(RuntimeError):
    """A `tofu` invocation exited non-zero."""


def _child_env() -> dict[str, str]:
    """The environment `tofu` runs with: this one, plus `KUBE_CONFIG_PATH`.

    The environment is inherited whole and deliberately. Provider credentials
    (`AWS_PROFILE`, `GOOGLE_APPLICATION_CREDENTIALS`, ...) and `TF_VAR_*`
    reach OpenTofu that way by design, and a filtered environment would break
    every one of them.

    **The one addition is two names for one file.** The `kubernetes` state
    backend reads `KUBECONFIG`; the `kubernetes` *provider* reads
    `KUBE_CONFIG_PATH`, because the rendered configuration leaves
    `config_path` unset so the file is chosen at run time rather than baked
    in. Setting only `KUBECONFIG` failed with a message naming neither:

        Error: no configuration has been provided, try setting KUBERNETES_MASTER

    So a `KUBECONFIG` naming a single file supplies the other. Issue #25.

    `KUBECONFIG` may hold several paths separated by `os.pathsep`, which the
    provider's single `config_path` cannot express -- OpenTofu has
    `config_paths` for that, and picking one of the list here would choose
    silently. A list is therefore left alone.
    """
    env = dict(os.environ)
    kubeconfig = env.get("KUBECONFIG", "")
    if not env.get("KUBE_CONFIG_PATH") and kubeconfig and os.pathsep not in kubeconfig:
        env["KUBE_CONFIG_PATH"] = kubeconfig
    return env


async def _run(unit: TofuUnit, workdir: Path, args: Sequence[str]) -> None:
    """Run `tofu` with *args* in *workdir*, or raise."""
    _log.info(f"{unit.name}: tofu {' '.join(args)}")
    # `stdout=None, stderr=None` inherits this process' own. `anyio.run_process`
    # pipes both by default, and a piped `tofu apply` shows nothing at all
    # until it has finished -- the progress lines are the whole point of
    # watching one run.
    completed = await anyio.run_process(
        [unit.tofu, *args],
        stdout=None,
        stderr=None,
        cwd=str(workdir),
        env=_child_env(),
        check=False,
    )
    if completed.returncode != 0:
        raise TofuError(f"{unit.name}: tofu {' '.join(args)} exited {completed.returncode}")


async def state_location(workdir: Path) -> str:
    """Where the state for the configuration in *workdir* actually lives, as
    one readable line.

    Reported on every run, and the reason is a footgun rather than a nicety.
    easykubenix has no default backend and asserts nothing about one, which is
    right -- a fixture unit legitimately wants local state, and a mandatory
    backend would force a fake block into every test to satisfy a rule about
    production. But a unit that simply forgot one gets a silent local state
    file, and nothing else would ever say so.

    Apply-time truth rather than an evaluation-time guess: this reads the
    rendered configuration, so it is right about what `tofu` will do.
    """
    config = json.loads(await (workdir / "config.tf.json").read_text())
    backend = config.get("terraform", {}).get("backend")
    if isinstance(backend, dict) and backend:
        kind = next(iter(backend))
        if kind != "local":
            return f"{kind} backend"
    return f"{workdir}/terraform.tfstate (local, not committed)"


async def prepare(unit: TofuUnit, root: Path | None = None) -> Path:
    """Lay out *unit*'s working directory and initialise it.

    Returns the directory. Safe to call again: the configuration is replaced
    from the store every time, so an edit to the Nix side reaches the next run
    and a hand-edit of the copy does not survive one.
    """
    workdir = (root or Path(WORK_ROOT)) / unit.name
    await workdir.mkdir(parents=True, exist_ok=True)

    source = Path(unit.config_file) / "config.tf.json"
    target = workdir / "config.tf.json"

    # An `init` that would change nothing is skipped. `ekn kubeapply
    # --kubeconfig-from-tofu` calls `prepare` only to read one output, and
    # re-initialising there means a provider download and a backend round trip
    # for a question already answered. Unchanged means both: the configuration
    # byte-identical to the store's, and a `.terraform/` already there.
    #
    # The directory only means this because a failed `init` no longer leaves
    # one -- see the `_run` call below. Issue #22.
    unchanged = await target.exists() and await target.read_bytes() == await source.read_bytes()
    if unchanged and await (workdir / ".terraform").exists():
        _log.info(f"{unit.name}: state at {await state_location(workdir)}")
        return workdir

    await anyio.to_thread.run_sync(shutil.copyfile, str(source), str(target), abandon_on_cancel=True)
    # `copyfile` copies contents and not permission bits, so the store's 0444
    # does not come along: the destination lands at 0666 before the umask, and
    # on a machine with a loose umask that is a world-writable file. Measured,
    # not assumed. (`copy2` would carry the 0444 instead, and then a second
    # `prepare` could not overwrite its own copy.)
    await (workdir / "config.tf.json").chmod(0o644)

    # See the module docstring: a lock written against an older nixpkgs pin
    # makes `init` fail, and it records nothing the wrapped `tofu` does not
    # already fix.
    lock = workdir / ".terraform.lock.hcl"
    if await lock.exists():
        await lock.unlink()

    # **A failed `init` must leave no `.terraform/`.** It creates the directory
    # before it can fail -- on a backend it cannot reach, for instance -- and
    # writes no lock file. The next run then read the directory as "already
    # initialised", skipped init, and `plan` died on "Inconsistent dependency
    # lock file: no version is selected", telling the user to run `tofu init`,
    # which `ekn` owns and would not run again. Nothing short of deleting the
    # directory by hand recovered. Issue #22.
    #
    # Deciding from `.terraform.lock.hcl` instead does not work: a lock left
    # by an older nixpkgs pin is exactly what the removal above exists for, and
    # reading it as "initialised" would skip the init that replaces it.
    try:
        await _run(unit, workdir, ["init", "-input=false"])
    except TofuError:
        await anyio.to_thread.run_sync(shutil.rmtree, str(workdir / ".terraform"), True, abandon_on_cancel=True)
        raise
    location = await state_location(workdir)
    _log.info(f"{unit.name}: state at {location}")

    # Adopting an existing tree is a file copy into this directory, and the
    # copy has to happen before the first run. Miss it and nothing objects:
    # `tofu` plans to create what already exists, which for an adopter is the
    # whole cluster a second time.
    #
    # Nothing downstream can catch that either -- an empty state this run just
    # created and an empty state that was always right look identical from
    # there. So it is said here, where it is still true and still cheap, and
    # only while there is no state to contradict it: after the first apply the
    # file exists and the line stops on its own.
    #
    # Local backends only. A remote one keeps no local file, so its absence
    # says nothing at all.
    if location.endswith("(local, not committed)") and not await (workdir / "terraform.tfstate").exists():
        _log.info(
            f"{unit.name}: no existing state, so this plans as if nothing exists yet. "
            f"Adopting an existing tree? Copy its terraform.tfstate into {workdir} first."
        )
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
    completed = await anyio.run_process(
        [unit.tofu, "output", "-raw", name],
        cwd=str(workdir),
        stderr=None,
        env=_child_env(),
        check=False,
    )
    if completed.returncode != 0:
        raise TofuError(f"{unit.name}: tofu output -raw {name} exited {completed.returncode}")
    return completed.stdout.decode()


#: What `ekn commit` takes from a unit's rendered directory. `config.tf.json`
#: is the configuration; `providers.json` is the version and store path Nix
#: resolved each provider to, which is the half `config.tf.json` cannot show --
#: it records `required_providers` constraints, not what they resolved to. See
#: `tofu.resolvedProviders` in easykubenix's tofu.nix for why that matters once
#: `.terraform.lock.hcl` is gone.
COMMITTED_FILES = ("config.tf.json", "providers.json")


def config_json(unit: TofuUnit) -> str:
    """*unit*'s rendered `config.tf.json`, read from the store.

    Read rather than re-serialised, so the committed bytes are the store's
    bytes -- `tofu.nix` pretty-prints through `jq --sort-keys` precisely so
    this file reads as a diff.
    """
    return (SyncPath(unit.config_file) / "config.tf.json").read_text()


@asynccontextmanager
async def kubeconfig_from_output(
    unit: TofuUnit,
    output_name: str,
    root: Path | None = None,
) -> AsyncGenerator[str]:
    """*unit*'s named output, written to a temporary kubeconfig, yielded as a
    path.

    This is the whole of the tofu-to-Kubernetes bridge. A `tf` unit builds the
    cluster and a Kubernetes unit has to reach it, and the two cannot be one
    apply -- so the credential travels here, at apply time, and never through
    Nix. Evaluation would otherwise depend on what a previous apply did.

    Mode 0600 before anything is written to it, and deleted on the way out. A
    kubeconfig is a credential, and a default-mode temporary file in a shared
    `/tmp` is readable by every other user on the machine.
    """
    content = await output(unit, output_name, root)
    handle, path = tempfile.mkstemp(prefix="ekn-kubeconfig-", suffix=".yaml")
    try:
        with os.fdopen(handle, "w") as stream:
            stream.write(content)
        _log.info(f"{unit.name}: kubeconfig from output {output_name}")
        yield path
    finally:
        with contextlib.suppress(FileNotFoundError):
            # Expected: the caller has no reason to remove it, but a crash
            # between mkstemp and here would leave nothing to unlink.
            await Path(path).unlink()


class OrphanScan(NamedTuple):
    """What `orphaned_state` looked at, and what it found.

    The count is not decoration. A scan that finds nothing and a scan that
    could not look are both an empty list, and a caller printing nothing in
    either case lets the reader convert silence into "no orphaned state" --
    a stronger claim than this can make, since it never sees a remote backend.
    Reporting what was checked keeps the negative result a scoped statement
    rather than an absence.
    """

    checked: int
    orphans: list[str]


async def orphaned_state(units: Sequence[TofuUnit], root: Path | None = None) -> OrphanScan:
    """Working directories under *root* with no unit in the evaluation, sorted
    by name, and how many were examined.

    The asymmetry this covers is the one real cost of having no ownership
    record of our own. On the Kubernetes side a unit deleted from the Nix
    configuration is safe: prune selects by label, so dropping the module
    deletes its objects. Delete a `tf` unit and the opposite happens -- nothing
    evaluates it any more, so `ekn tofu destroy --target <it>` can no longer
    even name it, and its real infrastructure is orphaned with no trace but a
    directory nobody references. A normal-looking refactor leaks cloud
    resources.

    Reads the one record that exists rather than inventing a second: state on
    disk, against the units the evaluation declares. Local working directories
    only -- a remote backend's keys are not ours to enumerate, which is why the
    ordering rule is documented as well as checked.
    """
    base = root or Path(WORK_ROOT)
    if not await base.exists():
        return OrphanScan(0, [])
    declared = {unit.name for unit in units}
    found = [entry.name async for entry in base.iterdir() if await entry.is_dir()]
    return OrphanScan(len(found), sorted(name for name in found if name not in declared))


def dependents(units: Sequence[TofuUnit], name: str) -> list[str]:
    """Every unit whose dependency closure holds *name*, in declaration order.

    The reverse of what `apply` walks, and the direction `destroy` needs.
    `apply` cannot run against a half-built dependency because it builds the
    closure first; `destroy` has no such protection of its own -- destroying a
    unit something else still needs either fails confusingly inside the
    provider or succeeds and leaves the dependent pointing at nothing.

    Each unit's `dependencies` is already transitive (easykubenix resolves the
    closure), so a plain membership test answers this without walking again.
    """
    return [unit.name for unit in units if name in unit.dependencies]


def file_groups(units: dict[str, TofuUnit]) -> list[tuple[str, str]]:
    """Each `tf` unit's rendered configuration as a `(path, content)` pair,
    for `ekn commit` to write to the deploy branch.

    Two files per unit, at that unit's own `path`, the same routing the
    Kubernetes side uses -- the configuration, and the provider versions Nix
    resolved (see `COMMITTED_FILES`). Two units sharing a path is an error rather than a
    merge: `config.tf.json` is one OpenTofu configuration with one state
    behind it, so the two would silently overwrite each other -- unlike two
    Kubernetes units sharing a path, whose objects are distinct files.

    Committing this is what makes an infrastructure change reviewable. It is
    also the reason `tofu.nix` pretty-prints the file: `builtins.toJSON` emits
    one line, and a one-line diff says only that something changed.
    """
    files: dict[str, str] = {}
    for name, unit in units.items():
        for filename in COMMITTED_FILES:
            path = str(PurePosixPath(unit.target.path) / filename)
            if path in files:
                raise TofuError(f'two tf units render to {path}; unit "{name}" is the second')
            files[path] = (SyncPath(unit.config_file) / filename).read_text()
    return list(files.items())


__all__ = [
    "COMMITTED_FILES",
    "WORK_ROOT",
    "OrphanScan",
    "TofuError",
    "config_json",
    "dependents",
    "file_groups",
    "kubeconfig_from_output",
    "orphaned_state",
    "output",
    "prepare",
    "run_chain",
    "state_location",
]
