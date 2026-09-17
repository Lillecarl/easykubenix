"""Refusing an apply whose store paths no node can fetch.

The case this guards is a combination nobody would write a test for. On
nixlab2 a newly-working garbage collector removed `cacheEnv` from all four
node stores while the cache push that would have replaced it had been
failing for hours. Either half alone was survivable; together they took out
the only copy of the environment the store server mounts, and the push that
fixes it goes through that same store server.

Issue Lillecarl/easykubenix#19, and #37 for why the walk is in `ekn`.

The probe is injected, so these exercise the walk itself -- the union across
substituters, the closure, and the three-way answer -- rather than a store.
The probe's own contract is checked against real stores; see
`storecheck.nanopynix_probe`'s docstring for the measurement.
"""

from __future__ import annotations

import pytest

from ekn.storecheck import (
    NoSubstitutersError,
    StorePathsUnavailableError,
    assert_fetchable,
    store_paths_in,
    walk,
)

CACHE_ENV = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-cacheEnv"
HTTPX = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-python3.14-httpx"
ABSENT = "cccccccccccccccccccccccccccccccc-never-pushed"

OBJECTS = [
    {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "pynixd-0"},
        "spec": {
            "volumes": [
                {
                    "name": "init-store",
                    "csi": {"volumeAttributes": {"storePath": f"/nix/store/{CACHE_ENV}"}},
                }
            ]
        },
    }
]


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


def probe_from(world: dict[str, dict[str, set[str]]], *, broken: set[str] = frozenset()):
    """A `Probe` over a made-up world: substituter -> path -> references."""

    async def probe(uri: str, base_name: str) -> tuple[set[str] | None, str | None]:
        if uri in broken:
            return None, f"{uri}: pretend this cache is down"
        references = world.get(uri, {}).get(base_name)
        return (references, None) if references is not None else (None, None)

    return probe


class TestWhatItReads:
    def test_it_finds_a_store_path_inside_a_csi_volume(self) -> None:
        assert store_paths_in(OBJECTS) == {CACHE_ENV}

    def test_a_path_without_the_store_prefix_is_not_one(self) -> None:
        assert store_paths_in([{"data": {"note": f"{CACHE_ENV} on its own is not a path"}}]) == set()


class TestTheUnionAcrossSubstituters:
    async def test_a_path_on_the_second_substituter_counts_as_present(self) -> None:
        """`cacheEnv` is on nixkube.cachix.org and its member
        `python3.14-httpx` is only on cache.nixos.org. Either alone reports
        a false failure; this is the case that made the union necessary."""
        world = {
            "https://nixkube.cachix.org": {CACHE_ENV: {HTTPX}},
            "https://cache.nixos.org": {HTTPX: set()},
        }

        verdict = await walk({CACHE_ENV}, list(world), probe_from(world))

        assert verdict.missing == frozenset()
        assert verdict.reachable == frozenset({CACHE_ENV, HTTPX})

    async def test_one_substituter_alone_reports_the_member_missing(self) -> None:
        world = {"https://nixkube.cachix.org": {CACHE_ENV: {HTTPX}}}

        verdict = await walk({CACHE_ENV}, list(world), probe_from(world))

        assert verdict.missing == frozenset({HTTPX})


class TestTheClosure:
    async def test_a_present_top_path_with_an_absent_member_still_fails(self) -> None:
        """Issue #8. Checking only the named paths passes this."""
        world = {"https://cache.nixos.org": {CACHE_ENV: {ABSENT}}}

        with pytest.raises(StorePathsUnavailableError, match="on no substituter"):
            await assert_fetchable(store_paths_in(OBJECTS), substituters=list(world), probe=probe_from(world))

    async def test_a_cycle_in_the_references_terminates(self) -> None:
        """A store path's references include itself, routinely."""
        world = {"https://cache.nixos.org": {CACHE_ENV: {CACHE_ENV, HTTPX}, HTTPX: {CACHE_ENV}}}

        verdict = await walk({CACHE_ENV}, list(world), probe_from(world))

        assert verdict.missing == frozenset()
        assert verdict.reachable == frozenset({CACHE_ENV, HTTPX})


class TestTheThreeAnswersStayThree:
    async def test_a_cache_that_cannot_answer_is_not_a_missing_path(self) -> None:
        """Reporting it as missing sends someone to push a path that is
        already there. The two need different fixes."""
        world = {"https://cache.nixos.org": {}}

        with pytest.raises(StorePathsUnavailableError, match="could not answer") as caught:
            await assert_fetchable(
                store_paths_in(OBJECTS),
                substituters=list(world),
                probe=probe_from(world, broken={"https://cache.nixos.org"}),
            )

        assert "on no substituter" not in str(caught.value)

    async def test_a_broken_cache_beside_a_working_one_is_not_a_problem(self) -> None:
        """The path was served. One substituter being down says nothing
        about it."""
        world = {
            "https://down.example": {},
            "https://cache.nixos.org": {CACHE_ENV: set()},
        }

        await assert_fetchable(
            store_paths_in(OBJECTS),
            substituters=list(world),
            probe=probe_from(world, broken={"https://down.example"}),
        )

    async def test_everything_fetchable_passes_quietly(self) -> None:
        world = {"https://cache.nixos.org": {CACHE_ENV: set()}}

        await assert_fetchable(store_paths_in(OBJECTS), substituters=list(world), probe=probe_from(world))


class TestItFailsClosed:
    async def test_paths_but_no_substituters_refuses_rather_than_passing(self) -> None:
        """`ekn.assertCached = [ ]` turns the check off at the call site, so
        reaching here with paths and no substituter is a caller's mistake.
        Asking nothing passes everything, which reads as a healthy cluster
        and is the opposite of what this guard is for."""
        with pytest.raises(NoSubstitutersError, match="no substituter was named"):
            await assert_fetchable(store_paths_in(OBJECTS), substituters=[], probe=probe_from({}))

    async def test_objects_naming_no_store_path_need_no_substituter(self) -> None:
        """Nothing to assert. An instance whose objects name no store path
        does not have to configure a cache to have the check on."""
        await assert_fetchable(store_paths_in([{"kind": "ConfigMap"}]), substituters=[], probe=probe_from({}))
