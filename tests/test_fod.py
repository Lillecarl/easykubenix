"""Tests for fixed-output derivation source updates."""

from __future__ import annotations

import pytest

from ekn.fod import (
    FodSourceUpdateError,
    derivation_name_from_path,
    extract_fod_hash_mismatch,
    extract_unique_fod_hash_mismatch,
    find_fod_hash_literal,
    replace_fod_hash,
)


def test_extracts_only_the_exact_ansi_colored_nix_fod_shape() -> None:
    mismatch = extract_fod_hash_mismatch(
        "error: hash mismatch in fixed-output derivation '\x1b[35;1m/nix/store/source.drv\x1b[0m':\n"
        "  specified: \x1b[35;1msha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=\x1b[0m\n"
        "     got:    \x1b[35;1msha256-XG19bBLOoknhsnwV5rVaVGB8DYUiNPMklhyNotZNcD4=\x1b[0m",
    )

    assert mismatch is not None
    assert mismatch.got == "sha256-XG19bBLOoknhsnwV5rVaVGB8DYUiNPMklhyNotZNcD4="
    assert mismatch.drv_path == "/nix/store/source.drv"
    assert extract_fod_hash_mismatch("hash mismatch in an unrelated format") is None


def test_extracts_through_the_sequences_a_colour_pattern_misses() -> None:
    """The extractor filters with Nix's own filter, so it sees every sequence.

    This module used to carry its own pattern, and that pattern read a CSI
    sequence only. Measured: an OSC 8 hyperlink passed it unchanged, so
    ``drv_path`` came back with the escape bytes still around it. A 24-bit
    colour carries five parameters, and the ``strip-ansi`` package before that
    pattern read three, so it left the opening sequence in front of the hash
    and the two-line shape stopped matching.

    Nix has no reason to write either one today. Nix decides that, this
    repository does not, and a change in Nix must not become a silent failure
    to read a hash.
    """
    mismatch = extract_fod_hash_mismatch(
        "error: hash mismatch in fixed-output derivation "
        "'\x1b]8;;https://example.invalid/drv\x1b\\/nix/store/source.drv\x1b]8;;\x1b\\':\n"
        "  specified: \x1b[38;2;255;0;0msha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=\x1b[0m\n"
        "     got:    \x1b[38;2;0;255;0msha256-XG19bBLOoknhsnwV5rVaVGB8DYUiNPMklhyNotZNcD4=\x1b[0m",
    )

    assert mismatch is not None
    assert mismatch.got == "sha256-XG19bBLOoknhsnwV5rVaVGB8DYUiNPMklhyNotZNcD4="
    assert mismatch.drv_path == "/nix/store/source.drv"


def test_extract_unique_fod_hash_mismatch_rejects_multiple_events() -> None:
    first = (
        "error: hash mismatch in fixed-output derivation '/nix/store/a.drv':\n"
        "  specified: sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=\n"
        "  got: sha256-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB="
    )
    second = first.replace("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC")

    assert extract_unique_fod_hash_mismatch([first]) == extract_fod_hash_mismatch(first)
    assert extract_unique_fod_hash_mismatch([first, second]) is None


def test_replaces_one_plain_hash_literal() -> None:
    source = 'pkgs.fetchFromGitHub { owner = "lillecarl"; hash = ""; }\n'
    literal = find_fod_hash_literal(source, "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")

    assert replace_fod_hash(source, literal, "sha256-XG19bBLOoknhsnwV5rVaVGB8DYUiNPMklhyNotZNcD4=") == (
        'pkgs.fetchFromGitHub { owner = "lillecarl"; hash = "sha256-XG19bBLOoknhsnwV5rVaVGB8DYUiNPMklhyNotZNcD4="; }\n'
    )


def test_refuses_ambiguous_hash_literals() -> None:
    source = '{ first = { hash = ""; }; second = { sha256 = ""; }; }\n'

    with pytest.raises(FodSourceUpdateError, match="multiple hash literals"):
        find_fod_hash_literal(source, "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")


def test_matches_run_command_output_hash_by_derivation_name() -> None:
    source = """with import <nixpkgs> {};
let
  first = runCommand "first" { outputHash = ""; outputHashAlgo = "sha256"; outputHashMode = "flat"; } "echo first > $out";
  second = runCommand "second" { outputHash = ""; outputHashAlgo = "sha256"; outputHashMode = "flat"; } "echo second > $out";
in [ first second ]
"""

    literal = find_fod_hash_literal(
        source,
        "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        derivation_name="second",
    )

    assert literal.derivation_name == "second"
    assert (
        replace_fod_hash(source, literal, "sha256-XG19bBLOoknhsnwV5rVaVGB8DYUiNPMklhyNotZNcD4=").count("sha256-") == 1
    )


def test_find_fod_hash_literal_prefers_an_exact_value_match_over_ambiguity() -> None:
    """Two candidates exist, but one's stored value already equals *specified*
    (e.g. it was never actually empty); that one must win outright rather than
    falling through to the "multiple hash literals" ambiguity error."""
    source = (
        'first = { hash = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="; }; second = { hash = "sha256-BBBB"; };'
    )

    literal = find_fod_hash_literal(source, "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")

    assert literal.value == "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


def test_find_fod_hash_literal_raises_when_derivation_name_matches_nothing() -> None:
    """A derivation_name hint that matches zero (or more than one) candidate
    must fall through to the plain candidate-count check, not silently pick
    one -- here that count is itself ambiguous."""
    source = 'first = { hash = ""; }; second = { hash = ""; };'

    with pytest.raises(FodSourceUpdateError, match="multiple hash literals"):
        find_fod_hash_literal(source, "sha256-nomatch", derivation_name="nonexistent")


def test_find_fod_hash_literal_raises_when_no_candidates_exist() -> None:
    source = '{ pname = "nothing-hash-shaped-here"; }'

    with pytest.raises(FodSourceUpdateError, match="no hash, sha256, or outputHash binding"):
        find_fod_hash_literal(source, "sha256-nomatch")


# `lib.fakeHash` is 32 zero bytes in base64 and `lib.fakeSha256` is the same 32
# bytes in hex, so Nix reports one SRI string for both. `lib.fakeSha512` is 64
# zero bytes. Read out of nixpkgs, and checked against `nix hash convert`.
_FAKE_SRI = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
_FAKE_SHA512_SRI = "sha512-" + "A" * 86 + "=="
_REAL = "sha256-XG19bBLOoknhsnwV5rVaVGB8DYUiNPMklhyNotZNcD4="


@pytest.mark.parametrize(
    ("attribute", "symbol", "specified"),
    [
        ("hash", "lib.fakeHash", _FAKE_SRI),
        ("sha256", "lib.fakeSha256", _FAKE_SRI),
        ("outputHash", "lib.fakeSha512", _FAKE_SHA512_SRI),
        # `with lib;` puts the name in scope on its own.
        ("hash", "fakeHash", _FAKE_SRI),
        # A longer path still ends in the name that carries the constant.
        ("hash", "pkgs.lib.fakeHash", _FAKE_SRI),
    ],
)
def test_updates_a_fake_hash_symbol_as_if_it_were_a_literal(attribute: str, symbol: str, specified: str) -> None:
    """The nixpkgs convention for "not computed yet" is a symbol, not a string.

    An author writes `hash = lib.fakeHash` far more often than the zero string
    it stands for, and the updater saw neither the binding nor a reason to fail
    clearly. Issue #109.
    """
    source = f'fetchurl {{ url = "u"; {attribute} = {symbol}; }}'

    literal = find_fod_hash_literal(source, specified)

    assert literal.value == specified
    assert replace_fod_hash(source, literal, _REAL) == f'fetchurl {{ url = "u"; {attribute} = "{_REAL}"; }}'


def test_a_real_hash_elsewhere_survives_an_update_of_a_fake_one() -> None:
    """The acceptance criterion of #109: update the fake, touch nothing else."""
    source = 'a = { hash = lib.fakeHash; }; b = { hash = "sha256-keepmekeepme"; };'

    updated = replace_fod_hash(source, find_fod_hash_literal(source, _FAKE_SRI), _REAL)

    assert updated == f'a = {{ hash = "{_REAL}"; }}; b = {{ hash = "sha256-keepmekeepme"; }};'


def test_two_fake_hash_symbols_are_ambiguous_and_refused() -> None:
    """Both name the same constant, so the reported hash cannot separate them."""
    source = "a = { hash = lib.fakeHash; }; b = { sha256 = fakeSha256; };"

    with pytest.raises(FodSourceUpdateError, match="refusing to guess"):
        find_fod_hash_literal(source, _FAKE_SRI)


@pytest.mark.parametrize(
    "binding",
    [
        # `or` gives the expression a value that is not the constant.
        'hash = lib.fakeHash or "something-else"',
        # A name that is not one of the three says nothing about its value.
        "hash = lib.realHash",
        "hash = myOwnHash",
    ],
)
def test_a_symbol_that_does_not_name_a_known_constant_is_invisible(binding: str) -> None:
    """An unknown symbol has an unknown value, so the updater must not guess."""
    with pytest.raises(FodSourceUpdateError, match="no hash, sha256, or outputHash binding"):
        find_fod_hash_literal(f"x = {{ {binding}; }}", _FAKE_SRI)


def test_replace_fod_hash_rejects_a_computed_hash_with_unsafe_characters() -> None:
    source = 'x = { hash = ""; };'
    literal = find_fod_hash_literal(source, "sha256-nomatch")

    with pytest.raises(FodSourceUpdateError, match="not safe to insert"):
        replace_fod_hash(source, literal, 'sha256-has"quote')
    with pytest.raises(FodSourceUpdateError, match="not safe to insert"):
        replace_fod_hash(source, literal, "sha256-has\nnewline")


def test_find_fod_hash_literal_skips_a_non_runcommand_curried_call() -> None:
    """_run_command_hash_bindings must not attribute a derivation name to a
    curried call whose callee isn't runCommand, even though it has the same
    "NAME { attrs }" apply shape."""
    source = """
    let
      first = mkDerivation "first" { outputHash = ""; };
      second = runCommand "second" { outputHash = ""; } "cmd";
    in [ first second ]
    """

    literal = find_fod_hash_literal(source, "sha256-nomatch", derivation_name="second")

    assert literal.derivation_name == "second"


def test_find_fod_hash_literal_skips_inherit_statements_in_a_runcommand_binding_set() -> None:
    source = """
    let
      second = runCommand "second" { inherit somethingUnrelated; outputHash = ""; } "cmd";
    in second
    """

    literal = find_fod_hash_literal(source, "sha256-nomatch", derivation_name="second")

    assert literal.derivation_name == "second"


def test_derivation_name_from_path_handles_missing_and_malformed_paths() -> None:
    assert derivation_name_from_path(None) is None
    assert derivation_name_from_path("/nix/store/no-drv-suffix") is None
    assert derivation_name_from_path("/nix/store/nodash.drv") is None
    assert derivation_name_from_path("/nix/store/abc123hash-real-name.drv") == "real-name"
