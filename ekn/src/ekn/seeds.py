# SPDX-License-Identifier: MIT
"""Bootstrap credentials seeded from the environment at apply time.

A cluster cannot invent its own ArgoCD repository credential, nor the
credential for the external secret store every other secret then lives in.
Those seeds come from outside. `ekn.envSeed "VARNAME"` puts a reference in
the manifest instead of the value, and this module resolves it against the
environment when the objects are applied.

Nothing about a secret ever reaches Nix, which could otherwise write it to
`/nix/store`. Evaluation handles the *name* of a variable; the value is read
here and nowhere else.

Two pieces of data come out of the module system, and they do different jobs.
The `ekn.dev/env-<n>` annotations say *that* an object is seeded and *which*
variables it wants -- a shallow key lookup, so nothing has to search a whole
resource set. The `$ekn:env:VARNAME` reference in a field says *where* the
value goes, and is found by walking one already-identified object.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Any, NamedTuple

import kr8s
import structlog

from ekn.apply import build_object

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from kr8s.asyncio import Api
    from nanopynix.models import JsonValue

    from ekn.apply import Manifest

_log = structlog.get_logger()

#: Prefix of a rendered reference. Must match `envSeedPrefix` in
#: easykubenix/lib/envSeed.nix -- the two halves of one contract.
REFERENCE_PREFIX = "$ekn:env:"

#: Prefix of the annotations `ekn.envSeeded` writes. Must match
#: `envSeedAnnotationPrefix` in the same file.
ANNOTATION_PREFIX = "ekn.dev/env-"

_ANNOTATION_INDEX = re.compile(rf"^{re.escape(ANNOTATION_PREFIX)}(\d+)$")


class SeedReference(NamedTuple):
    """One field of one object, and the variable that fills it."""

    path: tuple[str, ...]
    variable: str

    @property
    def field(self) -> str:
        return ".".join(self.path)


class SeedAction(NamedTuple):
    """What an apply will do about one seeded object."""

    #: "create", "update", "unchanged" or "skip".
    verb: str
    namespace: str
    kind: str
    name: str
    variables: list[str]

    @property
    def identity(self) -> str:
        return f"{self.namespace}/{self.kind}/{self.name}"


class MissingVariablesError(Exception):
    """Seeded objects are absent from the cluster and their variables unset.

    Raised before anything is applied, and listing every missing variable at
    once: bootstrapping a cluster with two seeds must not be two failed
    applies.
    """

    def __init__(self, missing: list[tuple[SeedAction, list[str]]]) -> None:
        self.missing = missing
        lines = [
            "Bootstrap credentials are missing from the environment.",
            "",
        ]
        for action, variables in missing:
            lines.extend(f"  {variable} is not set" for variable in variables)
            lines.append(f"    needed by {action.identity}")
            lines.append("")
        lines.append("Export them and run the apply again. They are read once,")
        lines.append("stored in the cluster, and not needed afterwards.")
        super().__init__("\n".join(lines))


def annotated_variables(obj: Manifest) -> list[str]:
    """The variables an object declares, read from its annotations.

    Shallow by design. This is what every caller uses to decide whether an
    object is seeded at all, so that deciding never costs a walk.
    """
    metadata = obj.get("metadata")
    if not isinstance(metadata, dict):
        return []
    annotations = metadata.get("annotations")
    if not isinstance(annotations, dict):
        return []
    indexed: list[tuple[int, str]] = []
    for key, value in annotations.items():
        match = _ANNOTATION_INDEX.match(key)
        if match is not None and isinstance(value, str):
            indexed.append((int(match.group(1)), value))
    return [variable for _, variable in sorted(indexed)]


def is_seeded(obj: Manifest) -> bool:
    """Whether an object declares seeded fields."""
    return bool(annotated_variables(obj))


def _walk(value: JsonValue, path: tuple[str, ...]) -> Iterator[SeedReference]:
    if isinstance(value, str):
        if value.startswith(REFERENCE_PREFIX):
            yield SeedReference(path, value[len(REFERENCE_PREFIX) :])
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _walk(child, (*path, str(key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, (*path, str(index)))


def references(obj: Manifest) -> list[SeedReference]:
    """Every reference in one object, with the path it sits at.

    Only ever called for an object whose annotations already said it is
    seeded, so this walk is bounded to the handful of objects that are.
    """
    return list(_walk(obj, ()))


def _set_at(obj: Manifest, path: tuple[str, ...], value: str) -> Manifest:
    """Return a copy of `obj` with `path` set to `value`.

    Structural rather than textual. Substituting into rendered JSON text
    would break on a value holding a quote, a backslash or a newline; setting
    a parsed field has no such failure.
    """
    if not path:
        raise ValueError("cannot replace the whole object")
    head, *rest = path
    updated: dict[str, Any] = dict(obj)
    if rest:
        child = updated.get(head)
        if not isinstance(child, dict):
            raise TypeError(f"cannot descend into {head!r}: not an object")
        updated[head] = _set_at(child, tuple(rest), value)
    else:
        updated[head] = value
    return updated


def substitute(obj: Manifest, values: dict[str, str]) -> Manifest:
    """Replace every reference in `obj` with the value of its variable."""
    result = obj
    for reference in references(obj):
        if reference.variable in values:
            result = _set_at(result, reference.path, values[reference.variable])
    return result


def _identity(obj: Manifest) -> tuple[str, str, str]:
    metadata = obj.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    namespace = metadata.get("namespace")
    name = metadata.get("name")
    kind = obj.get("kind")
    return (
        namespace if isinstance(namespace, str) else "none",
        kind if isinstance(kind, str) else "<unknown>",
        name if isinstance(name, str) else "<unnamed>",
    )


async def _live_values(obj: Manifest, api: Api) -> dict[str, str] | None:
    """The live object's `stringData`-equivalent values, or None if absent.

    A Secret comes back with base64 `data` rather than the `stringData` that
    was applied, so decode it. Used only to decide whether a set variable
    would actually change anything.
    """
    import base64

    live = await build_object(obj, api)
    try:
        await live.async_refresh()
    except kr8s.NotFoundError:
        return None
    raw = dict(live.raw)
    data = raw.get("data")
    values: dict[str, str] = {}
    if isinstance(data, dict):
        for key, encoded in data.items():
            if isinstance(encoded, str):
                try:
                    values[str(key)] = base64.b64decode(encoded).decode()
                except (ValueError, UnicodeDecodeError):
                    # A binary value is simply not comparable; leave it out
                    # and let the "would this change anything" test say yes.
                    continue
    return values


def _would_change(obj: Manifest, live: dict[str, str], values: dict[str, str]) -> bool:
    """Whether substituting `values` would change the live object."""
    for reference in references(obj):
        if reference.variable not in values:
            continue
        # `stringData.password` is applied, `data.password` is stored.
        key = reference.path[-1]
        if live.get(key) != values[reference.variable]:
            return True
    return False


class SeedPlan(NamedTuple):
    """The apply set, rewritten, plus what happened to each seed."""

    objects: list[Manifest]
    actions: list[SeedAction]


async def resolve(objects: Iterable[Manifest], *, api: Api) -> SeedPlan:
    """Decide what to do with every seeded object, before anything applies.

    Four cases, and exactly one of them is an error:

    ==================  ==========================  =========================
    .                   variable set                variable unset
    ==================  ==========================  =========================
    not in cluster      substitute and create       abort, listing them all
    in cluster          equal is a no-op, different  skip, leave it alone
                        substitutes and updates
    ==================  ==========================  =========================

    "In cluster, variable unset" is the steady state, not an edge case: a
    seed is exported once, applied once, and dropped, so every apply after
    the bootstrap takes that cell.

    A skipped object is REMOVED from the returned apply set rather than
    applied unchanged. `ssa_apply` is a real server-side apply under field
    manager `ekn`, and server-side apply deletes fields that manager
    previously owned and then omits -- so applying the rendered object
    (whose field still holds the reference) would overwrite the live
    credential with the literal `$ekn:env:VARNAME`.
    """
    planned: list[Manifest] = []
    actions: list[SeedAction] = []
    missing: list[tuple[SeedAction, list[str]]] = []

    for obj in objects:
        variables = annotated_variables(obj)
        if not variables:
            planned.append(obj)
            continue

        namespace, kind, name = _identity(obj)
        values = {variable: os.environ[variable] for variable in variables if variable in os.environ}
        unset = [variable for variable in variables if variable not in values]
        live = await _live_values(obj, api)

        if live is None:
            verb = "create"
        elif not values:
            # The steady state. Leave it alone, and do not apply it.
            verb = "skip"
        elif _would_change(obj, live, values):
            verb = "update"
        else:
            verb = "unchanged"

        action = SeedAction(verb=verb, namespace=namespace, kind=kind, name=name, variables=variables)
        if live is None and unset:
            missing.append((action, unset))
            continue
        if verb in {"create", "update"}:
            planned.append(substitute(obj, values))
        actions.append(action)

    if missing:
        raise MissingVariablesError(missing)

    return SeedPlan(objects=planned, actions=actions)


class SeedRow(NamedTuple):
    """One variable a configuration expects, and what is known about it."""

    variable: str
    namespace: str
    kind: str
    name: str
    field: str
    is_set: bool
    #: True, False, or None when no cluster was reachable.
    in_cluster: bool | None

    @property
    def identity(self) -> str:
        return f"{self.namespace}/{self.kind}/{self.name}"


def describe(objects: Iterable[Manifest]) -> list[SeedRow]:
    """Every variable the given objects expect, without touching a cluster.

    The field comes from the reference itself, so it cannot drift from where
    the value will actually land. A variable an object declares but never
    references reports an empty field rather than being dropped: that
    mismatch is worth seeing.
    """
    rows: list[SeedRow] = []
    for obj in objects:
        variables = annotated_variables(obj)
        if not variables:
            continue
        namespace, kind, name = _identity(obj)
        fields = {reference.variable: reference.field for reference in references(obj)}
        rows.extend(
            SeedRow(
                variable=variable,
                namespace=namespace,
                kind=kind,
                name=name,
                field=fields.get(variable, ""),
                is_set=variable in os.environ,
                in_cluster=None,
            )
            for variable in variables
        )
    return rows


async def inspect(rows: Iterable[SeedRow], *, api: Api | None) -> list[SeedRow]:
    """Fill in `in_cluster` for each row, when a cluster is reachable.

    One lookup per object rather than per variable, and `None` throughout
    when there is no cluster -- "unknown" and "absent" are different answers,
    and an operator bringing up a new cluster needs to tell them apart.
    """
    rows = list(rows)
    if api is None:
        return rows
    present: dict[tuple[str, str, str], bool] = {}
    result: list[SeedRow] = []
    for row in rows:
        key = (row.namespace, row.kind, row.name)
        if key not in present:
            probe: Manifest = {
                "apiVersion": "v1",
                "kind": row.kind,
                "metadata": {"name": row.name, "namespace": row.namespace},
            }
            present[key] = await _live_values(probe, api) is not None
        result.append(row._replace(in_cluster=present[key]))
    return result


def table(rows: Iterable[SeedRow]) -> str:
    """Render seed rows as a plain table.

    `ekn secrets` exists to be read, and its columns are wide -- a variable
    name beside a namespace/kind/name. One structured log line per seed is
    legible for one seed and stops being so at five.

    `in_cluster` is "?" when no cluster was reachable, because unknown and
    absent are different answers.
    """
    rows = list(rows)
    header = ("VARIABLE", "OBJECT", "FIELD", "SET", "IN CLUSTER")
    body = [
        (
            row.variable,
            row.identity,
            row.field,
            "yes" if row.is_set else "no",
            "?" if row.in_cluster is None else ("yes" if row.in_cluster else "no"),
        )
        for row in rows
    ]
    widths = [max(len(cell) for cell in column) for column in zip(header, *body, strict=False)]
    lines = [
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        for row in (header, *body)
    ]
    return "\n".join(lines)


def report(actions: Iterable[SeedAction]) -> None:
    """Say what happened to each seed, by name and never by value.

    A seed is meant to be exported once and then dropped, and that only works
    if the operator can confirm the value reached the cluster.
    """
    for action in actions:
        _log.info(
            "seed",
            verb=action.verb,
            object=action.identity,
            variables=",".join(action.variables),
        )


__all__ = [
    "ANNOTATION_PREFIX",
    "REFERENCE_PREFIX",
    "MissingVariablesError",
    "SeedAction",
    "SeedPlan",
    "SeedReference",
    "SeedRow",
    "annotated_variables",
    "describe",
    "inspect",
    "is_seeded",
    "references",
    "report",
    "resolve",
    "substitute",
    "table",
]
