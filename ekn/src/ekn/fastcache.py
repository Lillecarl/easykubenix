"""What this machine last applied, recorded locally, so an unchanged object
costs no request at all.

`ekn kubeapply --assume-unchanged` reads it. An object whose bytes, field
manager, environment and cluster all match the last successful apply is left
alone -- no PATCH, and nothing read to decide it.

**It is a claim about the cluster, made from a file on a laptop.** Nothing
validates it, so it is wrong the moment anybody else writes: a GitOps engine
syncing a different commit, a `kubectl edit`, a deleted object. That is why
the skip is opt-in and the recording is not, and why the escape is to leave
the flag off.

`livestate` answers the same question exactly, with one metadata LIST per
kind -- 2.56 MB for a 997-object cluster, and it sees drift. This one costs a
single GET. Issue #28 holds that design; a `--prune` run pays for its sweep
anyway, so that is where to go if this blindness ever bites.

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
    from collections.abc import Collection

    from kr8s.asyncio import Api

    from .apply import Manifest

_log = structlog.get_logger()

FORMAT_VERSION = 1
"""Bumped when the file's shape changes. An older file is discarded, not read.

A digest whose meaning moved -- a new input, a different canonicalisation --
answers "unchanged" for an object that is not, which is the one failure this
cache must not have.
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


@dataclass
class ApplyCache:
    """One cluster's record, for the length of one command.

    `environment` selects a section of the file rather than a file of its own:
    a cluster holds several environments, and a uid is a safe file name where
    an environment name is not.

    `skipped` and `recorded` are counted here because both apply paths funnel
    through these two methods, and the run has to be able to say how much it
    left alone.
    """

    path: Path
    environment: str
    entries: dict[str, dict[str, str]] = field(default_factory=dict)
    assume_unchanged: bool = False
    never_record: frozenset[tuple[str, str, str]] = frozenset()
    skipped: int = 0
    recorded: int = 0
    _dirty: bool = False

    def _section(self) -> dict[str, str]:
        return self.entries.setdefault(self.environment, {})

    def unchanged(self, spec: Manifest, *, field_manager: str) -> bool:
        """True when this object may be left alone.

        False whenever the operator did not ask for the skip, so a caller can
        hold one cache and let this decide.
        """
        identity = object_key(spec)
        if not self.assume_unchanged or identity in self.never_record:
            return False
        if self._section().get(_entry_key(identity)) != digest(spec, field_manager=field_manager):
            return False
        self.skipped += 1
        return True

    def record(self, spec: Manifest, *, field_manager: str) -> None:
        """Remember that this object was applied, exactly as it was sent.

        Called on every successful apply, whether or not the skip is on:
        otherwise the first `--assume-unchanged` run after an ordinary one has
        nothing to read and applies everything.
        """
        identity = object_key(spec)
        if identity in self.never_record:
            return
        self._section()[_entry_key(identity)] = digest(spec, field_manager=field_manager)
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
        body = json.dumps({"version": FORMAT_VERSION, "entries": self.entries}, indent=1, sort_keys=True)
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


def _read(path: Path) -> dict[str, dict[str, str]]:
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
    return {
        str(environment): {str(key): str(value) for key, value in section.items()}
        for environment, section in entries.items()
        if isinstance(section, dict)
    }


async def cluster_id(api: Api) -> str | None:
    """The uid of the `kube-system` Namespace, or None if it cannot be read.

    None is not an error. It means this command cannot tell which cluster it
    is talking to, so it must neither trust nor add to a record -- the caller
    applies everything instead.
    """
    try:
        namespace = await Namespace.async_get(IDENTITY_NAMESPACE, api=api)
    except Exception as exc:
        # Every failure means the same thing here -- this command cannot name
        # the cluster -- and none of them is worth failing an apply over.
        _log.debug("cluster identity unreadable", namespace=IDENTITY_NAMESPACE, error=str(exc))
        return None
    metadata = namespace.raw.get("metadata")
    uid = metadata.get("uid") if isinstance(metadata, dict) else None
    return uid if isinstance(uid, str) and uid else None


async def open_cache(
    api: Api,
    *,
    environment: str,
    assume_unchanged: bool = False,
    never_record: Collection[tuple[str, str, str]] = (),
) -> ApplyCache | None:
    """This cluster's cache, or None when the cluster cannot be identified."""
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
    "cache_root",
    "cluster_id",
    "digest",
    "open_cache",
]
