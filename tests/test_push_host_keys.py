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

import pytest

from ekn.eval import _pushing_ssh_opts, ssh_destination, ssh_opts_for_push

_ACCEPT_NEW = "-o StrictHostKeyChecking=accept-new"


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
