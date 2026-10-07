"""Where `schemacheck.Catalog` gets the built-in kinds' schemas.

- `load_spec_dir`: `api/openapi-spec/v3` from the Kubernetes source tree. Pinned
  with `kubernetes.package`, and needs no network, so it is what the Nix gate
  uses.
- `load_server`: the API server's own `/openapi/v3`, through kr8s. It also
  carries the schema of every CRD the cluster holds.
- `load_yannh`: yannh/kubernetes-json-schema, for when no server answers.
- `load_installed`: the API server's own ValidatingAdmissionPolicies, their
  bindings and its Namespaces, which `schemapolicy` evaluates beside the
  render's.

The last two are cached under `cache_root() / <cluster>`, where `<cluster>` is
the `kube-system` uid (`ekn.clusterUid`).
"""

from __future__ import annotations

import json
import os
import re
from typing import TYPE_CHECKING, Any, cast

import anyio
import httpx
import structlog
from anyio import Path
from kr8s import ServerError
from kr8s.asyncio.objects import APIObject, Namespace, new_class

from ekn.schemacheck import Origin

if TYPE_CHECKING:
    from collections.abc import Iterable

    import kr8s.asyncio

    from ekn.schemacheck import Catalog, GroupVersionKind

_log = structlog.get_logger()

#: The discovery paths that name one group-version: `api/v1`, `apis/<group>/<version>`.
_GROUP_VERSION_PATH = re.compile(r"^(api/v1|apis/[^/]+/[^/]+)$")

#: The spec dir's file names for the same: `api__v1_openapi.json`,
#: `apis__apps__v1_openapi.json`. The group-only `apis__apps_openapi.json` holds
#: no kinds.
_SPEC_FILE = re.compile(r"^(api__v1|apis__[^_]+(?:_[^_]+)*__[^_]+)_openapi\.json$")

YANNH_BASE = "https://raw.githubusercontent.com/yannh/kubernetes-json-schema/master"

#: The API server serves every group-version at once; this bounds how many.
_FETCH_CONCURRENCY = 8

#: What `load_installed` lists, as classes: a kind name goes through kr8s'
#: discovery lookup, which is not needed here. A policy's namespaceSelector
#: and `namespaceObject` read a Namespace the render may not hold.
_INSTALLED: tuple[type[APIObject], ...] = (
    new_class(
        "ValidatingAdmissionPolicy.admissionregistration.k8s.io/v1",
        namespaced=False,
        plural="validatingadmissionpolicies",
    ),
    new_class(
        "ValidatingAdmissionPolicyBinding.admissionregistration.k8s.io/v1",
        namespaced=False,
        plural="validatingadmissionpolicybindings",
    ),
    Namespace,
)


async def cache_root() -> Path:
    cache = os.environ.get("XDG_CACHE_HOME")
    base = Path(cache) if cache else await Path.home() / ".cache"
    return base / "ekn" / "schemas"


async def load_spec_dir(catalog: Catalog, spec_dir: str) -> int:
    """Add every group-version file in *spec_dir*. Returns how many it read."""
    count = 0
    async for entry in Path(spec_dir).iterdir():
        if _SPEC_FILE.match(entry.name):
            catalog.add_openapi_v3(json.loads(await entry.read_text()), Origin.SPEC)
            count += 1
    if count == 0:
        _log.error("no OpenAPI v3 group-version files in the spec directory", spec_dir=spec_dir)
        raise SystemExit(1)
    return count


async def _write_atomic(path: Path, text: str) -> None:
    partial = path.with_name(path.name + ".partial")
    await partial.write_text(text)
    await partial.rename(path)


async def _server_document(api: kr8s.asyncio.Api, path: str, url: str, cache_dir: Path) -> dict[str, Any]:
    """One group-version document, from *cache_dir* when its hash is there.

    The server names each document with a content hash, so a cached file is
    right for as long as the hash is the one discovery reports.
    """
    content_hash = httpx.URL(url).params.get("hash", "")
    cached = cache_dir / f"{path.replace('/', '__')}-{content_hash}.json"
    if content_hash and await cached.exists():
        return cast("dict[str, Any]", json.loads(await cached.read_text()))
    params = {"hash": content_hash} if content_hash else None
    async with api.call_api("GET", base="/openapi", version="", url=f"v3/{path}", params=params) as response:
        text = response.text
    if content_hash:
        async for stale in cache_dir.glob(f"{path.replace('/', '__')}-*.json"):
            await stale.unlink(missing_ok=True)
        await _write_atomic(cached, text)
    return cast("dict[str, Any]", json.loads(text))


async def load_server(catalog: Catalog, api: kr8s.asyncio.Api, cluster: str) -> int:
    """Add every group-version the API server describes. Returns how many."""
    # `version=""` and the path in `url`: kr8s joins with "/", and the
    # discovery root answers 404 at `/openapi/v3/`.
    async with api.call_api("GET", base="/openapi", version="", url="v3") as response:
        discovery = cast("dict[str, Any]", response.json())
    paths = {
        path: str(cast("dict[str, Any]", ref)["serverRelativeURL"])
        for path, ref in cast("dict[str, Any]", discovery.get("paths") or {}).items()
        if _GROUP_VERSION_PATH.match(path)
    }
    cache_dir = await cache_root() / cluster / "openapi-v3"
    await cache_dir.mkdir(parents=True, exist_ok=True)

    documents: dict[str, dict[str, Any]] = {}
    limiter = anyio.CapacityLimiter(_FETCH_CONCURRENCY)

    async def fetch(path: str, url: str) -> None:
        async with limiter:
            documents[path] = await _server_document(api, path, url, cache_dir)

    async with anyio.create_task_group() as tg:
        for path, url in paths.items():
            tg.start_soon(fetch, path, url)
    for path in sorted(documents):
        catalog.add_openapi_v3(documents[path], Origin.SERVER)
    return len(documents)


async def load_installed(catalog: Catalog, api: kr8s.asyncio.Api) -> int:
    """Add the cluster's admission policies, bindings and Namespaces to
    `catalog.installed`. Returns how many objects."""
    for kind in _INSTALLED:
        try:
            catalog.installed.extend(
                [
                    obj.raw
                    async for obj in api.async_get(kind)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType] -- kr8s Api.async_get's yield type is unannotated upstream
                    if isinstance(obj, APIObject)
                ]
            )
        except ServerError as exc:
            # admissionregistration.k8s.io/v1 serves policies from Kubernetes 1.30.
            if exc.response is None or exc.response.status_code != httpx.codes.NOT_FOUND:
                raise
            _log.info("the cluster serves no such kind", kind=kind.kind, version=kind.version)
    return len(catalog.installed)


def yannh_name(gvk: GroupVersionKind) -> str:
    """yannh's file name for a kind: the group's first label, core has none."""
    group = gvk.group.split(".")[0]
    middle = f"-{group}" if group else ""
    return f"{gvk.kind.lower()}{middle}-{gvk.version}".lower()


async def load_yannh(
    catalog: Catalog,
    gvks: Iterable[GroupVersionKind],
    kubernetes_version: str,
    cluster: str,
) -> int:
    """Add yannh's strict schema for each of *gvks* it has. Returns how many.

    Asks only for the kinds the manifest uses: the tree holds one file per
    kind per release. A 404 is a kind yannh does not carry, which leaves it
    to a rendered CRD or to the report's unknown list.
    """
    release = f"v{kubernetes_version.removeprefix('v')}-standalone-strict"
    cache_dir = await cache_root() / cluster / "yannh" / release
    await cache_dir.mkdir(parents=True, exist_ok=True)
    found = 0
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        for gvk in sorted(set(gvks)):
            if gvk in catalog:
                continue
            name = yannh_name(gvk)
            cached = cache_dir / f"{name}.json"
            if await cached.exists():
                text = await cached.read_text()
            else:
                response = await client.get(f"{YANNH_BASE}/{release}/{name}.json")
                if response.status_code == httpx.codes.NOT_FOUND:
                    continue
                response.raise_for_status()
                text = response.text
                await _write_atomic(cached, text)
            catalog.add_schema(gvk, cast("dict[str, Any]", json.loads(text)), Origin.YANNH)
            found += 1
    return found
