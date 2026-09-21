"""What this machine last applied, checked against what the cluster holds now,
so an unchanged object costs no PATCH.

`ekn kubeapply --assume-unchanged` reads it. An object is left alone only when
two independent things agree:

- **the bytes**, from this file: the manifest and field manager are what this
  machine last applied to this cluster;
- **the `resourceVersion`**, from `livestate.sweep`: the object is still at
  the version that apply returned, so nothing has written it since.

Either half alone is wrong. The file is a claim about the cluster made from a
laptop, and it is stale the moment anybody else writes -- a GitOps engine
syncing another commit, a `kubectl edit`, a delete. The sweep sees those, and
sees nothing about whether the manifest itself changed.

**`resourceVersion` and not the manifest hash.** It moves on a write by
anyone, including a writer reusing our own field-manager name, and a
server-side apply that changes nothing does not move it -- so `ekn` does not
invalidate its own record every run. Measured on nixlab2 over three idle
minutes: 0 of 1000 objects moved.

**Keyed by the cluster's own identity, never by the kubeconfig context
name.** `ekn validate` boots a fresh API server under a fixed context, so a
name-keyed cache would call every object unchanged against an empty cluster
and report success. The uid of the `kube-system` Namespace is the identity
Kubernetes itself offers.

**A credential is never recorded.** The rest of a manifest is in git, so a
digest of the sent bytes is a brute-force oracle for the one field that is
not. An object carrying a `sops:` block, or a seed `ekn` fills from the
environment, is applied on every run.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from kr8s.asyncio.objects import Namespace

from .converge import object_key
from .livestate import canonical_json

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

    from kr8s.asyncio import Api

    from .apply import Manifest
    from .livestate import LiveObject

_log = structlog.get_logger()

FORMAT_VERSION = 2
"""Bumped when the file's shape changes. An older file is discarded, not read.

A digest whose meaning moved -- a new input, a different canonicalisation --
answers "unchanged" for an object that is not, which is the one failure this
cache must not have.

A version 1 entry is a digest with no `resourceVersion` to check it against,
which nothing may skip on. Discarding the file and applying once is the
cheaper of the two ways to answer that.
"""

IDENTITY_NAMESPACE = "kube-system"
"""The Namespace whose uid names the cluster.

Every cluster has it, it is created once at bootstrap and never re-created,
and reading it needs no permission an apply does not already have. This is
also what `kubeadm` and several operators use as a cluster id.
"""


def digest(spec: Manifest, *, field_manager: str) -> str:
    """The value this cache stores for one object.

    The field manager is part of it. The same bytes applied under another
    manager are a different apply -- the manager is what decides field
    ownership -- so a `deployment.fieldManager` change has to miss.

    `canonical_json` is `livestate`'s, and deliberately the same function: two
    canonicalisations in one program is two answers to "are these the same
    object".
    """
    payload: Manifest = {"fieldManager": field_manager, "object": spec}
    return f"sha256:{hashlib.sha256(canonical_json(payload).encode()).hexdigest()}"


def cache_root() -> Path:
    """Where the per-cluster files live.

    `EKN_APPLY_CACHE_DIR` overrides it, for a test and for anyone who keeps
    state somewhere else.
    """
    override = os.environ.get("EKN_APPLY_CACHE_DIR")
    if override:
        return Path(override)
    state = os.environ.get("XDG_STATE_HOME")
    base = Path(state) if state else Path.home() / ".local" / "state"
    return base / "ekn" / "apply-cache"


@dataclass(frozen=True)
class Entry:
    """One object as this machine last applied it."""

    digest: str
    resource_version: str | None = None
    """What the apply response reported. `None` is an entry nothing can skip
    on: a version 1 file, or an apply whose response carried no version.
    """


@dataclass
class ApplyCache:
    """One cluster's record, for the length of one command.

    `environment` selects a section of the file rather than a file of its own:
    a cluster holds several environments, and a uid is a safe file name where
    an environment name is not.

    `live` is `livestate.sweep`'s result, set by the caller before the first
    apply. `None` means no sweep ran, and then **nothing is skippable** --
    this cache alone cannot tell a correct object from one somebody edited.

    `skipped` and `recorded` are counted here because both apply paths funnel
    through these two methods, and the run has to be able to say how much it
    left alone.
    """

    path: Path
    environment: str
    entries: dict[str, dict[str, Entry]] = field(default_factory=dict)
    assume_unchanged: bool = False
    never_record: frozenset[tuple[str, str, str]] = frozenset()
    live: Mapping[tuple[str, str, str], LiveObject] | None = None
    skipped: int = 0
    recorded: int = 0
    _dirty: bool = False

    def _section(self) -> dict[str, Entry]:
        return self.entries.setdefault(self.environment, {})

    def primed(self) -> bool:
        """True when some object of this environment can be skipped at all.

        False on the first run after this file was created or its format
        changed: every entry then has a digest and no `resourceVersion`, so
        the run applies everything and records it. That is a one-time cost and
        the caller says so, because `applied=1241` under a flag called
        "assume unchanged" reads as a regression otherwise.
        """
        return any(entry.resource_version for entry in self._section().values())

    def sent_before(self, spec: Manifest, *, field_manager: str) -> bool:
        """True when this machine last sent exactly these bytes as this manager.

        Half of `unchanged`, and never a reason to skip on its own. It is
        public because it costs nothing, where the other half needs the object
        built: a caller that would have to discover an unserved kind to build
        one asks this first.
        """
        identity = object_key(spec)
        if not self.assume_unchanged or identity in self.never_record:
            return False
        entry = self._section().get(_entry_key(identity))
        return entry is not None and entry.digest == digest(spec, field_manager=field_manager)

    def unchanged(self, spec: Manifest, *, field_manager: str, key: tuple[str, str, str]) -> bool:
        """True when this object may be left alone.

        *key* is the **built** object's `(namespace, kind, name)`, which is
        what the sweep saw. A manifest that names no namespace resolves to the
        API's default one, so a key taken from the manifest reads `none` where
        the cluster reads `default` -- and this would then skip nothing, for
        ever, while looking healthy.

        False whenever the operator did not ask for the skip, so a caller can
        hold one cache and let this decide.
        """
        if not self.sent_before(spec, field_manager=field_manager):
            return False
        entry = self._section()[_entry_key(object_key(spec))]
        if self.live is None or entry.resource_version is None:
            return False
        seen = self.live.get(key)
        if seen is None or seen.resource_version != entry.resource_version:
            return False
        self.skipped += 1
        return True

    def record(self, spec: Manifest, *, field_manager: str, resource_version: str | None) -> None:
        """Remember that this object was applied, exactly as it was sent.

        *resource_version* is the one the apply response carried. `None`
        records an entry that can never be skipped on, which is right: a run
        that did not learn where the object landed cannot tell later whether
        it moved.

        Called on every successful apply, whether or not the skip is on:
        otherwise the first `--assume-unchanged` run after an ordinary one has
        nothing to read and applies everything.
        """
        identity = object_key(spec)
        if identity in self.never_record:
            return
        self._section()[_entry_key(identity)] = Entry(
            digest=digest(spec, field_manager=field_manager),
            resource_version=resource_version,
        )
        self.recorded += 1
        self._dirty = True

    def save(self) -> None:
        """Write the file, or say why not and carry on.

        **A cache that cannot be written is not a failed apply.** The apply
        already happened; losing the record costs the next run some requests.
        The build sandboxes this runs in have no writable home at all.
        """
        if not self._dirty:
            return
        written = {
            environment: {key: _unparse(entry) for key, entry in section.items()}
            for environment, section in self.entries.items()
        }
        body = json.dumps({"version": FORMAT_VERSION, "entries": written}, indent=1, sort_keys=True)
        temporary = self.path.with_name(f"{self.path.name}.new")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # 0o600 and a rename: the file holds digests of everything this
            # cluster runs, and a half-written one read by the next run would
            # be discarded as corrupt -- losing every entry rather than the
            # ones this command added.
            temporary.write_text(body)
            temporary.chmod(0o600)
            temporary.replace(self.path)
        except OSError as exc:
            _log.debug("apply cache not written", path=str(self.path), error=str(exc))
            return
        self._dirty = False


def _entry_key(identity: tuple[str, str, str]) -> str:
    return "/".join(identity)


def _unparse(entry: Entry) -> dict[str, str | None]:
    return {"digest": entry.digest, "resourceVersion": entry.resource_version}


def _parse(value: Any) -> Entry | None:
    if not isinstance(value, dict):
        return None
    stored = value.get("digest")
    version = value.get("resourceVersion")
    if not isinstance(stored, str):
        return None
    return Entry(digest=stored, resource_version=version if isinstance(version, str) else None)


def _read(path: Path) -> dict[str, dict[str, Entry]]:
    """The file's entries, or nothing at all.

    Every failure answers the same way, and the answer is always the safe one:
    an empty cache applies everything. A missing file is the ordinary first
    run; a corrupt one is a crash mid-write or an older format.
    """
    try:
        loaded: Any = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(loaded, dict) or loaded.get("version") != FORMAT_VERSION:
        return {}
    entries = loaded.get("entries")
    if not isinstance(entries, dict):
        return {}
    read: dict[str, dict[str, Entry]] = {}
    for environment, section in entries.items():
        if not isinstance(section, dict):
            continue
        parsed = {str(key): entry for key, value in section.items() if (entry := _parse(value)) is not None}
        read[str(environment)] = parsed
    return read


class ClusterIdUnreadableError(RuntimeError):
    """The `kube-system` Namespace could not be read, or carried no uid."""


async def read_cluster_id(api: Api) -> str:
    """The uid of the `kube-system` Namespace, or raise saying why.

    Separate from `cluster_id` below because two callers want opposite
    things from the same read. The cache may carry on without an identity;
    `clusterfence` must refuse, and a refusal that cannot say *why* the
    cluster could not be named sends the reader looking in the wrong place.
    """
    try:
        namespace = await Namespace.async_get(IDENTITY_NAMESPACE, api=api)
    except Exception as exc:
        raise ClusterIdUnreadableError(f"cannot read the {IDENTITY_NAMESPACE} Namespace: {exc}") from exc
    metadata = namespace.raw.get("metadata")
    uid = metadata.get("uid") if isinstance(metadata, dict) else None
    if not isinstance(uid, str) or not uid:
        raise ClusterIdUnreadableError(f"the {IDENTITY_NAMESPACE} Namespace carries no uid")
    return uid


async def cluster_id(api: Api) -> str | None:
    """The uid of the `kube-system` Namespace, or None if it cannot be read.

    None is not an error. It means this command cannot tell which cluster it
    is talking to, so it must neither trust nor add to a record -- the caller
    applies everything instead.
    """
    try:
        return await read_cluster_id(api)
    except ClusterIdUnreadableError as exc:
        # Every failure means the same thing here -- this command cannot name
        # the cluster -- and none of them is worth failing an apply over.
        _log.debug("cluster identity unreadable", namespace=IDENTITY_NAMESPACE, error=str(exc))
        return None


async def open_cache(
    api: Api,
    *,
    environment: str,
    cluster: str | None = None,
    assume_unchanged: bool = False,
    never_record: Collection[tuple[str, str, str]] = (),
) -> ApplyCache | None:
    """This cluster's cache, or None when the cluster cannot be identified.

    *cluster* is the uid when the caller already read it -- `clusterfence`
    does, on every fenced command -- so an apply asks the API server for the
    `kube-system` Namespace once rather than twice.
    """
    if cluster is None:
        cluster = await cluster_id(api)
    if cluster is None:
        if assume_unchanged:
            _log.warning(
                "applying everything: --assume-unchanged needs the cluster's identity",
                namespace=IDENTITY_NAMESPACE,
            )
        return None
    try:
        root = cache_root()
    except RuntimeError as exc:
        # `Path.home()` on a machine with no home at all. An apply must not
        # fail because a cache has nowhere to live.
        _log.debug("apply cache has no directory", error=str(exc))
        return None
    path = root / f"{cluster}.json"
    return ApplyCache(
        path=path,
        environment=environment,
        entries=_read(path),
        assume_unchanged=assume_unchanged,
        never_record=frozenset(never_record),
    )


__all__ = [
    "FORMAT_VERSION",
    "IDENTITY_NAMESPACE",
    "ApplyCache",
    "ClusterIdUnreadableError",
    "Entry",
    "cache_root",
    "cluster_id",
    "digest",
    "open_cache",
    "read_cluster_id",
]
