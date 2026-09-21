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
from collections import Counter
from dataclasses import dataclass, field, replace
from http import HTTPStatus
from typing import TYPE_CHECKING, Protocol

import anyio
import kr8s
import structlog

# `Manifest` is a runtime import, not a type-only one: the memory object
# stream is parameterised with it, and that subscript is evaluated.
from .apply import DEFAULT_BARRIER_PRIORITY, KindNotServedError, Manifest

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence

    from anyio.streams.memory import MemoryObjectReceiveStream
    from kr8s.asyncio.objects import APIObject

_log = structlog.get_logger()

DEFAULT_SETTLE_SECONDS = 60.0
"""How long a barrier may make no progress before this gives up on it."""

DEFAULT_CONCURRENCY = 8
"""How many objects are applied at once.

Also the width of the ordering window. Workers pull from a queue sorted by
Helm order, so this many adjacent objects are attempted together and the
ordering holds only between windows, not inside one. See `converge_queue`.
"""

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
class Cause:
    """One `Status.details.causes` entry: which field, and what about it."""

    reason: str
    field_path: str
    message: str


@dataclass(frozen=True)
class Diagnosis:
    """A failed apply, as much of it as the API server was willing to say."""

    message: str
    status_code: int | None = None
    reason: str | None = None
    causes: tuple[Cause, ...] = ()
    retry_after: float | None = None

    def summary(self) -> str:
        """One line for the report, with the field path if there is one.

        The field path is what turns "Deployment/web is invalid" into
        something a person can act on without going back to the cluster.
        """
        fields = ", ".join(cause.field_path for cause in self.causes if cause.field_path)
        parts = [self.message]
        if self.reason:
            parts.append(f"reason={self.reason}")
        if fields:
            parts.append(f"field={fields}")
        return " ".join(parts)


@dataclass(frozen=True)
class Failure:
    """One object that did not apply, and everything known about why."""

    key: tuple[str, str, str]
    disposition: Disposition
    error: str
    diagnosis: Diagnosis | None = None
    attempts: int = 1


@dataclass(frozen=True)
class ConvergeReport:
    """What a run did, and what it could not do.

    `failures` is the interface. A converging run reports partial success by
    design, so its caller has nothing else to decide an exit code from.
    """

    applied: int
    skipped: int
    failures: list[Failure]

    @property
    def ok(self) -> bool:
        return not self.failures


def sort_for_apply(specs: Iterable[Manifest], resource_priority: Mapping[str, int]) -> list[Manifest]:
    """Objects in the order Helm would install them.

    The same numbers `barriers` groups by, flattened into one queue: a kind
    with a lower number comes first, and a kind nobody numbered sorts at
    `DEFAULT_BARRIER_PRIORITY`, which is deliberately not last. See that
    constant in `apply.py` for the six minutes of stalled apply that bought
    the rule.

    Stable, so objects of one kind keep the order the render gave them.
    """
    return sorted(
        specs,
        key=lambda spec: resource_priority.get(object_key(spec)[1], DEFAULT_BARRIER_PRIORITY),
    )


class ApplyOne(Protocol):
    """The "put this object on the cluster" step, as this loop needs it.

    A callable rather than an `Api`, because almost every decision here is
    made from what the call *raises*. A test scripts the exceptions directly
    and needs no API server, fake or otherwise, to reach the branch it is
    about.

    The object it returns matters for one case, and only one: an apply that
    succeeded against an object that is being deleted. See
    `_applied_but_terminating`.
    """

    def __call__(self, spec: Manifest) -> Awaitable[APIObject]: ...


class SkipCheck(Protocol):
    """Decide whether an object is already what it should be.

    Injected, because reading live state has two shapes whose costs differ by
    more than an order of magnitude -- one LIST per kind against one GET per
    object -- and the choice belongs to whoever owns the `Api`. `fastcache`
    builds the one `ekn kubeapply --assume-unchanged` uses; `livestate` holds
    the decision made from live state instead.
    """

    def __call__(self, spec: Manifest) -> Awaitable[bool]: ...


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

NAMESPACE_TERMINATING_REASON = "NamespaceTerminating"
"""The `causes[].reason` on the one `403` that is worth retrying.

Captured on Kubernetes 1.36: `configmaps "after-term" is forbidden: unable
to create new content in namespace ekn-term because it is being terminated`,
with `field: metadata.namespace`.
"""


def diagnose(exc: BaseException) -> Diagnosis:
    """Everything a failed apply can be asked, pulled out of its `Status`.

    A Kubernetes failure carries far more than the sentence `str(exc)`
    returns, and a converging run is exactly where the rest earns its keep:
    the report is the only interface, and "Deployment/web failed" without a
    field path sends a person back to the cluster to ask again.

    Four things come out of the body that the message does not have:

    `reason` is the machine-readable `Status.reason` -- `Invalid`,
    `Forbidden`, `AlreadyExists`, `Timeout` -- which is stable across
    versions in a way the prose is not.

    `causes[].field` is the JSON path of the offending field. For a rejected
    manifest it is the single most useful fact available, and nothing else
    reports it.

    `causes[].reason` is `FieldValueInvalid`, `FieldValueForbidden`,
    `FieldValueRequired` and so on, which is how an immutable StatefulSet
    update is recognised at all -- its message says nothing telling.

    `retryAfterSeconds` is the server stating how long to wait. It is sent
    with 429 and with a 503 from a `Timeout`/`ServerTimeout`, and honouring
    it beats guessing at a backoff curve.
    """
    if not isinstance(exc, kr8s.ServerError):
        return Diagnosis(message=str(exc))
    response = exc.response
    if response is None:
        return Diagnosis(message=str(exc))
    body: object
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return Diagnosis(message=str(exc), status_code=response.status_code)
    details = body.get("details")
    details_dict = details if isinstance(details, dict) else {}
    listed = details_dict.get("causes")
    causes = tuple(
        Cause(
            reason=str(cause.get("reason", "")),
            field_path=str(cause.get("field", "")),
            message=str(cause.get("message", "")),
        )
        for cause in (listed if isinstance(listed, list) else [])
        if isinstance(cause, dict)
    )
    retry_after = details_dict.get("retryAfterSeconds")
    return Diagnosis(
        message=str(exc),
        status_code=response.status_code,
        reason=str(body.get("reason", "")) or None,
        causes=causes,
        retry_after=float(retry_after) if isinstance(retry_after, int | float) else None,
    )


def _is_immutable_error(diagnosis: Diagnosis) -> bool:
    """True for the 422 that means "this field cannot be changed".

    The case that forces this rung to exist is a completed Job:
    `spec.template` is immutable, so every apply after the first fails for
    ever and a converging run can never reach a clean queue.

    Three shapes, all captured from a live API server rather than guessed --
    a Job, a Service and a StatefulSet. The third carries no phrase at all
    and is recognised by its cause reason instead. See `IMMUTABLE_PHRASES`
    and `FORBIDDEN_UPDATE_PHRASE`, which says why that check is narrow.
    """
    for reason, message in [("", diagnosis.message), *((c.reason, c.message) for c in diagnosis.causes)]:
        if any(phrase in message for phrase in IMMUTABLE_PHRASES):
            return True
        if reason == "FieldValueForbidden" and FORBIDDEN_UPDATE_PHRASE in message:
            return True
    return False


def _applied_but_terminating(applied: APIObject) -> Diagnosis | None:
    """The diagnosis for an apply that **succeeded and did not converge**.

    Applying an object that is being deleted returns 200 with a `Warning:`
    header, not an error. Measured on Kubernetes 1.36 against a namespace in
    `phase=Terminating`: `serverside-applied`, exit 0, and the namespace
    still goes away.

    The ladder cannot catch this, because nothing failed. Counting it as
    applied produces a run that cannot finish and cannot say why: a worker
    applies Namespace X and it "succeeds", so it is never retried; X finishes
    terminating; a worker applies an object in X, gets 404, and retries for
    the whole settle window; nothing ever re-applies X. The run ends non-zero
    blaming the object, and the real cause -- a successful apply that was
    undone -- appears nowhere.

    Read from `metadata.deletionTimestamp` rather than from the `Warning:`
    header the API server also sends: the field is the state, the header is a
    message about it. `ssa_apply` already assigns `obj.raw = result`, so this
    costs no extra request.

    Re-applying is inert rather than harmful -- three controlled runs on the
    same terminating namespace (never re-applied, re-applied once, re-applied
    every 5s) all finished in 10-11s -- so the object goes back on the retry
    queue and the ordinary backoff spaces the attempts.
    """
    metadata = applied.raw.get("metadata")
    stamp = metadata.get("deletionTimestamp") if isinstance(metadata, dict) else None
    if not stamp:
        return None
    return Diagnosis(
        message=f"the apply succeeded, but the object is being deleted (deletionTimestamp {stamp})",
        reason="Terminating",
        causes=(Cause(reason="Terminating", field_path="metadata.deletionTimestamp", message=str(stamp)),),
    )


def _blocked_by_a_terminating_namespace(diagnosis: Diagnosis) -> bool:
    """True for the one `403` that waiting fixes.

    Matched on `causes[].reason`, which the API server sets to
    `NamespaceTerminating`, rather than on the message. Every other `403`
    stays TERMINAL: an RBAC denial retried for the whole settle window is a
    slow failure, which is what the ladder exists to avoid.
    """
    return any(cause.reason == NAMESPACE_TERMINATING_REASON for cause in diagnosis.causes)


def classify(exc: BaseException) -> Disposition:
    """What a failed apply means, and therefore what to do about it.

    **`403` and a non-immutable `422` are terminal on purpose.** An RBAC
    refusal and a schema error do not become true by waiting, and retrying
    either for the whole settle window turns a clear failure into a slow
    one -- which is the failure mode this classification exists to prevent.

    **One `403` is retryable: a namespace that is terminating.** Captured on
    Kubernetes 1.36, applying into a namespace mid-deletion answers `403
    Forbidden` with cause reason `NamespaceTerminating`. It is retryable
    because the namespace finishes terminating and this run recreates it --
    it is in the desired set. Pruning makes the case ordinary rather than
    exceptional: prune deletes namespaces while applies are still in flight.
    The discriminator is the machine-readable cause reason, not the wording.

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
            return Disposition.RECREATE if _is_immutable_error(diagnose(exc)) else Disposition.TERMINAL
        if status == HTTPStatus.FORBIDDEN:
            return Disposition.RETRY if _blocked_by_a_terminating_namespace(diagnose(exc)) else Disposition.TERMINAL
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


def _wait_before_retrying(attempt: int, asked_by_server: float | None) -> float:
    """How long to leave the failed queue alone before sweeping it again.

    **The server's own answer wins when it gives one.** `retryAfterSeconds`
    arrives with a 429 and with a 503 from `Timeout`/`ServerTimeout`, and it
    is the API server saying how long it needs. Coming back sooner is what it
    is asking us not to do, so this takes the longer of the two rather than
    the backoff curve's guess.

    `MAX_BACKOFF_SECONDS` does not cap it. A cap on our own guess is
    sensible; a cap on an instruction is just ignoring the instruction more
    politely.
    """
    guess = _backoff(attempt)
    return guess if asked_by_server is None else max(guess, asked_by_server)


@dataclass
class _Pass:
    """What one sweep of the queue accumulated, shared by its workers."""

    lock: anyio.Lock
    last_progress: float
    applied: int = 0
    skipped: int = 0
    retry: list[Manifest] = field(default_factory=list)
    terminal: dict[tuple[str, str, str], Failure] = field(default_factory=dict)
    last_error: dict[tuple[str, str, str], str] = field(default_factory=dict)
    diagnoses: dict[tuple[str, str, str], Diagnosis] = field(default_factory=dict)
    retry_after: float | None = None
    """The longest `retryAfterSeconds` any object of this sweep was given.

    The server's own answer to "how long should I wait", which beats the
    backoff curve when it is offered. The longest rather than the shortest:
    coming back before the API server said to is what it is asking us not
    to do.
    """
    remaining: Counter[str] = field(default_factory=Counter)
    """How many objects of each kind this sweep has left to reach."""
    announced: set[str] = field(default_factory=set)
    """Kinds `announce` has already logged, so each is said once."""

    async def announce(self, kind: str) -> None:
        """Log the first object of *kind* this sweep reaches.

        The only thing a converging run says while it is running, and it
        exists to be aligned against something else. A window of about
        `concurrency` adjacent objects is in flight, so this is the front
        of that window rather than a boundary -- near enough to put a
        measurement taken beside the run on the right kind, which is what
        it is for. Reported from nixlab2, where an apiserver gained 982
        MiB in one 15-second sampling window of a 74-second apply and the
        log held nothing to say which objects were in flight at the time.
        """
        async with self.lock:
            if kind in self.announced:
                return
            self.announced.add(kind)
            count = self.remaining[kind]
        _log.info("converging kind", kind=kind, objects=count)


async def converge_queue(  # noqa: PLR0913 -- every argument is an injected seam; see the Protocols above
    specs: Sequence[Manifest],
    *,
    apply: ApplyOne,
    resource_priority: Mapping[str, int] | None = None,
    delete: DeleteOne | None = None,
    should_skip: SkipCheck | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    settle_seconds: float = DEFAULT_SETTLE_SECONDS,
    allow_recreate: bool = False,
    clock: Callable[[], float] = anyio.current_time,
    sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
) -> ConvergeReport:
    """Apply everything, in Helm order, `concurrency` objects at a time.

    One queue rather than a barrier per priority. Objects are sorted by
    `resource_priority` -- Helm's InstallOrder, as `ekn.resourcePriority`
    gives it -- and workers pull from the front, so the set in flight is a
    window of about `concurrency` adjacent objects in that order.

    **That is approximate ordering, not the strict barriers
    `apply_and_prune` uses, and the retry is what pays for it.** A strict
    barrier guarantees every CustomResourceDefinition is Established before
    any custom resource is attempted. A window does not: at a priority
    boundary a worker can reach a custom resource while its CRD is still
    being served. That arrives as `KindNotServedError`, classifies RETRY, and
    the next sweep gets it. So the ordering is a heuristic that makes retries
    rare, and convergence is what makes them harmless -- which is only true
    because this loop converges. Do not lift this design into a run that
    aborts on first failure.

    Each object is: skip if `should_skip` says so, otherwise apply. The skip
    check is injected because reading live state has two shapes with very
    different costs -- one LIST per kind, or one GET per object -- and that
    choice belongs to the caller that owns the `Api`, not here.

    `clock` and `sleep` are arguments so a test of the settle timer costs no
    wall time. `anyio.current_time` and not `time.monotonic`: it is the event
    loop's own clock, the one anyio's timeouts measure against, so a
    `fail_after` wrapped around this agrees with the settle timer rather than
    drifting from it.
    """
    pending = sort_for_apply(specs, resource_priority or {})
    workers = max(1, concurrency)
    applied = skipped = 0
    terminal: dict[tuple[str, str, str], Failure] = {}
    last_error: dict[tuple[str, str, str], str] = {}
    diagnoses: dict[tuple[str, str, str], Diagnosis] = {}
    # How many sweeps each object has been through. A report that says an
    # object failed once and a report that says it failed eleven times are
    # different reports, and only the second one says "this is not coming
    # back on its own".
    attempts: Counter[tuple[str, str, str]] = Counter()
    last_progress = clock()
    attempt = 0

    while pending:
        for spec in pending:
            attempts[object_key(spec)] += 1
        state = _Pass(
            lock=anyio.Lock(),
            last_progress=last_progress,
            remaining=Counter(str(spec.get("kind", "?")) for spec in pending),
        )
        # Buffered to the whole sweep and closed before any worker starts, so
        # a send never blocks and `receive_nowait` ends cleanly on EndOfStream.
        # Nothing is put back during a sweep: a retry goes to the next one,
        # which is what keeps the settle timer meaningful.
        send, receive = anyio.create_memory_object_stream[Manifest](max_buffer_size=len(pending))
        for spec in pending:
            send.send_nowait(spec)
        send.close()

        async with anyio.create_task_group() as tg:
            for _ in range(workers):
                tg.start_soon(
                    _worker,
                    receive,
                    state,
                    apply,
                    delete,
                    should_skip,
                    allow_recreate,
                    clock,
                )

        applied += state.applied
        skipped += state.skipped
        terminal.update(state.terminal)
        last_error.update(state.last_error)
        diagnoses.update(state.diagnoses)
        last_progress = state.last_progress
        pending = state.retry

        if not pending:
            break
        if settled(last_progress, clock(), settle_seconds):
            _log.warning(
                "the queue stopped making progress",
                remaining=len(pending),
                settle_seconds=settle_seconds,
            )
            break
        wait = _wait_before_retrying(attempt, state.retry_after)
        _log.info("retrying", objects=len(pending), seconds=f"{wait:.1f}", asked_by_server=state.retry_after)
        await sleep(wait)
        attempt += 1

    stuck = [
        Failure(
            object_key(spec),
            Disposition.RETRY,
            last_error.get(object_key(spec), ""),
            diagnosis=diagnoses.get(object_key(spec)),
            attempts=attempts[object_key(spec)],
        )
        for spec in pending
    ]
    terminal = {key: replace(failure, attempts=attempts[key]) for key, failure in terminal.items()}
    return ConvergeReport(
        applied=applied,
        skipped=skipped,
        failures=[*terminal.values(), *stuck],
    )


async def _worker(  # noqa: PLR0913 -- the shared state of one sweep, passed rather than closed over
    receive: MemoryObjectReceiveStream[Manifest],
    state: _Pass,
    apply: ApplyOne,
    delete: DeleteOne | None,
    should_skip: SkipCheck | None,
    allow_recreate: bool,
    clock: Callable[[], float],
) -> None:
    """Pull objects off the queue until it is empty, applying each."""
    while True:
        try:
            spec = receive.receive_nowait()
        except (anyio.EndOfStream, anyio.WouldBlock):
            return
        await state.announce(str(spec.get("kind", "?")))
        await _process(
            spec,
            state=state,
            apply=apply,
            delete=delete,
            should_skip=should_skip,
            allow_recreate=allow_recreate,
            clock=clock,
        )


async def _process(  # noqa: PLR0913 -- the shared state of one sweep, passed rather than closed over
    spec: Manifest,
    *,
    state: _Pass,
    apply: ApplyOne,
    delete: DeleteOne | None,
    should_skip: SkipCheck | None,
    allow_recreate: bool,
    clock: Callable[[], float],
) -> None:
    """One object: leave it alone if it is unchanged, otherwise apply it."""
    key = object_key(spec)

    if should_skip is not None and await should_skip(spec):
        async with state.lock:
            state.skipped += 1
        _log.debug("unchanged", namespace=key[0], kind=key[1], name=key[2])
        return

    try:
        applied = await apply(spec)
    except Exception as exc:
        # Broad on purpose: `classify` is the whole policy, and it answers
        # TERMINAL for anything it does not know.
        disposition = classify(exc)
        found = diagnose(exc)
        async with state.lock:
            state.last_error[key] = found.summary()
            state.diagnoses[key] = found
            if found.retry_after is not None:
                state.retry_after = max(state.retry_after or 0.0, found.retry_after)
        if disposition is Disposition.RECREATE:
            disposition = await _recreate(
                spec,
                key=key,
                apply=apply,
                delete=delete,
                allow_recreate=allow_recreate,
                last_error=state.last_error,
            )
        async with state.lock:
            if disposition is Disposition.TERMINAL:
                state.terminal[key] = Failure(
                    key,
                    Disposition.TERMINAL,
                    state.last_error[key],
                    diagnosis=state.diagnoses.get(key),
                )
                return
            if disposition is Disposition.RETRY:
                state.retry.append(spec)
                return
            state.applied += 1
            state.last_progress = clock()
        _log.debug("recreated", namespace=key[0], kind=key[1], name=key[2])
    else:
        terminating = _applied_but_terminating(applied)
        if terminating is not None:
            async with state.lock:
                state.last_error[key] = terminating.summary()
                state.diagnoses[key] = terminating
                state.retry.append(spec)
            # `last_progress` deliberately untouched. Nothing converged, and
            # the settle timer has to measure that rather than be reset by an
            # apply the cluster is about to undo.
            _log.debug("applied into a deleting object", namespace=key[0], kind=key[1], name=key[2])
            return
        async with state.lock:
            state.applied += 1
            state.last_progress = clock()
        _log.debug("applied", namespace=key[0], kind=key[1], name=key[2])


async def converge_barrier(  # noqa: PLR0913 -- every argument is an injected seam; see the Protocols above
    specs: Sequence[Manifest],
    *,
    apply: ApplyOne,
    delete: DeleteOne | None = None,
    should_skip: SkipCheck | None = None,
    settle_seconds: float = DEFAULT_SETTLE_SECONDS,
    allow_recreate: bool = False,
    clock: Callable[[], float] = anyio.current_time,
    sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
) -> list[Failure]:
    """`converge_queue` over one priority group, one object at a time.

    The objects that never applied, with the last error each gave. An empty
    list means the group is clean.
    """
    report = await converge_queue(
        specs,
        apply=apply,
        delete=delete,
        should_skip=should_skip,
        concurrency=1,
        settle_seconds=settle_seconds,
        allow_recreate=allow_recreate,
        clock=clock,
        sleep=sleep,
    )
    return report.failures


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
