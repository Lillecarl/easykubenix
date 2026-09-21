# SPDX-License-Identifier: MIT
"""Refuse to act against a cluster the configuration does not name.

**The ambient kubeconfig is not a neutral default.** With nothing declared
and no `--kubeconfig-from-tofu`, `ekn` uses whatever the operator's shell
points at -- which is usually a valid, reachable, credentialed cluster that
happens to be the wrong one. Nothing fails at the time: the apply succeeds
object by object until something unrelated collides, and by then a cluster
nobody meant to touch is carrying an environment's worth of objects.

The identity is the `uid` of the `kube-system` Namespace. See
`fastcache.IDENTITY_NAMESPACE` for why that one, and `ekn.clusterUid` for
what a configuration declares.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from ekn.fastcache import ClusterIdUnreadableError, read_cluster_id

if TYPE_CHECKING:
    from kr8s.asyncio import Api

_log = structlog.get_logger()

#: The flag that runs anyway with nothing declared. Long, and honest about
#: what passing it asserts: the friction belongs in a reviewed file and in a
#: postmortem, not in the terminal.
OVERRIDE_FLAG = "--i-dont-know-which-cluster-this-is"


def _adopt(uid: str) -> str:
    return f'Add this to the configuration:\n\n    ekn.clusterUid = "{uid}";\n'


async def require(api: Api, declared: str | None, *, environment: str, override: bool = False) -> str | None:
    """Check this cluster against *declared*, or raise `SystemExit`.

    Returns the uid it read, so a caller that needs it again -- `open_cache`
    does -- asks the API server once.

    *environment* is `ekn.environment`, and it is on every message because a
    bare uid says only that the fence fired. What the operator usually got
    wrong is which of two configurations they were pointed at, and the
    environment is the name they know that by. Reported by the operator this
    exists for: "a bare uid mismatch tells me the fence fired; the
    environment name tells me which of two configurations I was pointed at,
    which is the thing I actually got wrong."

    Four outcomes, and which one you are in decides what the message says:

    - declared and equal: return it, silently.
    - declared and different: refuse. **`override` does not help here**, and
      the message says so. A mismatch means the configuration declares an
      answer and the cluster disagrees; the fix is editing that line, which
      is a change someone reviews.
    - not declared, no override: refuse, and print the line to paste. Adopting
      is then one copy-paste and less work than typing the flag again, which
      is what retires the flag.
    - not declared, with override: warn and carry on, naming both the uid and
      the option.

    A cluster that cannot be named at all is the fifth case and refuses too.
    `cluster_id` treats that as benign because it only turns a cache off;
    here it would be a way past the fence, so it is not.
    """
    try:
        actual = await read_cluster_id(api)
    except ClusterIdUnreadableError as exc:
        raise SystemExit(
            f"refusing to act: this command cannot tell which cluster it is talking to, "
            f"so it cannot check environment {environment!r} against it.\n"
            f"{exc}\n"
            f"Nothing about ekn.clusterUid can be checked without it, and an unreadable "
            f"identity must not be a way past the check."
        ) from exc

    if declared is None:
        if override:
            _log.warning(
                "running against an unnamed cluster",
                environment=environment,
                cluster_uid=actual,
                flag=OVERRIDE_FLAG,
                option="ekn.clusterUid",
            )
            return actual
        raise SystemExit(
            f"refusing to act: environment {environment!r} does not say which cluster it is "
            f"for, and the kubeconfig points at {actual}.\n\n"
            f"{_adopt(actual)}\n"
            f"Or pass {OVERRIDE_FLAG} to run against it anyway."
        )

    if declared != actual:
        raise SystemExit(
            f"refusing to act: environment {environment!r} is for cluster {declared}, "
            f"and the kubeconfig points at {actual}.\n\n"
            f"Either point at the right cluster, or change ekn.clusterUid if this one "
            f"replaced it -- a rebuilt cluster has a new uid.\n"
            f"{OVERRIDE_FLAG} does not apply to a mismatch: the configuration already "
            f"names a cluster, so this is a disagreement and not a missing answer."
        )

    return actual


__all__ = ["OVERRIDE_FLAG", "require"]
