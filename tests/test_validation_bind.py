from __future__ import annotations

import ipaddress

import pytest

from ekn.validation import ETCD_HOST, advertise_address, bind_addresses


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


class TestAdvertiseAddress:
    """kube-apiserver 1.37.0 refuses a loopback advertise address outright:
    `cannot use public IP 127.0.0.1 with endpoint reconciler`. So it is not
    the bind address, and it has to be global unicast."""

    @pytest.mark.parametrize(
        ("subnet", "expected"),
        [
            ("10.96.0.0/16", "192.0.2.10"),
            ("fd00:96::/108", "2001:db8::10"),
            ("10.96.0.0/16,fd00:96::/108", "192.0.2.10"),
            ("  fd00:96::/108 , 10.96.0.0/16 ", "2001:db8::10"),
        ],
    )
    def test_the_family_follows_the_first_cidr(self, subnet: str, expected: str) -> None:
        """The same rule bind_addresses uses. The apiserver demands the two
        agree, so they are derived from one place."""
        assert advertise_address(subnet) == expected

    @pytest.mark.parametrize("subnet", ["10.96.0.0/16", "fd00:96::/108"])
    def test_it_is_global_unicast_and_never_loopback(self, subnet: str) -> None:
        """What the 1.37.0 check actually asks.

        Go's `IsGlobalUnicast()`, which is everything that is not loopback,
        link-local, multicast or unspecified. Deliberately not Python's
        `is_global`: that follows the IANA special-purpose registry, where
        documentation space is listed and so answers False. The two names
        mean different things, and the apiserver asks the Go one.
        """
        address = ipaddress.ip_address(advertise_address(subnet))

        assert not address.is_loopback
        assert not address.is_link_local
        assert not address.is_multicast
        assert not address.is_unspecified

    @pytest.mark.parametrize("subnet", ["10.96.0.0/16", "fd00:96::/108"])
    def test_it_matches_the_family_it_binds(self, subnet: str) -> None:
        """A mismatch is the other way this fails: `service IP family must
        match public address family`."""
        bare, _ = bind_addresses(subnet)

        assert ipaddress.ip_address(advertise_address(subnet)).version == ipaddress.ip_address(bare).version
