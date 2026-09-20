"""Move server-side-apply ownership from one field manager onto another.

**Why this has to exist.** Server-side apply removes a field only when its
*owning* manager stops declaring it. So a field written by one manager and not
declared by its successor stays on the object for ever: the successor's apply
cannot drop what it does not own, and `--force-conflicts` does not help,
because two different fields are not a conflict at all.

That is permanent once the first manager stops applying. The visible form is
an object the API server refuses outright -- a probe whose handler type
changed keeps both handlers, and every apply after it fails with `may not
specify more than 1 handler type`.

**When it happens.** Only on a transition: an object applied first under one
manager, and later under another. `deployment.fieldManager` is what stops the
transition happening by accident, and this is what repairs the objects that
already crossed one.

**The mechanism, and it is the discouraged one.** Kubernetes says
`metadata.managedFields` is the API server's and that you should not write it,
"except in exceptional circumstances" -- naming an inconsistent state as the
case it is for. This is that case, and there is no supported alternative: the
supported path is for the owning manager to apply again without the field, and
the owning manager is the one that has stopped applying.

So each entry is **renamed**, not deleted. Deleting leaves the fields unowned,
and nothing ever removes an unowned field, which is the bug rather than the
fix.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

import structlog

from .apply import build_object

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from kr8s.asyncio import Api

    from .apply import Manifest

log = structlog.get_logger(__name__)


def _entry_slot(entry: Mapping[str, Any]) -> tuple[str, str, str]:
    """What makes two entries the same owner record.

    The API server keys an entry by manager, operation, apiVersion and
    subresource. Two entries that agree on everything but the manager are the
    pair a rename has to merge rather than duplicate.
    """
    return (
        str(entry.get("operation", "")),
        str(entry.get("apiVersion", "")),
        str(entry.get("subresource", "")),
    )


def _union(left: Any, right: Any) -> Any:
    """Union two `fieldsV1` trees.

    A `fieldsV1` value is a nested object whose leaves are `{}` -- the keys
    carry the meaning and the values are empty. So the union is a recursive
    merge, and a leaf on either side wins over nothing.
    """
    if not isinstance(left, dict) or not isinstance(right, dict):
        return right or left
    merged = dict(left)
    for key, value in right.items():
        merged[key] = _union(merged[key], value) if key in merged else value
    return merged


def rewrite_managed_fields(
    entries: Sequence[Mapping[str, Any]],
    *,
    old: Iterable[str],
    new: str,
) -> list[dict[str, Any]] | None:
    """`entries` with every `old` manager's Apply record renamed to `new`.

    `None` when nothing matched, so a caller can skip the write rather than
    send a patch that changes nothing.

    **`Apply` records only.** An `Update` record is a different thing: it says
    a client wrote the field with a plain update rather than an apply, and
    server-side apply does not consult it for removal. Renaming one would
    claim ownership this cannot act on, and `kube-controller-manager`'s
    `status` record is exactly that shape.
    """
    old_names = {name for name in old if name}
    if not old_names or new in old_names:
        return None

    kept: list[dict[str, Any]] = []
    renamed: list[dict[str, Any]] = []
    for entry in entries:
        record = copy.deepcopy(dict(entry))
        if record.get("manager") in old_names and record.get("operation") == "Apply":
            record["manager"] = new
            renamed.append(record)
        else:
            kept.append(record)

    if not renamed:
        return None

    # Fold each renamed record into an existing record of `new` in the same
    # slot. Two records with one manager, operation and apiVersion is a shape
    # the API server does not produce, and it must not be one this writes.
    result: list[dict[str, Any]] = []
    by_slot: dict[tuple[str, str, str], dict[str, Any]] = {}
    for record in kept:
        if record.get("manager") == new and record.get("operation") == "Apply":
            by_slot[_entry_slot(record)] = record
        result.append(record)

    for record in renamed:
        existing = by_slot.get(_entry_slot(record))
        if existing is None:
            by_slot[_entry_slot(record)] = record
            result.append(record)
            continue
        existing["fieldsV1"] = _union(existing.get("fieldsV1") or {}, record.get("fieldsV1") or {})

    return result


async def reclaim_one(
    spec: Manifest,
    api: Api,
    *,
    old: Iterable[str],
    new: str,
    dry_run: bool = False,
) -> bool:
    """Take ownership of one object's fields. True when it changed.

    A missing object is not an error: the configuration names objects that a
    cluster may not have yet, and this runs before an apply rather than after.
    """
    obj = await build_object(spec, api)
    try:
        await obj.async_refresh()
    except Exception:
        log.debug("reclaim_object_absent", kind=obj.kind, name=obj.name, namespace=obj.namespace)
        return False

    entries = obj.raw.get("metadata", {}).get("managedFields") or []
    rewritten = rewrite_managed_fields(entries, old=old, new=new)
    if rewritten is None:
        return False

    owners = sorted({str(e.get("manager")) for e in entries if e.get("operation") == "Apply"})
    log.info(
        "reclaim_object",
        kind=obj.kind,
        name=obj.name,
        namespace=obj.namespace,
        owners=owners,
        new=new,
        dry_run=dry_run,
    )
    if dry_run:
        return True

    # A merge patch, deliberately not a server-side apply. An apply cannot
    # write `managedFields` -- the API server owns that field and computes it
    # from the apply itself. A plain patch is the documented way in, and the
    # one Kubernetes calls discouraged.
    await obj.async_patch({"metadata": {"managedFields": rewritten}})
    return True


async def reclaim(
    specs: Sequence[Manifest],
    api: Api,
    *,
    old: Iterable[str],
    new: str,
    dry_run: bool = False,
) -> int:
    """Reclaim every object in `specs`. Returns how many changed.

    Serial, and not a fan-out. This rewrites ownership records, it runs once
    per transition rather than on every apply, and a readable log of what it
    touched is worth more here than the seconds concurrency would save.
    """
    changed = 0
    for spec in specs:
        if await reclaim_one(spec, api, old=old, new=new, dry_run=dry_run):
            changed += 1
    return changed


__all__ = ["reclaim", "reclaim_one", "rewrite_managed_fields"]
