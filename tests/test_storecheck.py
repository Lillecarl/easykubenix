"""Refusing an apply whose store paths no node can fetch.

The case this guards is a combination nobody would write a test for. On
nixlab2 a newly-working garbage collector removed `cacheEnv` from all four
node stores while the cache push that would have replaced it had been
failing for hours. Either half alone was survivable; together they took out
the only copy of the environment the store server mounts, and the push that
fixes it goes through that same store server.

Issue Lillecarl/easykubenix#19.
"""

from __future__ import annotations

import json
import stat
from typing import TYPE_CHECKING

import pytest
from anyio import Path as AsyncPath

from ekn.storecheck import StorePathsUnavailableError, assert_fetchable

if TYPE_CHECKING:
    from pathlib import Path

OBJECTS = [
    {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "pynixd-0"},
        "spec": {
            "volumes": [{"name": "init-store", "csi": {"volumeAttributes": {"storePath": "/nix/store/x-cacheEnv"}}}]
        },
    }
]


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


def _checker(tmp_path: Path, code: int, stderr: str = "", stdout: str = "ok") -> str:
    """A stand-in for `assert-cached` that exits how a test needs.

    A real script rather than a mock, because what is being tested is how
    `ekn` reads another program's exit code, and a mock of that tests only
    the mock.
    """
    script = tmp_path / f"checker-{code}"
    script.write_text(
        f'#!/bin/sh\nprintf "%s\\n" {json.dumps(stdout)}\nprintf "%s\\n" {json.dumps(stderr)} >&2\nexit {code}\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(script)


class TestItRefusesRatherThanWarns:
    async def test_missing_paths_refuse_the_apply(self, tmp_path: Path) -> None:
        checker = _checker(tmp_path, 1, stderr="FAIL: 1 of 197 paths are on no substituter")

        with pytest.raises(StorePathsUnavailableError, match="on no substituter"):
            await assert_fetchable(OBJECTS, checker=checker, scratch=AsyncPath(tmp_path))

    async def test_a_broken_check_also_refuses_but_says_so(self, tmp_path: Path) -> None:
        """Exit 3 is "a substituter would not answer", which is a different
        problem from a missing path and needs a different fix. Reporting it
        as missing sends someone to push a path that is already there."""
        checker = _checker(tmp_path, 3, stderr="UNKNOWN: 2 path(s) got neither a 200 nor a 404")

        with pytest.raises(StorePathsUnavailableError, match="could not answer") as caught:
            await assert_fetchable(OBJECTS, checker=checker, scratch=AsyncPath(tmp_path))

        assert "on no substituter" not in str(caught.value)

    async def test_an_unexpected_exit_code_still_refuses(self, tmp_path: Path) -> None:
        """Fails closed. An exit code this does not recognise is far more
        likely to be a broken checker than a healthy cluster."""
        checker = _checker(tmp_path, 42, stderr="something else entirely")

        with pytest.raises(StorePathsUnavailableError, match="exited 42"):
            await assert_fetchable(OBJECTS, checker=checker, scratch=AsyncPath(tmp_path))

    async def test_everything_fetchable_passes_quietly(self, tmp_path: Path) -> None:
        checker = _checker(tmp_path, 0, stdout="OK: 197 paths, whole closure, all fetchable.")

        await assert_fetchable(OBJECTS, checker=checker, scratch=AsyncPath(tmp_path))


class TestWhatItHandsTheChecker:
    async def test_the_objects_this_apply_sends(self, tmp_path: Path) -> None:
        """Not the rendered manifest derivation. A `--target` slice is not
        what `ekn.cachePackage` covers, and a seed rewrites an object's data
        before it is sent -- so the file has to be built from what is
        actually going out."""
        captured = tmp_path / "captured.json"
        script = tmp_path / "capture"
        script.write_text(f'#!/bin/sh\ncat "$1" > {captured}\nexit 0\n')
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

        await assert_fetchable(OBJECTS, checker=str(script), scratch=AsyncPath(tmp_path))

        handed = json.loads(captured.read_text())
        assert handed == OBJECTS
        assert "/nix/store/x-cacheEnv" in captured.read_text(), "the store path must survive into the file"

    async def test_the_file_lands_in_the_scratch_directory(self, tmp_path: Path) -> None:
        """So the caller's `TemporaryDirectory` removes it. A manifest left
        in the working tree is one somebody commits."""
        checker = _checker(tmp_path, 0)
        scratch = AsyncPath(tmp_path)

        await assert_fetchable(OBJECTS, checker=checker, scratch=scratch)

        assert await (scratch / "apply-manifest.json").is_file()
