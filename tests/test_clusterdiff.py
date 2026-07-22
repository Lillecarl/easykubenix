from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import httpx
import kr8s
import pytest

from ekn.clusterdiff import _normalize, cluster_diff


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


class _FakeResponse:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def json(self) -> dict[str, Any]:
        return self._data


class FakeApi:
    """Stands in for kr8s's real Api: answers GET (live lookup) and PATCH
    (dry-run apply) calls from a canned script instead of a live cluster.
    """

    namespace = "default"

    def __init__(self, script: dict[tuple[str, str], Any]) -> None:
        self._script = script
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    @asynccontextmanager
    async def call_api(
        self,
        method: str,
        *,
        version: str | None = None,
        url: str | None = None,
        namespace: str | None = None,
        content: str | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ):
        self.calls.append((method, url or "", params))
        result = self._script[(method, url or "")]
        if isinstance(result, Exception):
            raise result
        yield _FakeResponse(result)

    async def async_lookup_kind(self, lookup: str) -> tuple[str, str, bool]:
        raise NotImplementedError("only builtin kinds are used in these tests")


def _not_found() -> kr8s.ServerError:
    response = httpx.Response(404, request=httpx.Request("GET", "http://x/"))
    return kr8s.ServerError("not found", response=response)


def _config_map(name: str, data: dict[str, str]) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "namespace": "default"},
        "data": data,
    }


class TestNormalize:
    def test_strips_noisy_metadata_and_status(self) -> None:
        obj = {
            "kind": "ConfigMap",
            "metadata": {
                "name": "cfg",
                "resourceVersion": "123",
                "managedFields": [{"manager": "kubectl"}],
                "uid": "abc-def",
                "generation": 3,
                "creationTimestamp": "2026-01-01T00:00:00Z",
                "annotations": {
                    "kubectl.kubernetes.io/last-applied-configuration": "{}",
                    "keep-me": "yes",
                },
            },
            "data": {"key": "value"},
            "status": {"phase": "Active"},
        }

        result = _normalize(obj)

        assert "status" not in result
        assert result["metadata"] == {"name": "cfg", "annotations": {"keep-me": "yes"}}
        assert result["data"] == {"key": "value"}


class TestClusterDiff:
    async def test_no_diff_when_live_matches_predicted(self) -> None:
        spec = _config_map("cfg", {"key": "value"})
        api = FakeApi(
            {
                ("GET", "configmaps/cfg"): spec,
                ("PATCH", "configmaps/cfg"): spec,
            }
        )

        result = await cluster_diff([spec], api=api)  # type: ignore[arg-type]

        assert result == ""

    async def test_diff_for_new_object(self) -> None:
        spec = _config_map("brand-new", {"key": "value"})
        api = FakeApi(
            {
                ("GET", "configmaps/brand-new"): _not_found(),
                ("PATCH", "configmaps/brand-new"): spec,
            }
        )

        result = await cluster_diff([spec], api=api)  # type: ignore[arg-type]

        assert "live/default/ConfigMap/brand-new" in result
        assert "predicted/default/ConfigMap/brand-new" in result
        assert "+data:" in result
        assert "+  key: value" in result

    async def test_diff_shows_changed_field(self) -> None:
        spec = _config_map("cfg", {"key": "updated"})
        live = _config_map("cfg", {"key": "old"})
        api = FakeApi(
            {
                ("GET", "configmaps/cfg"): live,
                ("PATCH", "configmaps/cfg"): spec,
            }
        )

        result = await cluster_diff([spec], api=api)  # type: ignore[arg-type]

        assert "-  key: old" in result
        assert "+  key: updated" in result
