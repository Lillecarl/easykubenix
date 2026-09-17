"""`ekn deploy` accepts a cache destination's host key the first time.

An unknown host key failed the push with `failed to start SSH connection to
'<host>'`, which names the connection and not the key. `ekn deploy` cannot
prompt, so the operator answered it by hand with
`ssh -o StrictHostKeyChecking=accept-new nix@host` -- the same decision this
makes, and `ekn.cacheAcceptNewHostKeys` turns off. easykubenix issue #18.

Nix reads `NIX_SSHOPTS` and hands it to OpenSSH, so these test the value
`ekn` puts there. The push itself belongs to nanopynix.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
import structlog.testing

from ekn.eval import _pushing_ssh_opts, ssh_destination, ssh_failure_hint, ssh_opts_for_push

if TYPE_CHECKING:
    import pathlib

_ACCEPT_NEW = "-o StrictHostKeyChecking=accept-new"

#: OpenSSH's own words, and the exit code it uses for every one of them --
#: which is why `ssh_failure_hint` reads the text rather than the status.
_UNKNOWN_KEY = """#!/bin/sh
printf '%s\\n' "$*" >> "$SSH_LOG"
echo "No ED25519 host key is known for pynixd.example and you have requested strict checking." >&2
echo "Host key verification failed." >&2
exit 255
"""

_CHANGED_KEY = """#!/bin/sh
echo "@@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@@" >&2
echo "Host key verification failed." >&2
exit 255
"""

_REFUSED = """#!/bin/sh
echo "ssh: connect to host pynixd.example port 22: Connection refused" >&2
exit 255
"""

#: The host answered and the key was fine. Nothing to add about host keys.
_DENIED = """#!/bin/sh
echo "nix@pynixd.example: Permission denied (publickey)." >&2
exit 255
"""


@pytest.fixture
def fake_ssh(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    """Put a scripted `ssh` first on `PATH`, and return its argument log."""
    log = tmp_path / "ssh-args"
    monkeypatch.setenv("SSH_LOG", str(log))
    monkeypatch.setenv("PATH", str(tmp_path), prepend=os.pathsep)

    def install(script: str) -> pathlib.Path:
        ssh = tmp_path / "ssh"
        ssh.write_text(script)
        ssh.chmod(0o755)
        return log

    return install


class TestSshDestination:
    @pytest.mark.parametrize("scheme", ["ssh", "ssh-ng"])
    def test_both_ssh_schemes_are_ssh(self, scheme: str) -> None:
        split = ssh_destination(f"{scheme}://nix@pynixd.example")
        assert split is not None
        assert split.hostname == "pynixd.example"
        assert split.username == "nix"

    def test_a_port_and_a_query_stay_out_of_the_host_name(self) -> None:
        """`ssh-key=` is an ordinary part of a Nix ssh-ng URI, and a hint that
        named the host wrongly would send the reader to the wrong machine."""
        split = ssh_destination("ssh-ng://nix@pynixd.example:2222?ssh-key=/run/key")
        assert split is not None
        assert (split.hostname, split.port) == ("pynixd.example", 2222)

    @pytest.mark.parametrize(
        "uri",
        ["https://nixkube.cachix.org", "file:///tmp/store", "daemon", "s3://bucket"],
    )
    def test_everything_else_is_not_ssh(self, uri: str) -> None:
        assert ssh_destination(uri) is None


class TestSshOptsForPush:
    def test_an_ssh_destination_gets_accept_new(self) -> None:
        assert ssh_opts_for_push("ssh-ng://nix@pynixd.example", None, accept_new_host_keys=True) == _ACCEPT_NEW

    def test_it_appends_rather_than_replaces(self) -> None:
        """A deploy host that sets its own identity file or jump host keeps
        both: replacing the variable would break the connection it fixes."""
        opts = ssh_opts_for_push(
            "ssh-ng://nix@pynixd.example",
            "-i /run/secrets/deploy_key",
            accept_new_host_keys=True,
        )
        assert opts == f"-i /run/secrets/deploy_key {_ACCEPT_NEW}"

    def test_a_policy_already_chosen_wins(self) -> None:
        current = "-o StrictHostKeyChecking=yes"
        assert ssh_opts_for_push("ssh-ng://nix@pynixd.example", current, accept_new_host_keys=True) is None

    def test_the_option_turns_it_off(self) -> None:
        assert ssh_opts_for_push("ssh-ng://nix@pynixd.example", None, accept_new_host_keys=False) is None

    def test_a_cachix_destination_is_left_alone(self) -> None:
        """`ekn.cacheTo` takes a list, and an https substituter beside an
        ssh-ng one must not get ssh options it has no use for."""
        assert ssh_opts_for_push("https://nixkube.cachix.org", None, accept_new_host_keys=True) is None


class TestPushingSshOpts:
    def test_it_sets_the_variable_and_removes_it_again(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NIX_SSHOPTS", raising=False)

        with _pushing_ssh_opts("ssh-ng://nix@pynixd.example", accept_new_host_keys=True):
            assert os.environ["NIX_SSHOPTS"] == _ACCEPT_NEW

        assert "NIX_SSHOPTS" not in os.environ

    def test_it_restores_what_was_there(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NIX_SSHOPTS", "-i /run/secrets/deploy_key")

        with _pushing_ssh_opts("ssh-ng://nix@pynixd.example", accept_new_host_keys=True):
            assert os.environ["NIX_SSHOPTS"] == f"-i /run/secrets/deploy_key {_ACCEPT_NEW}"

        assert os.environ["NIX_SSHOPTS"] == "-i /run/secrets/deploy_key"

    def test_a_failed_push_still_restores(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NIX_SSHOPTS", raising=False)

        with pytest.raises(TimeoutError), _pushing_ssh_opts("ssh-ng://nix@x", accept_new_host_keys=True):
            raise TimeoutError

        assert "NIX_SSHOPTS" not in os.environ

    def test_a_non_ssh_destination_touches_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NIX_SSHOPTS", raising=False)

        with _pushing_ssh_opts("https://nixkube.cachix.org", accept_new_host_keys=True):
            assert "NIX_SSHOPTS" not in os.environ


class TestSshFailureHint:
    """Nix says `failed to start SSH connection to '<host>'` for an unknown
    key, a refused connection and a rejected login alike. These are the three
    apart."""

    async def test_an_unknown_key_names_the_command_that_accepts_it(self, fake_ssh) -> None:
        fake_ssh(_UNKNOWN_KEY)

        hint = await ssh_failure_hint("ssh-ng://nix@pynixd.example")

        assert hint is not None
        assert "host key is not trusted" in hint
        assert "-o StrictHostKeyChecking=accept-new nix@pynixd.example" in hint
        assert "ekn.cacheAcceptNewHostKeys" in hint

    async def test_a_changed_key_is_not_the_same_advice(self, fake_ssh) -> None:
        """`accept-new` refuses a key that changed, so telling the reader to
        run it would send them in a circle."""
        fake_ssh(_CHANGED_KEY)

        hint = await ssh_failure_hint("ssh-ng://nix@pynixd.example")

        assert hint is not None
        assert "changed" in hint
        assert "ssh-keygen -R pynixd.example" in hint
        assert "accept-new" not in hint

    async def test_an_unreachable_host_says_the_key_is_not_it(self, fake_ssh) -> None:
        fake_ssh(_REFUSED)

        hint = await ssh_failure_hint("ssh-ng://nix@pynixd.example")

        assert hint is not None
        assert "host key is not the problem" in hint
        assert "Connection refused" in hint

    async def test_a_host_that_answered_adds_nothing(self, fake_ssh) -> None:
        fake_ssh(_DENIED)

        assert await ssh_failure_hint("ssh-ng://nix@pynixd.example") is None

    async def test_a_non_ssh_destination_starts_no_probe(self, fake_ssh) -> None:
        log = fake_ssh(_UNKNOWN_KEY)

        assert await ssh_failure_hint("https://nixkube.cachix.org") is None
        assert not log.exists(), "a cachix URL must not make this run ssh"

    async def test_the_probe_carries_the_port_and_never_writes_a_key(self, fake_ssh) -> None:
        """A diagnosis that changed what the machine trusts would answer the
        question by removing it."""
        log = fake_ssh(_UNKNOWN_KEY)

        await ssh_failure_hint("ssh-ng://nix@pynixd.example:2222?ssh-key=/run/key")

        argv = log.read_text()
        assert "-p 2222" in argv
        assert "StrictHostKeyChecking=yes" in argv
        assert "accept-new" not in argv
        assert "BatchMode=yes" in argv

    async def test_no_ssh_on_path_is_no_hint_and_no_crash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The push already failed. A missing `ssh` must not replace its
        error with a `FileNotFoundError` from the diagnosis."""
        monkeypatch.setenv("PATH", "")

        assert await ssh_failure_hint("ssh-ng://nix@pynixd.example") is None


class TestTheHintReachesTheReport:
    """A diagnosis nobody prints is no diagnosis. `_report_nix_error` exits,
    so the hint has to be gathered before it."""

    @staticmethod
    def _stub(monkeypatch: pytest.MonkeyPatch, failure: BaseException) -> None:
        async def push(*_args: object, **_kwargs: object) -> None:
            raise failure

        async def hint(*_args: object, **_kwargs: object) -> str:
            return "THE HOST KEY HINT"

        monkeypatch.setattr("ekn.cli.push_closure_to_store", push)
        monkeypatch.setattr("ekn.cli.ssh_failure_hint", hint)

    async def test_a_failed_push_prints_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nanopynix import NixError

        from ekn.cli import _push_one_cache

        self._stub(monkeypatch, NixError("SysError", "error: failed to start SSH connection to 'pynixd'"))

        with structlog.testing.capture_logs() as logs, pytest.raises(SystemExit) as exc:
            await _push_one_cache(
                "/nix/store/aaa-cache",
                "ssh-ng://nix@pynixd",
                timeout_sec=None,
                allow_failure=False,
            )

        assert exc.value.code == 1
        assert any("THE HOST KEY HINT" in str(entry.get("event", "")) for entry in logs)

    async def test_a_timed_out_push_prints_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A host that drops packets rather than refusing them ends here, and
        that is the case where "is it the key or the route" is hardest."""
        from ekn.cli import _push_one_cache

        self._stub(monkeypatch, TimeoutError())

        with structlog.testing.capture_logs() as logs, pytest.raises(SystemExit):
            await _push_one_cache(
                "/nix/store/aaa-cache",
                "ssh-ng://nix@pynixd",
                timeout_sec=5,
                allow_failure=False,
            )

        assert any("THE HOST KEY HINT" in str(entry.get("event", "")) for entry in logs)

    async def test_allow_failure_keeps_it_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nanopynix import NixError

        from ekn.cli import _push_one_cache

        self._stub(monkeypatch, NixError("SysError", "error: failed to start SSH connection to 'pynixd'"))

        with structlog.testing.capture_logs() as logs:
            await _push_one_cache(
                "/nix/store/aaa-cache",
                "ssh-ng://nix@pynixd",
                timeout_sec=None,
                allow_failure=True,
            )

        assert any("THE HOST KEY HINT" in str(entry.get("event", "")) for entry in logs)
