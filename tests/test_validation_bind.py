from __future__ import annotations

import pytest

from ekn.validation import ETCD_HOST, bind_addresses


class TestBindAddresses:
    """kube-apiserver refuses to start when its advertise address is of a
    different family from the first service CIDR, so the harness's loopback is
    derived from the range rather than chosen."""

    @pytest.mark.parametrize(
        ("subnet", "expected"),
        [
            ("10.96.0.0/16", ("127.0.0.1", "127.0.0.1")),
            ("fd00:96::/108", ("::1", "[::1]")),
            # IPv4 first, matching the order validation.nix builds the list in,
            # so dual-stack keeps IPv4 and nothing moves for anyone not
            # single-stack v6.
            ("10.96.0.0/16,fd00:96::/108", ("127.0.0.1", "127.0.0.1")),
            ("  fd00:96::/108 , 10.96.0.0/16 ", ("::1", "[::1]")),
        ],
    )
    def test_the_family_follows_the_first_cidr(self, subnet: str, expected: tuple[str, str]) -> None:
        assert bind_addresses(subnet) == expected

    def test_the_two_forms_are_not_interchangeable(self) -> None:
        """Flags take the bare address; a host:port join needs an IPv6 literal
        bracketed, or everything after the first colon reads as the port."""
        bare, host = bind_addresses("fd00:96::/108")

        assert f"--bind-address={bare}" == "--bind-address=::1"
        assert f"https://{host}:6443" == "https://[::1]:6443"

    def test_etcd_stays_on_ipv4_loopback(self) -> None:
        """Its family has no bearing on the constraint, and validation.nix
        hardcodes the same address -- these two harnesses agreeing is worth
        more than either being clever."""
        assert ETCD_HOST == "127.0.0.1"
