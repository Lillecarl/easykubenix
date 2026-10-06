"""Conservative fixed-output derivation hash extraction and source updates.

Moved here from nanopynix_helpers, with only the parts `ekn` calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import tree_sitter_nix  # type: ignore[reportMissingTypeStubs] -- tree-sitter-nix does not ship type stubs
from huggorm import filter_ansi_escapes
from tree_sitter import Language, Parser

if TYPE_CHECKING:
    from collections.abc import Iterable

_HASH_ATTRIBUTES = frozenset({"hash", "sha256", "outputHash"})
_HASH = r"(?:md5|sha1|sha256|sha512)(?::[0-9a-z]+|-[A-Za-z0-9+/]+={0,2})"
_FOD_MISMATCH = re.compile(
    rf"^\s*(?:error:\s+)?hash mismatch in (?:fixed-output derivation '(?P<drv_path>[^']+)'|file downloaded from '[^']+'):\n"
    rf"\s*specified:\s+(?P<specified>{_HASH})\n"
    rf"\s*got:\s+(?P<got>{_HASH})$",
    re.MULTILINE,
)
_NIX_LANGUAGE = Language(cast("int", tree_sitter_nix.language()))  # type: ignore[reportDeprecated, reportUnknownMemberType] -- tree-sitter-nix 0.3 exposes the legacy integer language pointer

# An `apply_expression` node is always a binary (function, argument) application.
_APPLY_EXPRESSION_ARITY = 2

# `lib.fakeHash` and its two siblings are plain string constants in nixpkgs,
# and an author writes one to mean "I have not computed this hash yet". Nix
# normalises whatever it reads to SRI before it reports a mismatch, so the
# `specified` hash of the failure is the SRI form of the constant. That is what
# makes a symbol safe to update: the value the file names is known, and it is
# the value the failure reports, so `find_fod_hash_literal` matches it on value
# exactly as it matches a literal.
#
# `fakeHash` and `fakeSha256` are the same 32 zero bytes, written in base64 and
# in hex, so both normalise to one SRI string. Read out of nixpkgs and checked
# against `nix hash convert`.
_FAKE_HASH_SYMBOLS = {
    "fakeHash": "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "fakeSha256": "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "fakeSha512": "sha512-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==",
}

# A dotted path of plain identifiers, and nothing else. `lib.fakeHash` matches,
# and so does a bare `fakeHash` under `with lib;`. `lib.fakeHash or "x"` and a
# select with an interpolated attribute do not, because neither one still names
# the constant.
_SYMBOL_PATH = re.compile(r"[A-Za-z_][A-Za-z0-9_'-]*(?:\.[A-Za-z_][A-Za-z0-9_'-]*)*")

_SYMBOL_EXPRESSIONS = frozenset({"variable_expression", "select_expression"})

# `binding` is `attrpath "=" expression ";"`, so it has exactly two named
# children and the value is the second.
_BINDING_NAMED_CHILDREN = 2


class FodSourceUpdateError(ValueError):
    """A fixed-output mismatch could not be mapped to one safe source literal."""


@dataclass(frozen=True)
class FodHashMismatch:
    """The specified and actual Nix hashes from one strict FOD mismatch message."""

    specified: str
    got: str
    drv_path: str | None


@dataclass(frozen=True)
class FodHashLiteral:
    """One updatable fixed-output hash binding in a Nix source file.

    ``value`` is the hash the binding names. For a string literal that is the
    text between the quotes. For a `lib.fakeHash`-style symbol it is the
    constant that symbol stands for, so a caller compares values and never has
    to know which of the two shapes it holds.

    ``start_byte`` and ``end_byte`` span the whole value node, so replacing it
    with a quoted literal is one splice in either case.
    """

    attribute: str
    value: str
    start_byte: int
    end_byte: int
    derivation_name: str | None = None


def extract_fod_hash_mismatch(message: str) -> FodHashMismatch | None:
    """Extract a FOD mismatch only from Nix's exact two-line diagnostic shape.

    TODO: Replace this fallback with a structured Nix API. One promising route is
    evaluating the target, walking its derivation closure, filtering unbuilt
    FODs, and building only those in a temporary chroot store where their
    output metadata can be observed directly.
    """
    matches = list(_FOD_MISMATCH.finditer(filter_ansi_escapes(message, filter_all=True)))
    if len(matches) != 1:
        return None
    match = matches[0]
    return FodHashMismatch(specified=match["specified"], got=match["got"], drv_path=match["drv_path"])


def extract_unique_fod_hash_mismatch(messages: Iterable[str]) -> FodHashMismatch | None:
    """Return one mismatch from log messages, refusing ambiguous diagnostics."""
    by_drv: dict[str | None, list[FodHashMismatch]] = {}
    for message in messages:
        mismatch = extract_fod_hash_mismatch(message)
        if mismatch is not None:
            by_drv.setdefault(mismatch.drv_path, []).append(mismatch)
    if not by_drv:
        return None
    unambiguous: list[FodHashMismatch] = []
    for items in by_drv.values():
        first = items[0]
        if all(item == first for item in items[1:]):
            unambiguous.append(first)
        else:
            return None
    return unambiguous[0] if unambiguous else None


def find_fod_hash_literal(source: str, specified: str, *, derivation_name: str | None = None) -> FodHashLiteral:
    """Find the unambiguous hash binding for a failed FOD."""
    encoded = source.encode()
    candidates = _fod_hash_literals(encoded)
    exact = [candidate for candidate in candidates if candidate.value == specified]
    if len(exact) == 1:
        return exact[0]
    if derivation_name is not None:
        named = [candidate for candidate in candidates if candidate.derivation_name == derivation_name]
        if len(named) == 1:
            return named[0]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise FodSourceUpdateError(
            "no hash, sha256, or outputHash binding found that is a plain string or a fakeHash symbol"
        )
    raise FodSourceUpdateError(
        "multiple hash literals found; refusing to guess which one produced the failed derivation"
    )


def replace_fod_hash(source: str, literal: FodHashLiteral, got: str) -> str:
    """Replace exactly one FOD hash binding with Nix's reported hash."""
    if '"' in got or "\n" in got:
        raise FodSourceUpdateError("the computed hash is not safe to insert into a Nix string literal")
    encoded = source.encode()
    return (encoded[: literal.start_byte] + f'"{got}"'.encode() + encoded[literal.end_byte :]).decode()


def derivation_name_from_path(drv_path: str | None) -> str | None:
    """Return the Nix derivation name embedded in a store ``.drv`` path."""
    if drv_path is None:
        return None
    base_name = drv_path.rsplit("/", 1)[-1]
    hash_separator = base_name.find("-")
    if hash_separator < 0 or not base_name.endswith(".drv"):
        return None
    return base_name[hash_separator + 1 : -len(".drv")]


def _fod_hash_literals(source: bytes) -> list[FodHashLiteral]:
    root = Parser(_NIX_LANGUAGE).parse(source).root_node
    run_command_bindings = _run_command_hash_bindings(root, source)
    literals: list[FodHashLiteral] = []
    nodes: list[Any] = [root]
    while nodes:
        node = nodes.pop()
        nodes.extend(reversed(node.children))
        if node.type != "binding":
            continue
        named = node.named_children
        if len(named) != _BINDING_NAMED_CHILDREN or named[0].type != "attrpath":
            continue
        attrpath, value_node = named
        attribute = source[attrpath.start_byte : attrpath.end_byte].decode()
        if attribute not in _HASH_ATTRIBUTES:
            continue
        value = _hash_binding_value(value_node, source)
        if value is None:
            continue
        literals.append(
            FodHashLiteral(
                attribute,
                value,
                value_node.start_byte,
                value_node.end_byte,
                run_command_bindings.get(node.start_byte),
            ),
        )
    return literals


def _hash_binding_value(node: Any, source: bytes) -> str | None:
    """The hash a binding's value names, whether it is a literal or a symbol.

    Returns ``None`` for every other shape, so a binding this function cannot
    read stays invisible to the updater rather than becoming a guess.
    """
    rendered = source[node.start_byte : node.end_byte]
    if node.type == "string_expression":
        if any(child.type == "interpolation" for child in node.children):
            return None
        if not rendered.startswith(b'"') or not rendered.endswith(b'"'):
            return None
        return rendered[1:-1].decode()
    if node.type in _SYMBOL_EXPRESSIONS:
        text = rendered.decode()
        if not _SYMBOL_PATH.fullmatch(text):
            return None
        return _FAKE_HASH_SYMBOLS.get(text.rsplit(".", 1)[-1])
    return None


def _run_command_hash_bindings(root: Any, source: bytes) -> dict[int, str]:  # noqa: C901 -- tracked complexity/arg-count debt, see TODO.md
    """Return direct output-hash bindings of ``runCommand NAME { ... }`` calls."""
    names: dict[int, str] = {}
    nodes: list[Any] = [root]
    while nodes:
        node = nodes.pop()
        nodes.extend(reversed(node.children))
        if node.type != "apply_expression" or len(node.named_children) != _APPLY_EXPRESSION_ARITY:
            continue
        function, attrs = node.named_children
        if attrs.type != "attrset_expression" or function.type != "apply_expression":
            continue
        if len(function.named_children) != _APPLY_EXPRESSION_ARITY:
            continue
        callee, name = function.named_children
        if name.type != "string_expression" or any(child.type == "interpolation" for child in name.children):
            continue
        callee_text = source[callee.start_byte : callee.end_byte]
        rendered_name = source[name.start_byte : name.end_byte]
        if (
            not callee_text.endswith(b"runCommand")
            or not rendered_name.startswith(b'"')
            or not rendered_name.endswith(b'"')
        ):
            continue
        derivation_name = rendered_name[1:-1].decode()
        binding_set = next((child for child in attrs.named_children if child.type == "binding_set"), None)
        if binding_set is None:
            continue
        for binding in binding_set.named_children:
            if binding.type != "binding":
                continue
            attrpath = next((child for child in binding.named_children if child.type == "attrpath"), None)
            if attrpath is None or source[attrpath.start_byte : attrpath.end_byte] != b"outputHash":
                continue
            names[binding.start_byte] = derivation_name
    return names
