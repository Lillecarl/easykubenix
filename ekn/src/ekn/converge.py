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
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Protocol

import anyio
import kr8s
import structlog

from .apply import KindNotServedError

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
    """Delete an object, so that `--allow-recreate` can apply it again.

    **It must not return until the object is gone.** A delete that returns
    when the API server has accepted it races the apply that follows: the
    create can reach the API server first and come back `409 AlreadyExists`.
    That classifies as RETRY and converges eventually, so the defect shows
    up as a slow barrier and a confusing log rather than as an error.

    Foreground propagation plus a poll until the GET is a 404 is what the
    contract asks for. `_recreate` names the race if it happens anyway,
    because a contract nothing checks is a comment.
    """

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


IMMUTABLE_PHRASES = (
    "immutable",
    "may not be changed",
    "may not change once set",
)
"""How the API server says "this cannot change", in the words it uses.

Three wordings, all measured against a live cluster rather than guessed:

    Job         spec.template     ": field is immutable"
    Service     spec.clusterIPs   ": may not change once set"

`may not change once set` is not `may not be changed`, and a table that
carries only the second misses every Service -- 57 of them in one render.
"""

FORBIDDEN_UPDATE_PHRASE = "updates to"
"""What makes a `FieldValueForbidden` an immutability error rather than a
schema one.

The StatefulSet case says `updates to statefulset spec for fields other than
'replicas', ... are forbidden`, and contains neither phrase above.

**Narrower than "every FieldValueForbidden".** That reason also covers a
field that may not be set at all, which is a configuration error: the answer
is to fix the manifest, not to delete the object. A recreate is a delete, so
the two mistakes are not symmetric -- missing an immutability error reports
it as terminal with the server's own message, and inventing one destroys
data. This errs at the safe end deliberately.
"""


def _causes(exc: kr8s.ServerError) -> list[tuple[str, str]]:
    """Every `(reason, message)` of a `Status` body, plus its own message.

    The API server puts the useful half of a 422 in `details.causes`, and
    `str(exc)` carries only the summary line. The reason matters as much as
    the message: `FieldValueForbidden` is how an immutability error arrives
    when the message says neither "immutable" nor "may not change".
    """
    response = exc.response
    causes: list[tuple[str, str]] = [("", str(exc))]
    if response is None:
        return causes
    try:
        body = response.json()
    except ValueError:
        return causes
    if not isinstance(body, dict):
        return causes
    details = body.get("details")
    if not isinstance(details, dict):
        return causes
    listed = details.get("causes")
    if not isinstance(listed, list):
        return causes
    causes.extend(
        (str(cause.get("reason", "")), str(cause.get("message", ""))) for cause in listed if isinstance(cause, dict)
    )
    return causes


def _is_immutable_error(exc: kr8s.ServerError) -> bool:
    """True for the 422 that means "this field cannot be changed".

    The case that forces this rung to exist is a completed Job:
    `spec.template` is immutable, so every apply after the first fails for
    ever and a converging run can never reach a clean queue.

    Three shapes, all captured from a live API server rather than guessed --
    a Job, a Service and a StatefulSet. The third carries no phrase at all
    and is recognised by its cause reason instead. See `IMMUTABLE_PHRASES`
    and `FORBIDDEN_UPDATE_PHRASE`, which says why that check is narrow.
    """
    for reason, message in _causes(exc):
        if any(phrase in message for phrase in IMMUTABLE_PHRASES):
            return True
        if reason == "FieldValueForbidden" and FORBIDDEN_UPDATE_PHRASE in message:
            return True
    return False


def classify(exc: BaseException) -> Disposition:
    """What a failed apply means, and therefore what to do about it.

    **`403` and a non-immutable `422` are terminal on purpose.** An RBAC
    refusal and a schema error do not become true by waiting, and retrying
    either for the whole settle window turns a clear failure into a slow
    one -- which is the failure mode this classification exists to prevent.

    **`KindNotServedError` is checked first, and it is not an HTTP error at all.**
    Discovery answers 200 and lacks the kind, so the apply never reaches a
    PATCH and no status code exists to classify. Left to the fall-through it
    lands on TERMINAL -- which would make the single most common retryable
    condition of a 188-CRD converge the one thing never retried, while its
    own message says a CRD "has to be applied first".
    """
    if isinstance(exc, KindNotServedError):
        return Disposition.RETRY
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


def _keeps_its_volumes(spec: Manifest) -> bool:
    """True when deleting this StatefulSet is known not to delete its volumes.

    A StatefulSet with `volumeClaimTemplates` owns PersistentVolumeClaims,
    and `persistentVolumeClaimRetentionPolicy.whenDeleted` decides what
    happens to them. The API default is `Retain`, so a recreate loses nothing
    today -- but it is a default, and a chart bump can set `Delete` without
    anyone reading the diff. Measured on one live cluster: 3 of 5
    StatefulSets declare volume claim templates and one states a policy.

    So this asks for the policy to be written down rather than inferred. An
    explicit `Retain` allows the recreate; anything else, including saying
    nothing, refuses it.
    """
    spec_value = spec.get("spec") or {}
    if not isinstance(spec_value, dict):
        return True
    templates = spec_value.get("volumeClaimTemplates")
    if not isinstance(templates, list) or not templates:
        return True
    policy = spec_value.get("persistentVolumeClaimRetentionPolicy") or {}
    if not isinstance(policy, dict):
        return False
    return policy.get("whenDeleted") == "Retain"


def _why_not_recreated(spec: Manifest) -> str:
    """The refusal, in terms of what the operator can do about it."""
    _, kind, _ = object_key(spec)
    if kind in RECREATE_DENY_KINDS:
        return f"immutable, and a {kind} is never recreated because the delete loses data"
    if kind == "StatefulSet" and not _keeps_its_volumes(spec):
        return (
            "immutable, and this StatefulSet has volumeClaimTemplates with no explicit "
            "spec.persistentVolumeClaimRetentionPolicy.whenDeleted = Retain, so a recreate "
            "may delete its PersistentVolumeClaims"
        )
    return "immutable, and this object carries " + RECREATE_OPT_OUT_ANNOTATION


def may_recreate(spec: Manifest) -> bool:
    """True when `--allow-recreate` is allowed to delete this object first."""
    _, kind, _ = object_key(spec)
    if kind in RECREATE_DENY_KINDS:
        return False
    if kind == "StatefulSet" and not _keeps_its_volumes(spec):
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
    clock: Callable[[], float] = anyio.current_time,
    sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
) -> list[Failure]:
    """Apply every object of one barrier, retrying what has not caught up.

    Returns the objects that never applied, with the last error each gave.
    An empty list means the barrier is clean.

    `clock` and `sleep` are arguments so that a test of the settle timer
    costs no wall time. Nothing else injects them. `anyio.current_time` and
    not `time.monotonic`: it is the event loop's own clock, the one anyio's
    timeouts measure against, so a future `fail_after` around this loop
    agrees with the settle timer rather than drifting from it.

    **The applies are serial, and a barrier is where concurrency belongs
    when it arrives.** Objects within one barrier are independent by
    construction -- that is what the barrier means -- so an
    `anyio.create_task_group` over this inner loop is the shape. Two pieces
    of state need moving first: `last_progress`, which several tasks would
    write, and `retry_next`, which they would append to.
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


async def converge_objects(  # noqa: PLR0913 -- every argument is an injected seam; see the Protocols above
    tiers: Sequence[Sequence[Manifest]],
    *,
    apply: ApplyOne,
    delete: DeleteOne | None = None,
    after_barrier: Callable[[Sequence[APIObject]], Awaitable[None]] | None = None,
    settle_seconds: float = DEFAULT_SETTLE_SECONDS,
    allow_recreate: bool = False,
    keep_going: bool = False,
    clock: Callable[[], float] = anyio.current_time,
    sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
) -> list[Failure]:
    """Converge every barrier in order, and report what never applied.

    `after_barrier` receives the objects that barrier applied, so a caller
    can wait for the CRDs among them to become Established. This module
    knows nothing about kr8s, and that hook is why it does not have to: the
    wait needs an `APIObject` and the policy here needs none.

    Without `keep_going`, a barrier that stops making progress ends the run.
    The barriers are an ordering, so carrying on past an unfinished one
    applies objects whose prerequisites are known to be missing -- which
    produces a second wave of failures that say nothing about the first.
    """
    failures: list[Failure] = []
    for index, tier in enumerate(tiers, start=1):
        _log.info("converging", barrier=f"{index}/{len(tiers)}", objects=len(tier))
        applied: list[APIObject] = []

        async def record(spec: Manifest, _applied: list[APIObject] = applied) -> APIObject:
            obj = await apply(spec)
            _applied.append(obj)
            return obj

        tier_failures = await converge_barrier(
            tier,
            apply=record,
            delete=delete,
            settle_seconds=settle_seconds,
            allow_recreate=allow_recreate,
            clock=clock,
            sleep=sleep,
        )
        failures.extend(tier_failures)
        if after_barrier is not None:
            await after_barrier(applied)
        if tier_failures and not keep_going:
            _log.error(
                "barrier did not finish; stopping",
                barrier=f"{index}/{len(tiers)}",
                failed=len(tier_failures),
                remaining_barriers=len(tiers) - index,
            )
            break
    return failures


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
        last_error[key] = f"{last_error[key]} ({_why_not_recreated(spec)})"
        return Disposition.TERMINAL
    try:
        await delete(spec)
    except Exception as exc:
        last_error[key] = f"{exc} (deleting it for a recreate)"
        return Disposition.TERMINAL
    try:
        await apply(spec)
    except Exception as exc:
        # A recreate that fails is reported, never retried: the delete has
        # already happened, so a second round would apply into a gap nobody
        # asked for.
        last_error[key] = _recreate_error(exc)
        return Disposition.TERMINAL
    return Disposition.RECREATE


def _recreate_error(exc: BaseException) -> str:
    """The message for an apply that failed after its delete succeeded.

    `409 AlreadyExists` here is the one failure that names its own cause: the
    delete returned before the object was gone, which `DeleteOne` says it
    must not. Left unnamed it reads as an ordinary conflict, retries, and
    reports a slow barrier rather than a broken delete.
    """
    if isinstance(exc, kr8s.ServerError) and _status_code(exc) == HTTPStatus.CONFLICT:
        return (
            f"{exc} (re-applying after a recreate. The delete returned before the object was gone, "
            f"so the create raced it -- see DeleteOne, which must poll until the object is absent.)"
        )
    return f"{exc} (re-applying after a recreate)"


__all__ = [
    "DEFAULT_SETTLE_SECONDS",
    "RECREATE_DENY_KINDS",
    "RECREATE_OPT_OUT_ANNOTATION",
    "Disposition",
    "Failure",
    "classify",
    "converge_barrier",
    "converge_objects",
    "may_recreate",
    "object_key",
    "settled",
]
