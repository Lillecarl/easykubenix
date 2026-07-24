from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock

from kr8s._api import Api
from kr8s.asyncio.objects import APIObject, get_class

from ekn.apply import apply_and_prune

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


def _fake_configmap(name: str, namespace: str = "ns") -> APIObject:
    """A real kr8s ConfigMap instance, constructed offline (no live/mocked
    apiserver call) -- `apply_and_prune`'s prune loop does `isinstance(obj,
    APIObject)`, so a duck-typed stand-in wouldn't pass; kr8s's own
    `get_class`+constructor pattern (the same one `build_object` uses) is
    the cheapest way to get a real instance. `api=` just needs to be
    truthy with a `.namespace` attribute -- `APIObject.namespace`'s property
    getter reads `self.api.namespace` as a `dict.get` default arg
    (evaluated eagerly) even though `metadata.namespace` is always present
    here.
    """
    cls = get_class("ConfigMap", "v1")
    obj = cls(
        {"kind": "ConfigMap", "apiVersion": "v1", "metadata": {"name": name, "namespace": namespace}},
        api=cast("Api", SimpleNamespace(namespace=None)),
    )
    obj.delete = AsyncMock()  # pyright: ignore[reportAttributeAccessIssue] -- instance-level override for the test
    return obj


class _FakeApi(Api):
    """Minimal stand-in for kr8s's Api -- only implements the one bit of
    surface `apply_and_prune`'s prune loop touches (`async_get`), since
    `objects=[]` in these tests means `apply_and_prune`'s barrier/apply loop
    (which needs the real `build_object`/`ssa_apply` machinery) never runs
    at all -- isolating `prune_kinds`' additive-scan behavior from that."""

    def __init__(self, objects_by_kind: dict[str, list[APIObject]]) -> None:
        self.objects_by_kind = objects_by_kind
        self.queried_kinds: list[str] = []

    # Mirrors kr8s.asyncio.Api.async_get exactly. The double subclasses Api so
    # that beartype's isinstance check on `prune_kinds`' annotated parameter
    # accepts it, and a subclass has to honour the base signature -- only
    # `kind` is read here, but a narrower override would be a different
    # method wearing the same name.
    async def async_get(
        self,
        kind: str | type,
        *names: str,
        namespace: str | None = None,
        label_selector: str | dict[str, str] | None = None,
        field_selector: str | dict[str, str] | None = None,
        as_object: type[APIObject] | None = None,
        allow_unknown_type: bool = True,
        raw: bool = False,
        **kwargs: object,
    ) -> AsyncGenerator[APIObject | dict[Any, Any]]:
        if not isinstance(kind, str):
            raise TypeError(f"this double is only exercised with string kinds, got {kind!r}")
        self.queried_kinds.append(kind)
        for obj in self.objects_by_kind.get(kind, []):
            yield obj


async def test_prune_kinds_is_scanned_even_when_absent_from_the_current_apply() -> None:
    stale = _fake_configmap("stale-one")
    api = _FakeApi(objects_by_kind={"ConfigMap": [stale]})

    await apply_and_prune(
        [],
        api=api,
        discriminator="disc",
        prune=True,
        prune_kinds={"ConfigMap"},
    )

    assert api.queried_kinds == ["ConfigMap"]
    cast("AsyncMock", stale.delete).assert_awaited_once()


async def test_prune_kinds_none_preserves_current_behavior_of_scanning_nothing_extra() -> None:
    stale = _fake_configmap("stale-one")
    api = _FakeApi(objects_by_kind={"ConfigMap": [stale]})

    await apply_and_prune(
        [],
        api=api,
        discriminator="disc",
        prune=True,
        prune_kinds=None,
    )

    assert api.queried_kinds == []
    cast("AsyncMock", stale.delete).assert_not_awaited()
