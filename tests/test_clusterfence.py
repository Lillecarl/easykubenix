# SPDX-License-Identifier: MIT
"""`clusterfence.require`, which is what stops an apply reaching the wrong
cluster.

Control flow, not Kubernetes: the one GET is stood in for. What each test
asserts is which message the operator gets, because the message is the whole
product here -- a refusal that does not say what to paste sends someone
looking for a kubeconfig problem instead.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from structlog.testing import capture_logs
from test_apply import FakeApi

from ekn import clusterfence
from ekn.clusterfence import OVERRIDE_FLAG, require

#: Shaped like a real one, because the refusal prints it as the line to paste
#: and `ekn.clusterUid` refuses anything else at evaluation.
LIVE = "3b2a1f0e-9d8c-4b7a-8e6f-5d4c3b2a1f0e"
OTHER = "00000000-1111-2222-3333-444444444444"

#: `ekn.environment`. On every message, because a bare uid says only that the
#: fence fired -- not which of two configurations the operator was pointed at.
ENV = "nixlab2"


@pytest.fixture
def cluster(monkeypatch: pytest.MonkeyPatch):
    """Stand in for the `kube-system` GET. `uid=None` raises, which is what a
    cluster that will not answer looks like."""

    def use(uid: str | None, *, error: str = "forbidden") -> None:
        class _Namespace:
            def __init__(self, data: dict[str, Any]) -> None:
                self.raw = data

            @classmethod
            async def async_get(cls, _name: str, *, api: Any) -> _Namespace:
                _ = api
                if uid is None:
                    raise RuntimeError(error)
                return cls({"metadata": {"uid": uid}})

        monkeypatch.setattr("ekn.fastcache.Namespace", _Namespace)

    return use


class TestDeclared:
    async def test_a_match_returns_the_uid(self, cluster) -> None:
        """Returned rather than just accepted: `open_cache` takes it, so the
        apply asks for the Namespace once and not twice."""
        cluster(LIVE)

        assert await require(FakeApi(), LIVE, environment=ENV) == LIVE  # type: ignore[arg-type]

    async def test_a_mismatch_names_both(self, cluster) -> None:
        cluster(LIVE)

        with pytest.raises(SystemExit) as caught:
            await require(FakeApi(), OTHER, environment=ENV)  # type: ignore[arg-type]

        message = str(caught.value)
        assert OTHER in message
        assert LIVE in message

    async def test_the_flag_does_not_override_a_mismatch(self, cluster) -> None:
        """The negative control for the rule that matters. Without this the
        flag quietly becomes "skip the check" on the first refactor, and the
        fence is then off for everyone who ever typed it once.
        """
        cluster(LIVE)

        with pytest.raises(SystemExit) as caught:
            await require(FakeApi(), OTHER, environment=ENV, override=True)  # type: ignore[arg-type]

        assert OVERRIDE_FLAG in str(caught.value), "the message must say the flag does not apply here"

    async def test_a_rebuilt_cluster_is_explained(self, cluster) -> None:
        """A new uid is the ordinary way a mismatch happens to someone who did
        nothing wrong, so the message says it rather than leaving them to
        conclude the kubeconfig is broken."""
        cluster(LIVE)

        with pytest.raises(SystemExit) as caught:
            await require(FakeApi(), OTHER, environment=ENV)  # type: ignore[arg-type]

        assert "rebuilt" in str(caught.value)


class TestNotDeclared:
    async def test_it_refuses_and_prints_the_line_to_paste(self, cluster) -> None:
        cluster(LIVE)

        with pytest.raises(SystemExit) as caught:
            await require(FakeApi(), None, environment=ENV)  # type: ignore[arg-type]

        message = str(caught.value)
        assert f'ekn.clusterUid = "{LIVE}";' in message, "adopting must be a copy-paste, not a lookup"
        assert OVERRIDE_FLAG in message

    async def test_the_flag_runs_anyway_and_says_so(self, cluster) -> None:
        cluster(LIVE)

        with capture_logs() as logs:
            assert await require(FakeApi(), None, environment=ENV, override=True) == LIVE  # type: ignore[arg-type]

        warnings = [entry for entry in logs if entry["log_level"] == "warning"]
        assert len(warnings) == 1
        assert warnings[0]["cluster_uid"] == LIVE


class TestUnreadable:
    async def test_it_refuses_rather_than_passing(self, cluster) -> None:
        """`fastcache.cluster_id` treats this as benign, because it only turns
        a cache off. Here the same silence would be a way past the fence: any
        permission error on `kube-system` would apply to anything."""
        cluster(None, error='namespaces "kube-system" is forbidden')

        with pytest.raises(SystemExit) as caught:
            await require(FakeApi(), LIVE, environment=ENV)  # type: ignore[arg-type]

        assert "forbidden" in str(caught.value), "the reason it could not be read has to reach the operator"

    async def test_the_flag_does_not_help_either(self, cluster) -> None:
        cluster(None)

        with pytest.raises(SystemExit):
            await require(FakeApi(), None, environment=ENV, override=True)  # type: ignore[arg-type]

    async def test_a_namespace_with_no_uid_is_unreadable_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Not an empty string compared against a declared value, which would
        match a configuration declaring nothing useful."""

        class _Namespace:
            raw: ClassVar[dict[str, Any]] = {"metadata": {}}

            @classmethod
            async def async_get(cls, _name: str, *, api: Any) -> _Namespace:
                _ = api
                return cls()

        monkeypatch.setattr("ekn.fastcache.Namespace", _Namespace)

        with pytest.raises(SystemExit) as caught:
            await require(FakeApi(), LIVE, environment=ENV)  # type: ignore[arg-type]

        assert "no uid" in str(caught.value)


class TestTheEnvironmentIsOnEveryMessage:
    """Asked for by the operator this fence exists for: "a bare uid mismatch
    tells me the fence fired; the environment name tells me which of two
    configurations I was pointed at, which is the thing I actually got
    wrong."

    One test per refusal rather than one for the pair, because a message that
    drops it does so on its own and the others would still pass.
    """

    async def test_on_a_mismatch(self, cluster) -> None:
        cluster(LIVE)

        with pytest.raises(SystemExit) as caught:
            await require(FakeApi(), OTHER, environment=ENV)  # type: ignore[arg-type]

        assert ENV in str(caught.value)

    async def test_on_an_undeclared_cluster(self, cluster) -> None:
        cluster(LIVE)

        with pytest.raises(SystemExit) as caught:
            await require(FakeApi(), None, environment=ENV)  # type: ignore[arg-type]

        assert ENV in str(caught.value)

    async def test_on_an_unreadable_one(self, cluster) -> None:
        """The case where it carries the most: nothing else in the message
        identifies what was being applied."""
        cluster(None)

        with pytest.raises(SystemExit) as caught:
            await require(FakeApi(), LIVE, environment=ENV)  # type: ignore[arg-type]

        assert ENV in str(caught.value)

    async def test_and_on_the_override_warning(self, cluster) -> None:
        cluster(LIVE)

        with capture_logs() as logs:
            await require(FakeApi(), None, environment=ENV, override=True)  # type: ignore[arg-type]

        assert logs[0]["environment"] == ENV


def test_the_flag_name_is_what_the_cli_declares() -> None:
    """The message quotes it, and the CLI derives its own from the attribute
    name -- so the two can drift without anything failing."""
    from ekn.cli import FencedCommand

    declared = "--" + "i_dont_know_which_cluster_this_is".replace("_", "-")
    assert declared == clusterfence.OVERRIDE_FLAG
    assert "i_dont_know_which_cluster_this_is" in FencedCommand.specs
