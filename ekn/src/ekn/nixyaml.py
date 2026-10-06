"""YAML as Nix builtins: `fromYAML`, `fromGoLikeYAML`, `toYAML` and their kin.

Moved here from nanopynix, whose only consumer of them was `ekn`. Most YAML
`ekn` reads arrives through import-from-derivation, so these are a
convenience; `lib/serialiseYaml.nix` probes for `toYAML` and falls back
when it is absent.
"""

from __future__ import annotations

import math
import re
import struct
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

import yaml
from huggorm.jsonprimop import JsonValue, register_json_primop
from pydantic import TypeAdapter, ValidationError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from huggorm import AsyncEvalState

_JsonValue: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


def _yaml12_loader() -> type[Any]:
    # CSafeLoader (libyaml-backed) instead of the pure-Python SafeLoader: the
    # C scanner/parser/composer still calls back into the same Python-level
    # Resolver/SafeConstructor machinery add_implicit_resolver/add_constructor
    # mutate, so this custom-tag setup carries over unchanged -- just faster
    # (easykubenix's ekn makes the same swap on the dump side in its own
    # gitops.py, benchmarked ~7.5x).
    class Loader(yaml.CSafeLoader):  # type: ignore[reportUnknownBaseType] -- PyYAML stubs may be incomplete
        pass

    legacy_tags = {
        "tag:yaml.org,2002:bool",
        "tag:yaml.org,2002:float",
        "tag:yaml.org,2002:int",
    }
    Loader.yaml_implicit_resolvers = {  # type: ignore[reportUnknownMemberType] -- yaml_implicit_resolvers class attribute not in stubs
        ch: [(tag, regexp) for tag, regexp in resolvers if tag not in legacy_tags]
        for ch, resolvers in yaml.CSafeLoader.yaml_implicit_resolvers.items()  # type: ignore[reportUnknownMemberType] -- yaml_implicit_resolvers not in stubs
    }

    Loader.add_implicit_resolver(  # type: ignore[reportUnknownMemberType] -- yaml.Loader methods may not have complete stubs
        "tag:yaml.org,2002:bool",
        re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
        list("tTfF"),
    )
    Loader.add_implicit_resolver(  # type: ignore[reportUnknownMemberType] -- yaml.Loader methods may not have complete stubs
        "tag:yaml.org,2002:int",
        re.compile(r"^[-+]?(?:[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$"),
        list("-+0123456789"),
    )
    Loader.add_implicit_resolver(  # type: ignore[reportUnknownMemberType] -- yaml.Loader methods may not have complete stubs
        "tag:yaml.org,2002:float",
        re.compile(
            r"""^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+]?[0-9]+)?
            |[-+]?(?:[0-9][0-9_]*)(?:[eE][-+]?[0-9]+)
            |[-+]?\.(?:inf|Inf|INF)
            |\.(?:nan|NaN|NAN))$""",
            re.VERBOSE,
        ),
        list("-+0123456789."),
    )
    Loader.add_constructor("tag:yaml.org,2002:int", _construct_yaml12_int)  # type: ignore[reportUnknownMemberType] -- yaml.Loader methods may not have complete stubs
    return Loader


# A float a YAML 1.2 reader resolves and a YAML 1.1 reader does not.
#
# 1.1's float production requires a decimal point before the exponent, and
# requires the exponent to carry a sign. So `1.5e+06` is a float in both, while
# `1e+06`, `1e6` and `1.5e6` are floats in 1.2 and plain strings in 1.1.
#
# That gap is not academic. Helm renders a chart value through Go's `%v` on a
# float64, which goes to scientific notation from 1e6 upwards, so a chart with
# `priorityClass.value: 1000000` -- topolvm, and most charts that set one --
# emits `value: 1e+06`. Read as 1.1 that is the string "1e+06", and the API
# server refuses it: ".value: expected numeric (int or float), got string".
_YAML12_ONLY_FLOAT = re.compile(
    r"^[-+]?(?:[0-9][0-9_]*(?:\.[0-9_]*)?|\.[0-9_]+)[eE][-+]?[0-9]+$",
)

# YAML 1.2's octal form, which 1.1's integer production has no alternative for.
# go-yaml reads `0o755` as 493.
_YAML12_ONLY_INT = re.compile(r"^[-+]?0o[0-7_]+$")

# Where Go's JSON encoder changes from the plain form to the exponent form.
#
# go-yaml reads `1e+06` as a float64, and then Go writes that float64 as
# `1000000`, because `encoding/json` prints an integral float64 with no
# decimal point below 1e21. The number reaches Nix as an integer. This reader
# gives a Python float, which reaches Nix as a float and goes back out as
# `1000000.0` -- into a field the API server types as int32.
#
# Kubernetes makes the same conversion for the same reason:
# `k8s.io/apimachinery/pkg/util/json` turns an integral JSON number into an
# int64 before anything decodes it.
_GO_PLAIN_FLOAT_LIMIT = 1e21


# `int | float` and not `float`: beartype checks the annotation at run time,
# and an int is not an instance of float there.
def _construct_yaml11_float(loader: Any, node: Any) -> int | float:
    value: float = yaml.CSafeLoader.construct_yaml_float(loader, node)  # type: ignore[reportUnknownMemberType] -- PyYAML stubs may be incomplete
    if value.is_integer() and abs(value) < _GO_PLAIN_FLOAT_LIMIT:
        return int(value)
    return value


def _yaml11_loader() -> type[Any]:
    class Loader(yaml.CSafeLoader):  # type: ignore[reportUnknownBaseType] -- PyYAML stubs may be incomplete
        pass

    # Helm's dialect, which is what this parser is for, and which is neither
    # version cleanly. It keeps 1.1's integers, because `defaultMode: 0644`
    # means 420 and 1.2 reads the same text as 644. It adds 1.2's floats,
    # because of the exponent above, and 1.2's octal, because go-yaml resolves
    # both and this loader is a description of go-yaml.
    #
    # Appending is what makes this narrow. PyYAML tries a first character's
    # resolvers in order and takes the first match, so every scalar 1.1
    # already resolves -- every octal integer, every 1.1 float -- is decided
    # before this is reached. The only scalars whose type changes are ones 1.1
    # called strings and no producer here means as strings.
    Loader.add_implicit_resolver(  # type: ignore[reportUnknownMemberType] -- yaml.Loader methods may not have complete stubs
        "tag:yaml.org,2002:float",
        _YAML12_ONLY_FLOAT,
        list("-+0123456789."),
    )
    # The constructor needs no change. PyYAML's 1.1 `construct_yaml_int` gives
    # a leading-zero integer to `int(text, 8)`, and Python accepts the `0o`
    # prefix when it matches the base.
    Loader.add_implicit_resolver(  # type: ignore[reportUnknownMemberType] -- yaml.Loader methods may not have complete stubs
        "tag:yaml.org,2002:int",
        _YAML12_ONLY_INT,
        list("-+0"),
    )
    Loader.add_constructor("tag:yaml.org,2002:float", _construct_yaml11_float)

    # YAML 1.1's core schema resolves a bare, unquoted `=` scalar to the
    # special "value" type (historically a mapping's "default key" marker),
    # but PyYAML's SafeConstructor never registers a constructor for it --
    # only the non-safe Constructor does. Real-world YAML 1.1 producers (Helm
    # charts, generated CRDs -- e.g. prometheus-operator's Alertmanager CRD
    # enumerates `=` as a literal matcher operator) emit bare `=` as plain
    # string content and expect it to round-trip as such, so treat it the
    # same way `construct_yaml_str` does instead of raising.
    Loader.add_constructor(
        "tag:yaml.org,2002:value",
        yaml.CSafeLoader.construct_yaml_str,  # type: ignore[reportUnknownMemberType, reportUnknownArgumentType] -- yaml.Loader methods may not have complete stubs
    )
    return Loader


def _construct_yaml12_int(loader: Any, node: Any) -> int:
    value = loader.construct_scalar(node).replace("_", "")
    sign = -1 if value.startswith("-") else 1
    unsigned = value[1:] if value.startswith(("-", "+")) else value
    if unsigned.startswith("0o"):
        return sign * int(unsigned, 0)
    if unsigned.startswith("0x"):
        return sign * int(unsigned, 0)
    return sign * int(unsigned, 10)


_STR_TAG = "tag:yaml.org,2002:str"
_BOOL_TAG = "tag:yaml.org,2002:bool"
_INT_TAG = "tag:yaml.org,2002:int"
_FLOAT_TAG = "tag:yaml.org,2002:float"
_NULL_TAG = "tag:yaml.org,2002:null"
_MERGE_TAG = "tag:yaml.org,2002:merge"
_MAP_TAG = "tag:yaml.org,2002:map"

# `resolveTable` in `resolve.go`. go-yaml reads the first character of a plain
# scalar and stops at once when that character is absent from this table. So
# `=`, `_1` and `yellow` are strings before any pattern runs.
_GO_HINT: dict[str, str] = {
    **dict.fromkeys("+-", "S"),
    **dict.fromkeys("0123456789", "D"),
    **dict.fromkeys("yYnNtTfFoO~", "M"),
    ".": ".",
}

# `resolveMapList` in `resolve.go`. These spellings and no others: `YeS` is a
# string, and so is `Y_ES`.
_GO_SCALAR_MAP: dict[str, tuple[str, Any]] = {}
for _go_value, _go_tag, _go_spellings in (
    (True, _BOOL_TAG, ("y", "Y", "yes", "Yes", "YES")),
    (True, _BOOL_TAG, ("true", "True", "TRUE")),
    (True, _BOOL_TAG, ("on", "On", "ON")),
    (False, _BOOL_TAG, ("n", "N", "no", "No", "NO")),
    (False, _BOOL_TAG, ("false", "False", "FALSE")),
    (False, _BOOL_TAG, ("off", "Off", "OFF")),
    (None, _NULL_TAG, ("", "~", "null", "Null", "NULL")),
    (math.nan, _FLOAT_TAG, (".nan", ".NaN", ".NAN")),
    (math.inf, _FLOAT_TAG, (".inf", ".Inf", ".INF", "+.inf", "+.Inf", "+.INF")),
    (-math.inf, _FLOAT_TAG, ("-.inf", "-.Inf", "-.INF")),
    # `resolveMapList` also holds `<<`, and `resolve()` can never reach it:
    # `resolveTable` has no `<`, so the hint is zero and the scalar is a
    # string. See `_go_resolve`, which answers `<<` before the hint.
):
    for _go_spelling in _go_spellings:
        _GO_SCALAR_MAP[_go_spelling] = (_go_tag, _go_value)

# `yamlStyleFloat` in `resolve.go`. It gates the float attempt for a scalar
# that starts with a digit or with a sign. go-yaml removes the underscores
# before it applies this pattern, so the pattern has none.
_GO_STYLE_FLOAT = re.compile(r"[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?")

# The `.` branch of `resolve.go` gives the text to `strconv.ParseFloat` with
# the underscores still in it, and Go's own literal syntax permits one between
# two digits. So `.5_0` is 0.5 and `.5e` is a string.
_GO_DOT_FLOAT = re.compile(r"\.[0-9](?:_?[0-9])*(?:[eE][-+]?[0-9](?:_?[0-9])*)?")

_GO_DIGITS = {
    2: re.compile(r"[01]+"),
    8: re.compile(r"[0-7]+"),
    10: re.compile(r"[0-9]+"),
    16: re.compile(r"[0-9a-fA-F]+"),
}
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1
_UINT64_MAX = 2**64 - 1

# A float32 needs at most 9 significant digits to read back as itself.
_GO_FLOAT32_DIGITS = 9
# The window where Go's `'g'` writes a plain decimal: `-4 <= exponent < 6`.
_GO_G_PLAIN_MINIMUM = -4
_GO_G_PLAIN_LIMIT = 6

_GO_BASE_PREFIX = {"b": 2, "o": 8, "x": 16}
# The shortest text a base prefix can be part of: the zero, the letter and one
# digit. Go's `strconv` takes the same bound, so `0x` is not a hexadecimal.
_GO_PREFIXED_MINIMUM = 3


def _go_base(digits: str) -> tuple[int, str]:
    """The base that `strconv.ParseInt(text, 0, 64)` reads, and the digits."""
    if not digits.startswith("0"):
        return (10, digits)
    if len(digits) >= _GO_PREFIXED_MINIMUM:
        base = _GO_BASE_PREFIX.get(digits[1].lower())
        if base is not None:
            return (base, digits[2:])
    # A bare leading zero is octal, and it is also the prefix. `0` alone
    # therefore leaves no digit at all.
    return (8, digits[1:])


def _go_parse_int(plain: str) -> int | None:
    """`strconv.ParseInt(plain, 0, 64)`, and `ParseUint` after it."""
    # Python's `int` accepts a Unicode digit and surrounding space. Go rejects
    # both, so every part below matches ASCII only.
    if not plain.isascii():
        return None
    digits = plain
    negative = digits.startswith("-")
    if digits[:1] in ("+", "-"):
        digits = digits[1:]
    signless = digits
    base, digits = _go_base(digits)
    if digits == "" and signless.startswith("0"):
        # `0` and `-0`. `ParseUint` runs its digit loop over the empty text
        # and returns zero with no error, so these are integers and not the
        # floats the attempt below would give. A value cannot tell the
        # difference; a key can, and go-yaml names an integer key `0` where
        # it names a float key `-0`.
        value = 0
    elif not _GO_DIGITS[base].fullmatch(digits):
        return None
    else:
        value = int(digits, base)
    if negative:
        value = -value
    if _INT64_MIN <= value <= _INT64_MAX:
        return value
    # ParseInt reported a range error. go-yaml then calls ParseUint on the
    # same text, and ParseUint permits no sign at all.
    if plain[:1] not in ("+", "-") and value <= _UINT64_MAX:
        return value
    return None


def _go_parse_float(text: str, pattern: re.Pattern[str]) -> float | None:
    """`strconv.ParseFloat(text, 64)`, behind the pattern of its own branch."""
    if not text.isascii() or not pattern.fullmatch(text):
        return None
    value = float(text)
    # ParseFloat reports a range error for a value it cannot hold, and go-yaml
    # leaves that scalar a string. Python returns an infinity instead, so
    # `75.e993` needs this line to stay the string it is in Kubernetes.
    if not math.isfinite(value):
        return None
    return value


# `int | float` and not `float`: beartype checks the annotation at run time,
# and an int is not an instance of float there.
def _go_number(value: float) -> int | float:
    """A float64 as Go's JSON encoder writes it.

    `encoding/json` writes an integral float64 with no decimal point below
    1e21, so the number reaches Nix as an integer. `repr` gives the same
    shortest decimal that Go gives, and `Decimal` then keeps the digits that
    `int()` of the float drops: Go writes float64(2**64) as
    18446744073709552000.
    """
    if value.is_integer() and abs(value) < _GO_PLAIN_FLOAT_LIMIT:
        return int(Decimal(repr(value)))
    return value


def _go_json_numbers(value: Any) -> Any:
    """Apply `_go_number` to every value, and to no key.

    Go answers a scalar in three stages, and only the last one sees a value
    as a number. go-yaml gives a float64, `sigs.k8s.io/yaml` names each key
    from that float64, and `encoding/json` writes what is left. So `1e+06`
    is the integer 1000000 as a value and the name `1e+06` as a key.

    Converting in the scalar constructor collapses the two, and the fuzzer
    reports it: a document keyed `1e+06` came out keyed `1000000`.
    """
    if isinstance(value, dict):
        return {key: _go_json_numbers(item) for key, item in value.items()}  # pyright: ignore[reportUnknownVariableType] -- the tree is Any by construction
    if isinstance(value, list):
        return [_go_json_numbers(item) for item in value]  # pyright: ignore[reportUnknownVariableType] -- the tree is Any by construction
    if isinstance(value, float):
        return _go_number(value)
    return value


def _go_resolve(text: str) -> tuple[str, Any]:
    """`resolve()` in `go.yaml.in/yaml/v2/resolve.go`, for an untyped target."""
    # go-yaml decides a merge in `decode.go`, in `isMerge`, and not here: it
    # asks whether a plain key is the text `<<`. PyYAML decides it from the
    # tag instead, so the tag has to say merge. The constructor gives the
    # string back, which is what `<<` in value position is to both readers.
    if text == "<<":
        return (_MERGE_TAG, "<<")
    hint = "N" if text == "" else _GO_HINT.get(text[0], "")
    if hint:
        item = _GO_SCALAR_MAP.get(text)
        if item is not None:
            return item
        # Base 60 is absent on purpose. go-yaml reads `1:30` as a string, and
        # YAML 1.2 dropped the notation.
        if hint == ".":
            value = _go_parse_float(text, _GO_DOT_FLOAT)
            if value is not None:
                return (_FLOAT_TAG, value)
        elif hint in ("D", "S"):
            # go-yaml tries a timestamp first here, and this port leaves that
            # out. `decode.go` sets the original string into an `interface{}`
            # for a timestamp, and no scalar that `parseTimestamp` accepts
            # resolves to anything but a string anyway. The branch cannot
            # change an answer. Do not add it back.
            plain = text.replace("_", "")
            number = _go_parse_int(plain)
            if number is not None:
                return (_INT_TAG, number)
            value = _go_parse_float(plain, _GO_STYLE_FLOAT)
            if value is not None:
                return (_FLOAT_TAG, value)
            # The `0b` fallback of `resolve.go` follows here. `ParseInt` with
            # base 0 already reads that prefix, so the fallback answers
            # nothing, and this port leaves it out.
    return (_STR_TAG, text)


def _construct_go_scalar(loader: Any, node: Any) -> Any:
    text: str = loader.construct_scalar(node)
    _tag, value = _go_resolve(text)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(
            f"YAML scalar {text!r} reads as {value}, and JSON holds no such number. "
            "Quote the scalar to keep it a string.",
        )
    return value


def _go_float32_text(value: float) -> str:
    """`strconv.FormatFloat(value, 'g', -1, 32)`.

    It is the shortest text that reads back as the same **float32**, which is
    what go-yaml's own writer gives a float. A value a float32 cannot hold
    becomes an infinity there, and `sigs.k8s.io/yaml` spells that `.inf`.
    """
    try:
        packed = struct.pack("<f", value)
    except OverflowError:
        return "-.inf" if value < 0 else ".inf"
    # A NaN reaches no key: `_construct_go_scalar` refuses the scalar first.
    narrowed: float = struct.unpack("<f", packed)[0]
    digits = _GO_FLOAT32_DIGITS
    for count in range(1, _GO_FLOAT32_DIGITS):
        if struct.unpack("<f", struct.pack("<f", float(f"{narrowed:.{count - 1}e}")))[0] == narrowed:
            digits = count
            break
    exponent = int(f"{narrowed:.{digits - 1}e}".split("e")[1])
    # **Go decides the form with precision 6, and not with the digit count.**
    # `ftoa.go` says so where it formats: "if precision was the shortest
    # possible, use precision 6 for this decision". Python's `%g` uses the
    # precision it is given, so `.1g` of -2000 is `-2e+03` where Go gives
    # `-2000`. The fuzzer reports it as a key that changed name.
    if _GO_G_PLAIN_MINIMUM <= exponent < _GO_G_PLAIN_LIMIT:
        return f"{narrowed:.{max(digits - 1 - exponent, 0)}f}"
    mantissa = f"{narrowed:.{digits - 1}e}".split("e")[0]
    return f"{mantissa}e{'+' if exponent >= 0 else '-'}{abs(exponent):02d}"


def _go_json_key(key: Any) -> str:
    """`convertToJSONableObject` in `sigs.k8s.io/yaml`.

    JSON holds no key but a string, so the step that turns the YAML into JSON
    names every other key. go-yaml has nothing to say here: it gives a map
    whose keys carry whatever type it resolved, and `on: 1` gives a boolean.
    """
    if isinstance(key, str):
        return key
    if isinstance(key, bool):
        return "true" if key else "false"
    if isinstance(key, int):
        return str(key)
    if isinstance(key, float):
        return _go_float32_text(key)
    raise ValueError(f"unsupported map key of type {type(key).__name__}: {key!r}")


def _construct_go_map(loader: Any, node: Any) -> Any:
    # A generator, like PyYAML's own `construct_yaml_map`: the empty dict goes
    # out first so a value inside it can refer to it.
    data: dict[str, Any] = {}
    yield data
    loader.flatten_mapping(node)
    # Each key is named before it goes in, and not after. Go holds a map of
    # untyped keys, where float64(-0.0) and int(0) are two keys; Python's dict
    # calls them one, because `-0.0 == 0` and the hashes agree. Naming first
    # moves the collision onto the name, which is where Go has it.
    for key_node, value_node in node.value:
        key = _go_json_key(loader.construct_object(key_node, deep=True))
        data[key] = loader.construct_object(value_node, deep=False)


def _go_like_loader() -> type[Any]:
    """A loader that answers like go-yaml v2, and not like a YAML version.

    `sigs.k8s.io/yaml` reads with go-yaml v2, so the Kubernetes API server
    decodes with it and Helm renders through it. That dialect is neither YAML
    1.1 nor YAML 1.2: it reads `0644` as 420, which is 1.1, and `1e+06` as a
    number, which is 1.2.

    The loader replaces PyYAML's resolution of a plain scalar rather than
    adding patterns to it. One function answers the question, and that
    function is a port of one function of Go. Issue #307 reports what a
    description written from memory costs.
    """

    class Loader(yaml.CSafeLoader):  # type: ignore[reportUnknownBaseType] -- PyYAML stubs may be incomplete
        def resolve(self, kind: Any, value: Any, implicit: Any) -> str:
            # A plain scalar is the only scalar go-yaml resolves. A quoted one
            # is a string, and PyYAML gives the answer go-yaml gives for a
            # sequence and for a mapping.
            if kind is yaml.ScalarNode and implicit[0]:
                return _go_resolve(value)[0]
            return cast("str", super().resolve(kind, value, implicit))  # type: ignore[reportUnknownMemberType] -- PyYAML stubs may be incomplete

    # `str` and `null` keep PyYAML's constructors: the first returns the text
    # and the second returns None, whatever the spelling. Every other tag
    # needs Go's value, so `_go_resolve` answers again.
    for tag in (_BOOL_TAG, _INT_TAG, _FLOAT_TAG, _MERGE_TAG):
        Loader.add_constructor(tag, _construct_go_scalar)  # type: ignore[reportUnknownMemberType] -- yaml.Loader methods may not have complete stubs
    Loader.add_constructor(_MAP_TAG, _construct_go_map)  # type: ignore[reportUnknownMemberType] -- yaml.Loader methods may not have complete stubs
    return Loader


def _validate_document(value: Any, builtin: str) -> JsonValue:
    try:
        result: JsonValue = _JsonValue.validate_python(value)  # type: ignore[reportUnknownVariableType] -- TypeAdapter returns Any
    except ValidationError as exc:
        raise ValueError(f"{builtin}: YAML document is not JSON-compatible: {exc}") from exc
    return result


def _validate_documents(values: Iterable[Any], builtin: str) -> list[JsonValue]:
    return [_validate_document(value, builtin) for value in values]


def _single_document(values: Iterable[Any], builtin: str, stream_builtin: str) -> JsonValue:
    docs = list(values)
    if len(docs) != 1:
        raise ValueError(
            f"{builtin}: expected exactly one YAML document, got {len(docs)}; "
            f"use {stream_builtin} for multi-document YAML",
        )
    return _validate_document(docs[0], builtin)


def _parse_error_message(exc: Exception) -> str:
    problem = getattr(exc, "problem", None)  # type: ignore[reportUnknownVariableType] -- dynamic attribute access on yaml exception
    mark = getattr(exc, "problem_mark", None)  # type: ignore[reportUnknownVariableType] -- dynamic attribute access on yaml exception
    if problem is None:
        return str(exc)
    if mark is None:
        return str(problem)
    return f"{problem} at line {mark.line + 1}, column {mark.column + 1}"  # type: ignore[reportUnknownMemberType] -- mark is Any from getattr on yaml exception


def from_yaml(source: str) -> JsonValue:
    """Parse YAML 1.2-style input into JSON-like Python values."""

    try:
        return _single_document(yaml.load_all(source, Loader=_yaml12_loader()), "fromYAML", "fromYAMLStream")
    except yaml.YAMLError as exc:
        raise ValueError(f"fromYAML: failed to parse YAML 1.2 document: {_parse_error_message(exc)}") from exc


def from_go_like_yaml(source: str) -> JsonValue:
    """Parse one YAML document the way go-yaml v2 reads it."""

    try:
        return _single_document(
            (_go_json_numbers(document) for document in yaml.load_all(source, Loader=_go_like_loader())),
            "fromGoLikeYAML",
            "fromGoLikeYAMLStream",
        )
    except yaml.YAMLError as exc:
        raise ValueError(f"fromGoLikeYAML: failed to parse YAML document: {_parse_error_message(exc)}") from exc


def from_go_like_yaml_stream(source: str) -> list[JsonValue]:
    """Parse a YAML document stream the way go-yaml v2 reads it."""

    try:
        return _validate_documents(
            (_go_json_numbers(document) for document in yaml.load_all(source, Loader=_go_like_loader())),
            "fromGoLikeYAMLStream",
        )
    except yaml.YAMLError as exc:
        raise ValueError(f"fromGoLikeYAMLStream: failed to parse YAML stream: {_parse_error_message(exc)}") from exc


def from_yaml11(source: str) -> JsonValue:
    """Parse legacy YAML 1.1 input into JSON-like Python values.

    .. deprecated::
       Use `from_go_like_yaml`. This loader describes go-yaml v2 with YAML
       1.1's tables and two additions, and issue #307 lists six classes of
       scalar where the description is wrong.
    """

    try:
        return _single_document(
            yaml.load_all(source, Loader=_yaml11_loader()),
            "fromYAML11",
            "fromYAML11Stream",
        )
    except yaml.YAMLError as exc:
        raise ValueError(f"fromYAML11: failed to parse YAML 1.1 document: {_parse_error_message(exc)}") from exc


def from_yaml_stream(source: str) -> list[JsonValue]:
    """Parse a YAML 1.2-style document stream into JSON-like Python values."""

    try:
        return _validate_documents(yaml.load_all(source, Loader=_yaml12_loader()), "fromYAMLStream")
    except yaml.YAMLError as exc:
        raise ValueError(f"fromYAMLStream: failed to parse YAML 1.2 stream: {_parse_error_message(exc)}") from exc


def from_yaml11_stream(source: str) -> list[JsonValue]:
    """Parse a legacy YAML 1.1 document stream into JSON-like Python values.

    .. deprecated::
       Use `from_go_like_yaml_stream`. See `from_yaml11`.
    """

    try:
        return _validate_documents(
            yaml.load_all(source, Loader=_yaml11_loader()),
            "fromYAML11Stream",
        )
    except yaml.YAMLError as exc:
        raise ValueError(f"fromYAML11Stream: failed to parse YAML 1.1 stream: {_parse_error_message(exc)}") from exc


class _BlockStyleDumper(yaml.CSafeDumper):
    """CSafeDumper that renders multi-line strings as literal blocks (``|``).

    A plain SafeDumper falls back to an escaped double-quoted scalar for any
    string PyYAML doesn't consider "simple" (e.g. containing '${{ ... }}'
    literals), which is valid YAML but unreadable for multi-line shell
    scripts. Scoped to a Dumper subclass rather than mutating
    yaml.SafeDumper globally, since other code in this process may still
    want PyYAML's default string style. CSafeDumper (libyaml-backed) instead
    of the pure-Python SafeDumper for the same reason as `_yaml12_loader`/
    `_yaml11_loader` above -- add_representer still mutates the shared
    Python-level Representer machinery the C emitter calls back into.

    **A string goes out plain only when reading it back gives that same
    string.** PyYAML asks `resolve` for the text it is about to emit and
    writes the scalar plain when the answer is still the tag it holds, so
    that one method is the whole quoting rule. The C emitter calls it too.

    Two readers answer here, and a disagreement from either one is enough to
    quote. `_go_resolve` answers for go-yaml v2, which is what the API server
    decodes with and so what reads a manifest GitOps commits: it reads a bare
    `n` as false and `08` as 8. PyYAML's own resolvers answer for YAML 1.1,
    which reads `1:30` as 90. Quoting on the union costs quotes and nothing
    else.

    The rule is the reader's own function, and not a description of it. A
    description drifts: nanopynix #307 lists six classes where one did.
    """

    def resolve(self, kind: Any, value: Any, implicit: Any) -> str:
        if kind is yaml.ScalarNode and implicit[0]:
            tag = _go_resolve(value)[0]
            if tag != _STR_TAG:
                return tag
        return cast("str", super().resolve(kind, value, implicit))  # type: ignore[reportUnknownMemberType] -- PyYAML stubs may be incomplete


def _represent_str(dumper: _BlockStyleDumper, data: str) -> yaml.Node:
    style = "|" if "\n" in data else None
    return dumper.represent_scalar(  # type: ignore[reportUnknownMemberType] -- PyYAML stubs don't type represent_scalar's return precisely
        "tag:yaml.org,2002:str",
        data,
        style=style,
    )


_BlockStyleDumper.add_representer(str, _represent_str)


def to_yaml(value: JsonValue) -> str:
    """Render JSON-like Nix/Python values as Kubernetes-compatible YAML."""

    value = _validate_document(value, "toYAML")
    try:
        if isinstance(value, list):
            rendered = yaml.dump_all(  # type: ignore[reportUnknownVariableType] -- PyYAML's dump_all overloads don't narrow the return type for a custom Dumper
                value,
                explicit_start=True,
                sort_keys=False,
                Dumper=_BlockStyleDumper,
            )
        else:
            rendered = yaml.dump(  # type: ignore[reportUnknownVariableType] -- PyYAML's dump overloads don't narrow the return type for a custom Dumper
                value,
                sort_keys=False,
                Dumper=_BlockStyleDumper,
            )
    except yaml.YAMLError as exc:
        raise ValueError(f"toYAML: failed to render YAML: {_parse_error_message(exc)}") from exc
    result: str = rendered
    return result


#: Every builtin this module adds, by name. A deprecated name stays while
#: `lib/` or a consumer may still call it.
YAML_PRIMOPS: dict[str, Callable[[Any], Any]] = {
    "fromYAML": from_yaml,
    "fromGoLikeYAML": from_go_like_yaml,
    "fromGoLikeYAMLStream": from_go_like_yaml_stream,
    "fromYAML11": from_yaml11,
    "fromYAMLStream": from_yaml_stream,
    "fromYAML11Stream": from_yaml11_stream,
    "toYAML": to_yaml,
}


async def register_yaml_primops(state: AsyncEvalState) -> None:
    """Give `state` every builtin in `YAML_PRIMOPS`."""
    for name, fn in YAML_PRIMOPS.items():
        await register_json_primop(state, name, 1, fn)
