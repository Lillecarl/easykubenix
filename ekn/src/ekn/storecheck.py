"""Refuse an apply whose store paths no node can fetch.

A CSI-mounted store path can only be substituted: the volume names an
output path, so `allowSubstitutes = false` holds and a node cannot build it.
A path on no substituter is therefore a mount that fails on a node, minutes
after the apply and nowhere near it.

**Fails closed**, because the case this exists for is a combination nobody
predicted. On nixlab2 a newly-working garbage collector removed `cacheEnv`
from all four node stores while the cache push that would have replaced it
had been failing since 01:19. Either half alone was survivable. Together
they took out the only copy of the environment the store server itself
mounts, so the store server could not start, and the push that would have
fixed it goes *through* the store server. Nothing would have written a test
for that; a precondition catches it without having to.

**The check is here, and the substituters come from Nix.** It used to run
an external program and read its exit code, with the list baked into that
program. `ekn` already opens a store by URI through nanopynix -- see
`push_closure_to_store` -- so the work belongs here, and Nix already knows
which substituters it has, so nothing declares a second list that can
drift. `ekn.assertCached` is a boolean. See issue #37.

The three answers a substituter can give stay three, because nanopynix
keeps them apart:

    present       is_valid_path -> True
    absent        is_valid_path -> False   (InvalidPathError from the query)
    cannot ask    NixError

Measured 2026-09-17 against cache.nixos.org and an unroutable ssh-ng host.
One code path therefore covers `https`, `ssh-ng`, `s3` and `file`; a
scheme-specific fetcher could only ask an HTTP substituter (nixkube #41).
"""

from __future__ import annotations

import json
import re
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

import anyio
import structlog
from nanopynix import InvalidPathError, NixError
from nanopynix.rpc import Session

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
    from typing import Any

    #: `(uri, base_name)` -> `(references, None)` present, `(None, None)`
    #: absent, `(None, why)` when that substituter could not answer.
    type Probe = Callable[[str, str], Awaitable[tuple[set[str] | None, str | None]]]

_log = structlog.get_logger()

STORE_PATH = re.compile(r"/nix/store/([a-z0-9]{32}-[A-Za-z0-9._+?=-]*)")

DEFAULT_JOBS = 16


class StorePathsUnavailableError(RuntimeError):
    """The apply was refused because its store paths are not all fetchable."""


@dataclass(frozen=True)
class Verdict:
    """What every substituter together could say about a closure."""

    reachable: frozenset[str]
    missing: frozenset[str]
    #: Kept apart from `missing` deliberately. A substituter that will not
    #: answer is a broken check rather than a missing path, and the two need
    #: different fixes -- one is "push it", the other is "the cache is down
    #: or the hostname is wrong". Conflating them sends someone to push a
    #: path that is already there.
    unclear: Mapping[str, str]


def store_paths_in(objects: Sequence[dict[str, Any]]) -> set[str]:
    """Every store path these objects name, as `<digest>-<name>`.

    Read from the objects this apply sends, not from the rendered manifest
    derivation: a `--target` slice is not what `ekn.cachePackage` covers,
    and a seed rewrites an object's data before it goes out.
    """
    return {match.group(1) for match in STORE_PATH.finditer(json.dumps(objects))}


async def walk(
    roots: set[str],
    substituters: Sequence[str],
    probe: Probe,
    *,
    jobs: int = DEFAULT_JOBS,
) -> Verdict:
    """Ask every substituter about the whole closure of `roots`.

    Breadth-first, one level at a time, because a level is usually wide and
    a path's references are only known once it has been found somewhere.
    """
    seen: set[str] = set()
    missing: set[str] = set()
    unclear: dict[str, str] = {}
    frontier = set(roots)
    limiter = anyio.CapacityLimiter(jobs)

    while frontier:
        batch = sorted(frontier - seen)
        seen.update(batch)
        found: dict[str, set[str]] = {}
        problems: dict[str, list[str]] = {}

        async def one(base_name: str, found: dict[str, set[str]], problems: dict[str, list[str]]) -> None:
            for uri in substituters:
                async with limiter:
                    references, problem = await probe(uri, base_name)
                if problem is not None:
                    problems.setdefault(base_name, []).append(problem)
                    continue
                if references is not None:
                    found[base_name] = references
                    return

        async with anyio.create_task_group() as group:
            for base_name in batch:
                group.start_soon(one, base_name, found, problems)

        unresolved = set(batch) - set(found)
        # A path one substituter could not answer for, that another served,
        # is not unclear. Report a problem only where nothing resolved it.
        unclear.update({name: "; ".join(messages) for name, messages in problems.items() if name in unresolved})
        missing.update(unresolved - set(unclear))

        frontier = set()
        for references in found.values():
            frontier.update(references - seen)

    return Verdict(frozenset(seen), frozenset(missing), unclear)


@asynccontextmanager
async def nanopynix_probe() -> AsyncIterator[tuple[list[str], Probe]]:
    """Nix's own substituters, and a `Probe` backed by them.

    The list is read from Nix rather than declared in easykubenix, because
    a second list is a list that drifts.

    Each store is opened once, and not per level: a store per level
    reconnects an `ssh-ng` substituter every time.
    """
    async with Session() as session, AsyncExitStack() as stack:
        settings = await session.settings()
        substituters = settings.get("substituters", "").split()
        stores = {uri: await stack.enter_async_context(session.store(uri=uri)) for uri in substituters}

        async def probe(uri: str, base_name: str) -> tuple[set[str] | None, str | None]:
            store = stores[uri]
            path = f"/nix/store/{base_name}"
            try:
                if not await store.is_valid_path(path):
                    return None, None
                info = await store.query_path_info(path)
            except InvalidPathError:
                return None, None
            except NixError as exc:
                return None, f"{uri}: {exc}"
            return {reference.removeprefix("/nix/store/") for reference in info.references}, None

        yield substituters, probe


class NoSubstitutersError(RuntimeError):
    """Nix names no substituter, so the check would pass everything."""


async def assert_fetchable(
    objects: Sequence[dict[str, Any]],
    *,
    jobs: int = DEFAULT_JOBS,
    asking: tuple[Sequence[str], Probe] | None = None,
) -> None:
    """Refuse unless every store path `objects` names can be fetched.

    `asking` exists so the walk -- the part with the union and the
    three-way answer in it -- is testable without a store.
    """
    roots = store_paths_in(objects)
    if not roots:
        _log.info("no store paths to assert", objects=len(objects))
        return

    async with AsyncExitStack() as stack:
        if asking is None:
            asking = await stack.enter_async_context(nanopynix_probe())
        substituters, probe = asking

        if not substituters:
            # Fails closed. Asking nothing passes everything, which reads as
            # a healthy cluster and is the opposite of what this guard is for.
            raise NoSubstitutersError(
                "refusing to apply: ekn.assertCached is on and Nix names no substituter, "
                "so the check would pass every path without asking anything. Set "
                "nix.settings.substituters, or turn ekn.assertCached off."
            )

        _log.info(
            "asserting every store path is fetchable",
            paths=len(roots),
            substituters=list(substituters),
        )
        verdict = await walk(roots, substituters, probe, jobs=jobs)

    if verdict.unclear:
        detail = "\n".join(f"  {name}: {why}" for name, why in sorted(verdict.unclear.items())[:10])
        raise StorePathsUnavailableError(
            f"refusing to apply: the store-path check could not answer for "
            f"{len(verdict.unclear)} path(s).\n{detail}\n"
            f"This is a broken check rather than a missing path -- a substituter that will not "
            f"answer needs a different fix from one that is missing a path. Applying anyway would "
            f"be applying without the guard."
        )

    if verdict.missing:
        detail = "\n".join(f"  /nix/store/{name}" for name in sorted(verdict.missing))
        raise StorePathsUnavailableError(
            f"refusing to apply: {len(verdict.missing)} of {len(verdict.reachable)} paths in the "
            f"closure are on no substituter.\n{detail}\n"
            f"A node cannot build these -- a CSI volume names an output path, so it can only be "
            f"substituted. Push them first (see ekn.cacheTo), then apply."
        )

    _log.info("store paths are fetchable", paths=len(verdict.reachable))


__all__ = [
    "DEFAULT_JOBS",
    "NoSubstitutersError",
    "StorePathsUnavailableError",
    "Verdict",
    "assert_fetchable",
    "nanopynix_probe",
    "store_paths_in",
    "walk",
]
