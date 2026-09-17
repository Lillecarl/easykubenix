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

The check itself belongs to whoever knows the cluster's substituters --
`ekn.assertCached` names the program. This module only runs it and reads
what it says.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import structlog

from .validation import exec_capture

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Any

    from anyio import Path

_log = structlog.get_logger()

PATHS_MISSING = 1
"""Exit code: paths in the closure are on no substituter."""

CHECK_BROKEN = 3
"""Exit code: a substituter answered neither 200 nor 404.

Kept apart from `PATHS_MISSING` deliberately. A substituter that will not
answer is a broken check rather than a missing path, and the two need
different fixes -- one is "push it", the other is "the cache is down or the
hostname is wrong". Conflating them sends someone to push a path that is
already there.
"""


class StorePathsUnavailableError(RuntimeError):
    """The apply was refused because its store paths are not all fetchable."""


async def assert_fetchable(
    objects: Sequence[dict[str, Any]],
    *,
    checker: str,
    scratch: Path,
) -> None:
    """Refuse unless every store path `objects` names can be fetched.

    The objects are written out and handed to `checker` as a file, rather
    than the rendered manifest derivation, so what is asserted is exactly
    what this apply sends -- including a `--target` slice, which is not what
    `ekn.cachePackage` covers.
    """
    manifest = scratch / "apply-manifest.json"
    await manifest.write_text(json.dumps(objects))
    _log.info("asserting every store path is fetchable", checker=checker, objects=len(objects))
    code, out, err = await exec_capture(checker, str(manifest))
    if code == 0:
        _log.info("store paths are fetchable", detail=out.strip().splitlines()[-1] if out.strip() else "")
        return

    if code == PATHS_MISSING:
        msg = (
            f"refusing to apply: store paths this apply names are on no substituter.\n{err.strip()}\n"
            f"A node cannot build these -- a CSI volume names an output path, so it can only be "
            f"substituted. Push them first (see ekn.cacheTo), then apply."
        )
    elif code == CHECK_BROKEN:
        msg = (
            f"refusing to apply: the store-path check could not answer.\n{err.strip()}\n"
            f"This is a broken check rather than a missing path -- a substituter that will not "
            f"answer needs a different fix from one that is missing a path. Applying anyway would "
            f"be applying without the guard."
        )
    else:
        msg = f"refusing to apply: {checker} exited {code}.\n{err.strip()}"
    raise StorePathsUnavailableError(msg)


__all__ = ["CHECK_BROKEN", "PATHS_MISSING", "StorePathsUnavailableError", "assert_fetchable"]
