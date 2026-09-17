"""Applying a whole generated set without stopping at the first failure.

`apply_and_prune` walks barriers and lets the first `apply_one` that raises
abort the run. For a narrow `--target` slice that is right: the failure is
almost always the whole story. For a whole-cluster apply it is the worst
outcome available, because the run stops half applied and says nothing at
all about the half it never reached.

This converges instead. Within a barrier it applies every object, collects
the failures rather than raising, and retries the ones the cluster has
merely not caught up with. It gives up on a barrier when nothing has
succeeded for a while, and it reports every object that never applied.

The classification is the whole design. "Retry with backoff" without one
spends the settle window re-sending a schema error, and reports a clear
failure slowly instead of quickly. Issue Lillecarl/easykubenix#28.
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Protocol

import anyio
import kr8s
import structlog

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from kr8s.asyncio.objects import APIObject

    from .apply import Manifest

_log = structlog.get_logger()

DEFAULT_SETTLE_SECONDS = 60.0
"""How long a barrier may make no progress before this gives up on it."""

INITIAL_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 15.0

RECREATE_DENY_KINDS = frozenset(
    {
        "PersistentVolumeClaim",
        "Secret",
        "Namespace",
        "CustomResourceDefinition",
    },
)
"""Kinds that `--allow-recreate` never deletes, whatever the API server says.

Each one loses data that no re-apply brings back: a PVC's volume, a Secret
whose value was seeded once, everything in a Namespace, and every custom
object a CRD serves. An immutable-field error on one of these is a fault to
report, not a delete to perform.
"""

RECREATE_OPT_OUT_ANNOTATION = "ekn.dev/no-recreate"
"""Set on an object to keep `--allow-recreate` away from it by name."""


class Disposition(enum.Enum):
    """What to do with an object the API server refused."""

    RETRY = "retry"
    RECREATE = "recreate"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class Failure:
    """One object that did not apply, and the last reason it gave."""

    key: tuple[str, str, str]
    disposition: Disposition
    error: str


class ApplyOne(Protocol):
    """The "put this object on the cluster" step, as this loop needs it.

    A callable rather than an `Api`, because every decision here is made from
    what the call *raises*. A test scripts the exceptions directly and needs
    no API server, fake or otherwise, to reach the branch it is about.
    """

    def __call__(self, spec: Manifest) -> Awaitable[APIObject]: ...


class DeleteOne(Protocol):
    """Delete an object, so that `--allow-recreate` can apply it again."""

    def __call__(self, spec: Manifest) -> Awaitable[None]: ...


def object_key(spec: Manifest) -> tuple[str, str, str]:
    """The `(namespace, kind, name)` identity of a rendered manifest."""
    metadata_value = spec.get("metadata") or {}
    metadata = metadata_value if isinstance(metadata_value, dict) else {}
    namespace = metadata.get("namespace")
    name = metadata.get("name")
    kind = spec.get("kind")
    return (
        str(namespace) if isinstance(namespace, str) else "none",
        str(kind) if isinstance(kind, str) else "?",
        str(name) if isinstance(name, str) else "?",
    )


_RETRY_STATUSES = frozenset(
    {
        HTTPStatus.NOT_FOUND,
        HTTPStatus.CONFLICT,
        HTTPStatus.TOO_MANY_REQUESTS,
        HTTPStatus.INTERNAL_SERVER_ERROR,
        HTTPStatus.BAD_GATEWAY,
        HTTPStatus.SERVICE_UNAVAILABLE,
        HTTPStatus.GATEWAY_TIMEOUT,
    },
)
"""Statuses that mean "the cluster has not caught up yet", so try again.

`404` covers the two orderings this apply cannot fix by sorting: a custom
kind whose CRD is in an earlier barrier and not yet served, and an object
whose Namespace is in this apply too. Barriers order by kind, not by
dependency, so both are ordinary rather than exceptional.

`409` is here rather than behind a `force=True` retry. `ssa_apply` already
forces every apply (`apply.py:153`, unchanged since 2026-08-10), so a
server-side apply never conflicts on field ownership -- it takes the field.
A `409` that reaches this point therefore means something else, most often a
Namespace that is terminating, and re-sending is the only useful answer to
that.

`500` is where a failing admission webhook lands: the API server reports the
webhook's refusal as its own error, and a webhook whose backing Deployment
is still starting is exactly the "not caught up" case.
"""


def _status_code(exc: kr8s.ServerError) -> int | None:
    response = exc.response
    return None if response is None else response.status_code


def _causes(exc: kr8s.ServerError) -> list[str]:
    """Every `causes[].message` of a `Status` body, plus its own message.

    The API server puts the useful half of a 422 in `details.causes`, and
    `str(exc)` carries only the summary line.
    """
    response = exc.response
    messages = [str(exc)]
    if response is None:
        return messages
    try:
        body = response.json()
    except ValueError:
        return messages
    if not isinstance(body, dict):
        return messages
    details = body.get("details")
    if not isinstance(details, dict):
        return messages
    causes = details.get("causes")
    if not isinstance(causes, list):
        return messages
    messages.extend(str(cause.get("message", "")) for cause in causes if isinstance(cause, dict))
    return messages


def _is_immutable_error(exc: kr8s.ServerError) -> bool:
    """True for the 422 that means "this field cannot be changed".

    The case that forces this rung to exist is a completed Job:
    `spec.template` is immutable, so every apply after the first fails
    forever and a converging run can never reach a clean queue. The API
    server words it `field is immutable`; several controllers word their own
    version `may not be changed`.
    """
    return any("immutable" in message or "may not be changed" in message for message in _causes(exc))


def classify(exc: BaseException) -> Disposition:
    """What a failed apply means, and therefore what to do about it.

    **`403` and a non-immutable `422` are terminal on purpose.** An RBAC
    refusal and a schema error do not become true by waiting, and retrying
    either for the whole settle window turns a clear failure into a slow
    one -- which is the failure mode this classification exists to prevent.
    """
    if isinstance(exc, kr8s.ServerError):
        status = _status_code(exc)
        if status == HTTPStatus.UNPROCESSABLE_ENTITY:
            return Disposition.RECREATE if _is_immutable_error(exc) else Disposition.TERMINAL
        if status in _RETRY_STATUSES:
            return Disposition.RETRY
        if status is None:
            # kr8s builds a ServerError with no response for a transport
            # failure, which is a webhook or an API server that is not
            # answering rather than one that refused.
            return Disposition.RETRY
        return Disposition.TERMINAL
    if isinstance(exc, TimeoutError | ConnectionError | OSError):
        return Disposition.RETRY
    return Disposition.TERMINAL


def may_recreate(spec: Manifest) -> bool:
    """True when `--allow-recreate` is allowed to delete this object first."""
    _, kind, _ = object_key(spec)
    if kind in RECREATE_DENY_KINDS:
        return False
    metadata_value = spec.get("metadata") or {}
    metadata = metadata_value if isinstance(metadata_value, dict) else {}
    annotations_value = metadata.get("annotations") or {}
    annotations = annotations_value if isinstance(annotations_value, dict) else {}
    return RECREATE_OPT_OUT_ANNOTATION not in annotations


def settled(last_progress: float, now: float, settle_seconds: float) -> bool:
    """True when the barrier has stopped making progress.

    **"No progress for N seconds", and not "N seconds after the clean queue
    emptied."** A barrier where an operator is slowly coming up is making
    progress, and a deadline measured from the moment the queue emptied cuts
    it off while it is still working. `last_progress` moves on every
    successful apply.

    One function, because which of the two readings is right is a product
    decision that is still open on issue #28. Changing it changes here.
    """
    return now - last_progress >= settle_seconds


def _backoff(attempt: int) -> float:
    return min(INITIAL_BACKOFF_SECONDS * (2**attempt), MAX_BACKOFF_SECONDS)


async def converge_barrier(  # noqa: PLR0913 -- every argument is an injected seam; see the Protocols above
    specs: Sequence[Manifest],
    *,
    apply: ApplyOne,
    delete: DeleteOne | None = None,
    settle_seconds: float = DEFAULT_SETTLE_SECONDS,
    allow_recreate: bool = False,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
) -> list[Failure]:
    """Apply every object of one barrier, retrying what has not caught up.

    Returns the objects that never applied, with the last error each gave.
    An empty list means the barrier is clean.

    `clock` and `sleep` are arguments so that a test of the settle timer
    costs no wall time. Nothing else injects them.
    """
    pending = list(specs)
    terminal: dict[tuple[str, str, str], Failure] = {}
    last_error: dict[tuple[str, str, str], str] = {}
    last_progress = clock()
    attempt = 0

    while pending:
        retry_next: list[Manifest] = []
        for spec in pending:
            key = object_key(spec)
            try:
                await apply(spec)
            except Exception as exc:
                # Broad on purpose: `classify` is the whole policy, and it
                # answers TERMINAL for anything it does not know.
                disposition = classify(exc)
                last_error[key] = str(exc)
                if disposition is Disposition.RECREATE:
                    disposition = await _recreate(
                        spec,
                        key=key,
                        apply=apply,
                        delete=delete,
                        allow_recreate=allow_recreate,
                        last_error=last_error,
                    )
                if disposition is Disposition.TERMINAL:
                    terminal[key] = Failure(key, Disposition.TERMINAL, last_error[key])
                    continue
                if disposition is Disposition.RETRY:
                    retry_next.append(spec)
                    continue
                # RECREATE that succeeded falls through as progress.
                last_progress = clock()
                _log.debug("recreated", namespace=key[0], kind=key[1], name=key[2])
            else:
                last_progress = clock()
                _log.debug("applied", namespace=key[0], kind=key[1], name=key[2])

        pending = retry_next
        if not pending:
            break
        if settled(last_progress, clock(), settle_seconds):
            _log.warning(
                "barrier stopped making progress",
                remaining=len(pending),
                settle_seconds=settle_seconds,
            )
            break
        await sleep(_backoff(attempt))
        attempt += 1

    stuck = [Failure(object_key(spec), Disposition.RETRY, last_error.get(object_key(spec), "")) for spec in pending]
    return [*terminal.values(), *stuck]


async def _recreate(  # noqa: PLR0913 -- one caller, and every argument is state that caller holds
    spec: Manifest,
    *,
    key: tuple[str, str, str],
    apply: ApplyOne,
    delete: DeleteOne | None,
    allow_recreate: bool,
    last_error: dict[tuple[str, str, str], str],
) -> Disposition:
    """Delete an object with an immutable field, then apply it again.

    Refusing is the default. A recreate is a delete, and a delete of the
    wrong object is the one failure in this module that no later run can
    undo -- so it happens only when the operator asked for it, only for a
    kind that `RECREATE_DENY_KINDS` allows, and only when the object does
    not carry the opt-out annotation.
    """
    if not allow_recreate or delete is None:
        last_error[key] = f"{last_error[key]} (immutable; pass --allow-recreate to delete and re-apply)"
        return Disposition.TERMINAL
    if not may_recreate(spec):
        last_error[key] = f"{last_error[key]} (immutable, and this kind is never recreated)"
        return Disposition.TERMINAL
    try:
        await delete(spec)
        await apply(spec)
    except Exception as exc:
        # A recreate that fails is reported, never retried: the delete may
        # already have happened, so a second round would apply into a gap
        # nobody asked for.
        last_error[key] = str(exc)
        return Disposition.TERMINAL
    return Disposition.RECREATE


__all__ = [
    "DEFAULT_SETTLE_SECONDS",
    "RECREATE_DENY_KINDS",
    "RECREATE_OPT_OUT_ANNOTATION",
    "Disposition",
    "Failure",
    "classify",
    "converge_barrier",
    "may_recreate",
    "object_key",
    "settled",
]
