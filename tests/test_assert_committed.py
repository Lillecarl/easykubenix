"""`ekn deploy` refuses to move a branch naming an unfetchable store path.

Issue #19. CI proves the render *it* built is fetchable; nothing proved the
render about to be deployed is, and the two differ the moment a pin moves.
Reached by hand on nixlab2 one minute before a deploy that would have taken
pynixd down -- a nixkube bump moved one leg of the cache package, and the
decision to stop rested on one person running one `curl` against a hostname
they typed from memory.

`storecheck.assert_fetchable` does the walk, and `tests/test_storecheck.py`
covers it. These cover the wiring on the commit path: which paths get asked
about, which substituters get used, and what happens when the answer is no.
"""

from __future__ import annotations

from typing import Any

import pytest

from ekn import storecheck
from ekn.cli import _assert_committed_fetchable

_FILES = [
    ("app/default/ConfigMap/c.yaml", "data:\n  env: /nix/store/" + "a" * 32 + "-nodeEnv\n"),
    ("app/kustomization.yaml", "resources:\n  - default/ConfigMap/c.yaml\n"),
]

_NODE_ENV = "/nix/store/" + "a" * 32 + "-nodeEnv"


class _Recorder:
    """Stands in for `assert_fetchable`, and records how it was called."""

    def __init__(self, raises: Exception | None = None) -> None:
        self.calls: list[tuple[set[str], list[str]]] = []
        self.raises = raises

    async def __call__(self, roots: set[str], *, substituters: Any, **_kwargs: Any) -> None:
        self.calls.append((roots, list(substituters)))
        if self.raises is not None:
            raise self.raises


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch):
    def install(raises: Exception | None = None) -> _Recorder:
        rec = _Recorder(raises)
        monkeypatch.setattr("ekn.cli.storecheck.assert_fetchable", rec)
        return rec

    return install


class TestWhatItAsksAbout:
    async def test_the_paths_come_from_the_committed_text(self, recorder) -> None:
        """The committed bytes, not the object tree. That is what a GitOps
        engine reads from the branch, and it covers a `tf` unit's rendered
        configuration, which is committed beside the manifests and is not a
        Kubernetes object at all."""
        rec = recorder()

        await _assert_committed_fetchable(_FILES, ["https://nixkube.cachix.org"])

        assert len(rec.calls) == 1
        roots, substituters = rec.calls[0]
        assert roots == {_NODE_ENV.removeprefix("/nix/store/")}
        assert substituters == ["https://nixkube.cachix.org"]

    async def test_a_tf_units_rendered_config_is_covered(self, recorder) -> None:
        rec = recorder()
        tf_path = "/nix/store/" + "b" * 32 + "-provider"
        files = [("infra/config.tf.json", f'{{"x": "{tf_path}"}}\n')]

        await _assert_committed_fetchable(files, ["https://cache.example"])

        assert rec.calls[0][0] == {tf_path.removeprefix("/nix/store/")}


class TestWhenItDoesNothing:
    async def test_no_substituters_is_off(self, recorder) -> None:
        """Empty means off, which is right for an instance with no
        CSI-backed store: it has nothing to assert and should not pay for
        it."""
        rec = recorder()

        await _assert_committed_fetchable(_FILES, [])

        assert rec.calls == []

    async def test_no_store_paths_asks_nothing(self, recorder) -> None:
        """A `tf`-only instance, or a render that names no store path, has
        nothing to ask about -- and asking with an empty set would trip
        `assert_fetchable`'s own "no substituters would pass everything"
        guard for the wrong reason."""
        rec = recorder()

        await _assert_committed_fetchable(
            [("app/default/ConfigMap/c.yaml", "data:\n  key: value\n")],
            ["https://nixkube.cachix.org"],
        )

        assert rec.calls == []


class TestWhenTheAnswerIsNo:
    async def test_an_unfetchable_path_stops_the_deploy(self, recorder) -> None:
        """Refuses rather than warns. A path on no substituter is a mount
        that fails on a node, minutes later and well away from the deploy
        that caused it."""
        recorder(storecheck.StorePathsUnavailableError("nothing can serve /nix/store/...-nodeEnv"))

        with pytest.raises(SystemExit) as exc:
            await _assert_committed_fetchable(_FILES, ["https://nixkube.cachix.org"])

        assert "nodeEnv" in str(exc.value)

    async def test_a_substituter_that_cannot_answer_also_stops_it(self, recorder) -> None:
        """A cache that is down and a path that was never pushed need
        different fixes, and `assert_fetchable` keeps them apart. Neither is
        a reason to move the branch."""
        recorder(storecheck.NoSubstitutersError("no substituter was named"))

        with pytest.raises(SystemExit):
            await _assert_committed_fetchable(_FILES, ["https://nixkube.cachix.org"])
