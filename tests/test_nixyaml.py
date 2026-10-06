"""Unit tests for ekn.nixyaml: the parsing and rendering, with no evaluator."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import yaml
from yaml.representer import RepresenterError

from ekn.nixyaml import (
    _parse_error_message,  # pyright: ignore[reportPrivateUsage] -- test reaches into the private helper for direct unit coverage of its fallback branches
    from_go_like_yaml,
    from_go_like_yaml_stream,
    from_yaml,
    from_yaml11,
    from_yaml11_stream,
    from_yaml_stream,
    to_yaml,
)

if TYPE_CHECKING:
    from huggorm.jsonprimop import JsonValue


def test_from_yaml_parses_hex_integers_as_yaml12() -> None:
    assert from_yaml("x: 0x1F") == {"x": 31}


def test_from_yaml_rejects_a_document_that_is_not_json_compatible() -> None:
    """A bare YAML timestamp parses to a datetime.date, which JsonValue
    cannot represent -- this must surface as a descriptive ValueError."""
    with pytest.raises(ValueError, match="YAML document is not JSON-compatible"):
        from_yaml("2023-01-01")


def test_from_yaml11_reports_a_parse_error() -> None:
    with pytest.raises(ValueError, match=r"fromYAML11: failed to parse YAML 1\.1 document"):
        from_yaml11("key: [unterminated")


def test_from_yaml_stream_reports_a_parse_error() -> None:
    with pytest.raises(ValueError, match=r"fromYAMLStream: failed to parse YAML 1\.2 stream"):
        from_yaml_stream("key: [unterminated")


def test_from_yaml11_stream_reports_a_parse_error() -> None:
    with pytest.raises(ValueError, match=r"fromYAML11Stream: failed to parse YAML 1\.1 stream"):
        from_yaml11_stream("key: [unterminated")


def test_to_yaml_wraps_a_render_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """No JsonValue-shaped input actually fails yaml.dump; force the failure
    path with a monkeypatch instead of contriving an unrepresentable value."""

    def _boom(*_args: object, **_kwargs: object) -> str:
        raise RepresenterError("cannot represent an object")

    monkeypatch.setattr(yaml, "dump", _boom)

    with pytest.raises(ValueError, match="toYAML: failed to render YAML"):
        to_yaml({"a": 1})


def test_parse_error_message_falls_back_to_str_without_a_problem_attribute() -> None:
    exc = RepresenterError("cannot represent an object")

    assert _parse_error_message(exc) == str(exc)


def test_parse_error_message_uses_the_problem_without_a_mark() -> None:
    class _FakeYamlError(Exception):
        problem = "mock problem"
        problem_mark = None

    assert _parse_error_message(_FakeYamlError()) == "mock problem"


# -- what Helm emits, and what has to come back out ------------------------
#
# `fromYAML11` reads Helm's output, and Helm's output is neither version
# cleanly: 1.1 integers, so that `defaultMode: 0644` means 420, and 1.2
# floats, because Go's `%v` on a float64 goes to scientific notation from 1e6
# upwards. The two below are one round trip and both halves used to break it.


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # The shape topolvm's priorityClass produces. 1.1's float production
        # requires a decimal point before the exponent, so this used to parse
        # as the string "1e+06" and the API server refused the PriorityClass:
        # ".value: expected numeric (int or float), got string".
        #
        # The results are integers, not floats, because go-yaml's are. Go
        # reads a float64 and `encoding/json` writes an integral float64 with
        # no decimal point, so the number reaches Nix as an integer.
        ("1e+06", 1000000),
        ("1e6", 1000000),
        # 1.1 also requires a sign in the exponent.
        ("1.5e6", 1500000),
        ("-2e3", -2000),
        (".5e1", 5),
        # Above 1e21 Go changes to the exponent form, and the number stays a
        # float on both sides.
        ("1e30", 1e30),
    ],
)
def test_from_yaml11_reads_a_float_the_way_helm_writes_one(text: str, expected: float) -> None:
    parsed = from_yaml11(f"value: {text}")

    assert parsed == {"value": expected}
    assert isinstance(parsed["value"], type(expected))  # type: ignore[reportIndexIssue] -- JsonValue is a union, and this document is a mapping


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Octal is the whole reason this parser is 1.1 rather than 1.2, and
        # widening the float production must not have touched it.
        ("0644", 420),
        ("017", 15),
        # 1.2's octal form. 1.1's integer production has no alternative for
        # the prefix, so this resolved as the string "0o755" until the 1.2
        # resolver joined the float one. go-yaml reads 493.
        ("0o755", 493),
        ("-0o17", -15),
        # A 1.1 float stays one, decided before the added resolver is reached.
        # The value is an integer for the same reason as above: go-yaml writes
        # an integral float64 without a decimal point.
        ("1.5e+06", 1500000),
        ("1.5", 1.5),
        ("2.0", 2),
        # And a scalar neither version calls a number stays a string.
        ("1e", "1e"),
        ("abc", "abc"),
        ("1.2.3", "1.2.3"),
    ],
)
def test_from_yaml11_still_resolves_what_it_always_did(text: str, expected: object) -> None:
    assert from_yaml11(f"value: {text}") == {"value": expected}


@pytest.mark.parametrize(
    "text",
    [
        # Written plain, a YAML 1.2 reader takes this back as a float. The
        # dumper's own resolvers are 1.1's, which call it a string, so it used
        # to go out unquoted and change type on the next parse.
        "1e+06",
        "1e6",
        "1.5e6",
        # 1.1 already quoted these; they are here so a later change cannot
        # quietly stop.
        "0644",
        "1.5",
        "1000",
        "true",
        # 1.2 octal, which 1.1 calls a string.
        "0o17",
    ],
)
def test_to_yaml_quotes_a_string_some_reader_would_call_a_number(text: str) -> None:
    assert to_yaml({"value": text}) == f"value: '{text}'\n"


@pytest.mark.parametrize(
    "text",
    [
        # go-yaml's one-letter booleans. PyYAML's 1.1 resolver leaves these
        # alone, so each went out plain and the API server read it back as a
        # boolean.
        "y",
        "n",
        "Y",
        "N",
        # A leading zero with a digit 1.1's octal production refuses. 1.1
        # gives up and calls it a string; go-yaml reads decimal.
        "08",
        "-0892864",
        # 1.1's hex and octal productions are lowercase only.
        "0X1f",
        "0O17",
    ],
)
def test_to_yaml_quotes_a_string_go_yaml_would_call_something_else(text: str) -> None:
    """The reader of this output is go-yaml, so its scalars decide the quoting.

    A manifest that carries the string "n" and writes it plain reaches the
    cluster as false.
    """
    assert to_yaml({"value": text}) == f"value: '{text}'\n"


@pytest.mark.parametrize(
    "text",
    [
        "yellow",
        "Nx",
        "x",
        "name",
        "0x",
    ],
)
def test_to_yaml_leaves_an_ordinary_string_plain(text: str) -> None:
    """Widening the quoting must not quote every string that starts with a y."""
    assert to_yaml({"value": text}) == f"value: {text}\n"


@pytest.mark.parametrize(
    ("value", "rendered"),
    [
        # A real number still goes out plain. PyYAML writes `1.0e+30` rather
        # than `1e+30`, which both versions resolve as a float, so nothing
        # here starts quoting numbers.
        (1000000.0, "1000000.0"),
        (1e30, "1.0e+30"),
        (420, "420"),
        (1.5, "1.5"),
        (True, "true"),
    ],
)
def test_to_yaml_leaves_a_number_unquoted(value: JsonValue, rendered: str) -> None:
    assert to_yaml({"value": value}) == f"value: {rendered}\n"


def test_a_helm_float_survives_the_round_trip() -> None:
    """Read what Helm wrote, write it again, read it again."""
    once = from_yaml11("value: 1e+06")

    assert from_yaml11(to_yaml(once)) == once == {"value": 1000000}


def test_a_float_this_reader_turns_into_an_integer_still_round_trips() -> None:
    """A float given to the dumper comes back as the integer it equals.

    The dumper writes 1000000.0 with the decimal point, because it holds a
    Python float. The reader gives that text back as an integer, because
    go-yaml's JSON does. The value is the same and the type is not, so the
    second read is the fixed point and not the first.
    """
    written = to_yaml({"value": 1000000.0})

    assert written == "value: 1000000.0\n"
    assert from_yaml11(written) == {"value": 1000000}
    assert from_yaml11(to_yaml(from_yaml11(written))) == {"value": 1000000}


# What go-yaml v2 gives for each scalar, measured and not reasoned about.
#
# The measurement is `ekn-yaml2json` in easykubenix, which reads through
# `sigs.k8s.io/yaml` v1.6.0 and so through `go.yaml.in/yaml/v2`. That is the
# parser the Kubernetes API server decodes with. These tests run no Go
# program, so the answers live here. Re-measure with:
#
#     printf 'x: <scalar>\n' | ekn-yaml2json --shape list
#
# The type matters as much as the value, so the test compares both.
_GO_LIKE_SCALARS: list[tuple[str, object]] = [
    # Integers YAML 1.1 reads and YAML 1.2 does not.
    ("0644", 420),
    ("+0644", 420),
    ("0_0", 0),
    ("00", 0),
    ("0", 0),
    ("-0", 0),
    # YAML 1.2's octal, and both cases of every prefix.
    ("0o755", 493),
    ("0O17", 15),
    ("0X1f", 31),
    ("-0X_5", -5),
    ("0x_1f", 31),
    ("0b101", 5),
    ("0b1_01", 5),
    ("1_000", 1000),
    ("1__0", 10),
    ("1_", 1),
    # A leading zero that is not octal. YAML 1.1 gives up and calls these
    # strings; go-yaml falls through to the float attempt and reads a number.
    ("08", 8),
    ("-0892864", -892864),
    # Helm writes a chart value through Go's `%v`, which reaches scientific
    # notation from 1e6. Go's JSON writes that float64 back with no decimal
    # point, so the number reaches Nix as an integer.
    ("1e+06", 1000000),
    ("1.0", 1),
    ("75.", 75),
    (".5e3", 500),
    # Floats.
    ("1.5", 1.5),
    (".5", 0.5),
    (".5_0", 0.5),
    ("-.5", -0.5),
    ("1_000.5", 1000.5),
    ("1.2e+3_0", 1.2e30),
    ("1e21", 1e21),
    ("123456789012345678901234567890", 1.2345678901234568e29),
    # The edges of int64 and uint64. Past them go-yaml reads a float64, and
    # Go's JSON writes the shortest decimal of that float64.
    ("9007199254740993", 9007199254740993),
    ("9223372036854775807", 9223372036854775807),
    ("9223372036854775808", 9223372036854775808),
    ("18446744073709551615", 18446744073709551615),
    ("18446744073709551616", 18446744073709552000),
    ("-9223372036854775809", -9223372036854776000),
    # Booleans. YAML 1.1 leaves a bare `y` alone; go-yaml does not.
    ("y", True),
    ("Y", True),
    ("yes", True),
    ("on", True),
    ("TRUE", True),
    ("n", False),
    ("N", False),
    ("No", False),
    ("off", False),
    ("'No'", "No"),
    # Nulls.
    ("~", None),
    ("null", None),
    ("", None),
    # Base 60. YAML 1.1 reads `1:30` as 90; go-yaml reads a string.
    ("1:30", "1:30"),
    ("12:34:56", "12:34:56"),
    # A timestamp. go-yaml resolves one and then puts the original text into
    # an untyped value, so nothing but the string reaches JSON.
    ("2023-01-01", "2023-01-01"),
    ("2023-01-01T10:00:00Z", "2023-01-01T10:00:00Z"),
    ("2023-1-2", "2023-1-2"),
    ("2023-01-02 15:04:05", "2023-01-02 15:04:05"),
    # A number too large for a float64. Go reports a range error and keeps
    # the string; Python's `float()` returns an infinity instead.
    ("75.e993", "75.e993"),
    ("1e400", "1e400"),
    # Strings. The first character decides: go-yaml consults no table for a
    # scalar that starts outside its hint set.
    ("=", "="),
    ("_1", "_1"),
    ("yellow", "yellow"),
    ("ne", "ne"),
    ("v1", "v1"),
    ("1.2.3", "1.2.3"),
    ("٣", "٣"),
    ("+", "+"),
    (".", "."),
    ("0x", "0x"),
    ("0X", "0X"),
    ("0o8", "0o8"),
    ("1e", "1e"),
    (".inf5", ".inf5"),
    (".5e", ".5e"),
    ("<<", "<<"),
    ('"8080"', "8080"),
]


@pytest.mark.parametrize(("text", "expected"), _GO_LIKE_SCALARS)
def test_a_scalar_reads_the_way_go_yaml_reads_it(text: str, expected: object) -> None:
    document = from_go_like_yaml(f"x: {text}")

    assert isinstance(document, dict)
    assert document == {"x": expected}
    assert type(document["x"]) is type(expected)


def test_a_quoted_scalar_stays_a_string() -> None:
    """go-yaml resolves a plain scalar only."""
    assert from_go_like_yaml("a: \"0644\"\nb: 'y'\n") == {"a": "0644", "b": "y"}


@pytest.mark.parametrize("text", [".inf", "-.inf", ".nan", "+.INF"])
def test_a_scalar_with_no_json_number_is_refused(text: str) -> None:
    """go-yaml reads these as an infinity or a NaN, and JSON holds neither.

    The Go path fails too: `ekn-yaml2json` exits with
    `json: unsupported value: +Inf`. Silence is the worse answer, because
    pydantic writes an infinity out as null.
    """
    with pytest.raises(ValueError, match="JSON holds no such number"):
        from_go_like_yaml(f"x: {text}")


def test_a_merge_key_merges() -> None:
    assert from_go_like_yaml("base: &b {a: 1}\nuse:\n  <<: *b\n  b: 2\n") == {
        "base": {"a": 1},
        "use": {"a": 1, "b": 2},
    }


def test_from_go_like_yaml_reads_a_stream() -> None:
    assert from_go_like_yaml_stream("a: y\n---\nb: 0644\n") == [{"a": True}, {"b": 420}]


def test_from_go_like_yaml_needs_exactly_one_document() -> None:
    with pytest.raises(ValueError, match="use fromGoLikeYAMLStream"):
        from_go_like_yaml("a: 1\n---\nb: 2\n")


def test_from_go_like_yaml_reports_a_parse_error() -> None:
    with pytest.raises(ValueError, match="fromGoLikeYAML: failed to parse YAML document"):
        from_go_like_yaml("key: [unterminated")


def test_from_go_like_yaml_stream_reports_a_parse_error() -> None:
    with pytest.raises(ValueError, match="fromGoLikeYAMLStream: failed to parse YAML stream"):
        from_go_like_yaml_stream("key: [unterminated")


# What `ekn-yaml2json` writes for each mapping key. Same measurement as the
# scalar table, and a different rule behind it: `convertToJSONableObject` in
# `sigs.k8s.io/yaml` names the key, because JSON holds no other kind.
@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("on", "true"),
        ("No", "false"),
        ("1", "1"),
        ("0644", "420"),
        ("1.5", "1.5"),
        ("1.0", "1"),
        # A key is named from the float64, and a value is written by Go's
        # JSON encoder. Only the second turns this into 1000000.
        ("1e+06", "1e+06"),
        # `-0` is an integer to go-yaml and `-0.0` is a float, and only a key
        # can tell: both are the value 0.
        ("-0.0", "-0"),
        ("-0", "0"),
        # The shortest text that reads back as the same float32, and not as
        # the same float64: `3.14159265358979` loses its tail.
        ("3.14159265358979", "3.1415927"),
        ("1e-7", "1e-07"),
        ("1.5e30", "1.5e+30"),
        # Go picks the plain form for -4 <= exponent < 6, and it decides that
        # with precision 6 and not with the digit count. Python's `%g` uses
        # the precision it is given, so `.1g` of -2000 is `-2e+03`.
        ("-2e+03", "-2000"),
        ("4.5e+05", "450000"),
        ("4.6602e+05", "466020"),
        ("2.0855e+07", "2.0855e+07"),
        ("4.497e+18", "4.497e+18"),
        # Larger than a float32 holds, so Go's conversion gives an infinity.
        ("1e40", ".inf"),
        ("1e-50", "0"),
        ('"1"', "1"),
    ],
)
def test_a_mapping_key_takes_the_name_kubernetes_gives_it(text: str, name: str) -> None:
    assert from_go_like_yaml(f"{text}: a") == {name: "a"}


# A string the dumper must not write plain. Each one is a scalar some reader
# takes back as a number, a boolean or a null, so writing it plain is a type
# change on the next parse -- and go-yaml is what parses `manifestYAMLFile`
# after GitOps commits it.
@pytest.mark.parametrize(
    "text",
    [
        "y",
        "n",
        "Y",
        "N",
        "08",
        "-0892864",
        "0X1f",
        "0O17",
        "0o755",
        "0644",
        "1e+06",
        "1:30",
        "yes",
        "true",
        "on",
        "2023-01-01",
        # The two the fuzzer found at seed 0. go-yaml removes an underscore
        # before it reads a number, and no hand-written pattern had that.
        "+_803.e+048",
        "-_9.e-515",
        ".inf",
        "~",
        "",
        "<<",
        "0b101",
        "75.",
        "8080",
    ],
)
def test_a_string_survives_the_write_and_the_read(text: str) -> None:
    assert from_go_like_yaml(to_yaml({"v": text})) == {"v": text}


@pytest.mark.parametrize("text", ["yellow", "x", "Nx", "ne", "v1", "1.2.3"])
def test_an_ordinary_string_still_goes_out_plain(text: str) -> None:
    """The rule only ever adds quotes, and it adds none of these."""
    assert to_yaml({"v": text}) == f"v: {text}\n"


def test_a_null_key_has_no_name() -> None:
    """`sigs.k8s.io/yaml` refuses this one too, in the same words."""
    with pytest.raises(ValueError, match="unsupported map key of type NoneType"):
        from_go_like_yaml("~: a")


def test_the_date_that_stops_the_deprecated_reader_is_read_here() -> None:
    """The class of #307 that breaks a chart today.

    `fromYAML11` gives a `datetime.date`, which JsonValue cannot hold, and
    the whole document fails.
    """
    with pytest.raises(ValueError, match="not JSON-compatible"):
        from_yaml11("x: 2023-01-01")

    assert from_go_like_yaml("x: 2023-01-01") == {"x": "2023-01-01"}
