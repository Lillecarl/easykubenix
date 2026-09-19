"""Differential fuzzer for the two YAML readers this project ships.

`ekn _yamlToJson --yaml-version yaml11` is PyYAML taught the Kubernetes
dialect by hand. `ekn-yaml2json` is go-yaml, the parser that description is a
description of. This generates YAML, gives the same text to both, and reports
every scalar they read differently.

It compares types and not only values. `1e+06` read as the float 1000000.0 and
as the integer 1000000 is the divergence that motivated the Go tool, and a
comparison that goes through `float()` cannot see it.

Run it from the repository root, with both programs on PATH:

    nix shell --file ./shell.nix
    python tools/yaml2json/fuzz.py --seed 0 --rounds 20

A seed makes a run reproducible, so a failing seed is a bug report.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

# Scalars whose reading is known to depend on the YAML version, plus the ones
# a bug in either reader has already been found at. A generator that only
# assembles digits reaches almost none of these.
CURATED = [
    # Integers, and the four prefixes.
    "0",
    "00",
    "08",
    "017",
    "0644",
    "0o755",
    "0o17",
    "0O17",
    "0xff",
    "0xFF",
    "0X1f",
    "0b1011",
    "1_000",
    "1__0",
    "+0",
    "-0",
    "-017",
    "+0o17",
    # Floats. 1.1 needs a decimal point before the exponent and a sign in it.
    "1e+06",
    "1e6",
    "1E6",
    "1.5e6",
    "1.5e+06",
    "-2e3",
    ".5e1",
    "1.",
    ".1",
    "2.0",
    "1e30",
    "1e21",
    "1e20",
    "1e-7",
    "0.0",
    "-0.0",
    # Not numbers, whatever they look like.
    "1e",
    "1.2.3",
    "0x",
    "0o",
    "0b",
    "1-2",
    # Booleans and null, 1.1 style.
    "yes",
    "Yes",
    "YES",
    "no",
    "on",
    "off",
    "Off",
    "true",
    "True",
    "false",
    "y",
    "n",
    "Y",
    "N",
    "null",
    "Null",
    "NULL",
    "~",
    "",
    # 1.1's sexagesimal integers, which 1.2 dropped.
    "1:30",
    "1:30:00",
    "-1:30",
    # The "value" tag, which PyYAML's SafeConstructor registers nothing for.
    "=",
    # Timestamps, which both readers may or may not resolve away from a string.
    "2023-01-01",
    "2023-01-01T00:00:00Z",
    "12:34:56",
    # Quoted, so no resolver runs at all. These must never differ.
    '"0644"',
    "'1e+06'",
    '"yes"',
]

_DIGITS = "0123456789"
_PREFIXES = ["", "0", "0x", "0X", "0o", "0O", "0b"]
_SIGNS = ["", "+", "-"]
_EXPONENTS = ["", "e", "E"]
# No whitespace, no `#`, and no leading indicator character. Those change the
# structure of the document rather than the reading of a scalar, which is a
# different test than this one.
_WORD = "abcdefABCDEF_.-+=~/@xXoObBeEyYnN"


def random_token(rng: random.Random) -> str:
    """One plain scalar, weighted towards things a resolver argues about."""
    kind = rng.random()
    if kind < 0.45:
        body = "".join(rng.choice(_DIGITS + "_") for _ in range(rng.randint(1, 6)))
        frac = rng.choice(["", ".", "." + "".join(rng.choice(_DIGITS) for _ in range(rng.randint(1, 3)))])
        exponent = rng.choice(_EXPONENTS)
        if exponent:
            exponent += rng.choice(_SIGNS) + "".join(rng.choice(_DIGITS) for _ in range(rng.randint(1, 3)))
        return rng.choice(_SIGNS) + rng.choice(_PREFIXES) + body + frac + exponent
    if kind < 0.60:
        parts = ["".join(rng.choice(_DIGITS) for _ in range(rng.randint(1, 2))) for _ in range(rng.randint(2, 3))]
        return rng.choice(_SIGNS) + ":".join(parts)
    return "".join(rng.choice(_WORD) for _ in range(rng.randint(1, 8)))


def plain(token: str) -> str:
    """A token as it appears on a line, with the empty scalar written out."""
    return token if token != "" else '""'


@dataclass
class Reading:
    """What one reader made of one stream."""

    failed: str | None = None
    documents: list[Any] = field(default_factory=list)


class Rejected:
    """A reader refused the document. Not a value, and not a string either."""

    def __repr__(self) -> str:
        return "rejected"


REJECTED = Rejected()


def kind_of(value: object) -> str:
    """The name this script groups a disagreement by."""
    if isinstance(value, Rejected):
        return "rejected"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, tuple):
        return str(value[0])
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "mapping"
    return type(value).__name__


def _number(text: str, kind: str) -> tuple[str, int | float]:
    # The kind travels with the value. Comparing `float(x)` alone would call
    # the integer 1000000 and the float 1000000.0 equal, and telling those two
    # apart is the reason this script exists.
    #
    # An integer keeps every digit. Past 2^53 a float round trip prints a
    # different integer, which would read as two readers agreeing.
    return ("int", int(text)) if kind == "int" else ("float", float(text))


def decode(text: str) -> list[Any]:
    return json.loads(
        text,
        parse_int=lambda t: _number(t, "int"),
        parse_float=lambda t: _number(t, "float"),
    )


def run(command: list[str], stream: str, drop_nulls: bool) -> Reading:
    completed = subprocess.run(command, input=stream, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        return Reading(failed=(completed.stderr.strip() or f"exit {completed.returncode}")[:200])
    try:
        documents = decode(completed.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        return Reading(failed=f"unreadable JSON: {exc}"[:200])
    if drop_nulls:
        # `ekn` keeps the documents that parse to null; Nix filters them later
        # and the Go tool drops them itself.
        documents = [document for document in documents if document is not None]
    return Reading(documents=documents)


@dataclass
class Fuzzer:
    python_command: list[str]
    go_command: list[str]
    # (what the Python made of it, what the Go made of it) -> the subjects.
    #
    # Grouped, because one cause produces many tokens: YAML 1.1's base-60
    # integers alone gave thirty lines of "python int, go string" in the first
    # run, which is one finding and not thirty.
    findings: dict[tuple[str, str], list[tuple[str, str, str]]] = field(default_factory=dict)
    streams: int = 0

    def read_both(self, stream: str) -> tuple[Reading, Reading]:
        self.streams += 1
        # One `ekn` start is about 1.4 s, so a run is minutes and a script
        # that prints only at the end looks hung. The counter goes to stderr,
        # which keeps stdout to the report.
        print(f"\rstreams {self.streams}, groups {len(self.findings)}", end="", file=sys.stderr, flush=True)
        return (
            run(self.python_command, stream, drop_nulls=True),
            run(self.go_command, stream, drop_nulls=False),
        )

    def record(self, subject: str, python: object, go: object) -> None:
        group = (kind_of(python), kind_of(go))
        subjects = self.findings.setdefault(group, [])
        # The generator reaches a token more than once, and one token is one
        # finding however often it comes up.
        if any(seen == subject for seen, _, _ in subjects):
            return
        subjects.append((subject, render(python), render(go)))

    def count(self) -> int:
        return sum(len(subjects) for subjects in self.findings.values())

    def scalars(self, tokens: list[str], position: str) -> None:
        """Compare one token per mapping entry, so a mismatch names itself.

        Both readers see one document holding every token, which is one
        subprocess each rather than one per token. A stream that fails on
        either side is halved until the tokens that caused it stand alone.
        """
        if not tokens:
            return
        if position == "key":
            lines = [f"{plain(token)}: 1" for token in tokens]
        else:
            lines = [f"k{index:04d}: {plain(token)}" for index, token in enumerate(tokens)]
        python, go = self.read_both("\n".join(lines) + "\n")

        if python.failed or go.failed:
            if len(tokens) == 1:
                # Both refusing is agreement. The messages differ because the
                # parsers differ, and neither reader is telling Nix anything.
                if python.failed and go.failed:
                    return
                self.record(
                    f"{position} {tokens[0]!r}",
                    REJECTED if python.failed else python.documents,
                    REJECTED if go.failed else go.documents,
                )
                return
            middle = len(tokens) // 2
            self.scalars(tokens[:middle], position)
            self.scalars(tokens[middle:], position)
            return

        if len(python.documents) != 1 or len(go.documents) != 1:
            self.record(f"{position} batch of {len(tokens)}", python.documents, go.documents)
            return

        python_map, go_map = python.documents[0], go.documents[0]
        if not isinstance(python_map, dict) or not isinstance(go_map, dict):
            self.record(f"{position} batch of {len(tokens)}", python_map, go_map)
            return

        if position == "key":
            # A key's own reading is the key itself, so compare the key sets.
            if set(python_map) != set(go_map):
                only_python = sorted(set(python_map) - set(go_map))
                only_go = sorted(set(go_map) - set(python_map))
                self.record(f"keys of a batch of {len(tokens)}", only_python, only_go)
            return

        for index, token in enumerate(tokens):
            name = f"k{index:04d}"
            python_value, go_value = python_map.get(name), go_map.get(name)
            if python_value != go_value:
                self.record(f"value {token!r}", python_value, go_value)

    def structure(self, rng: random.Random, tokens: list[str]) -> None:
        """A nested document, to reach the container and stream paths."""
        stream = "---\n" + emit(rng, tokens, depth=0, indent=0) + "\n"
        python, go = self.read_both(stream)
        if python.failed or go.failed:
            if python.failed and go.failed:
                return
            self.record(
                f"structure seeded {stream[:60]!r}",
                REJECTED if python.failed else python.documents,
                REJECTED if go.failed else go.documents,
            )
            return
        if python.documents != go.documents:
            self.record(f"structure seeded {stream[:60]!r}", python.documents, go.documents)


def emit(rng: random.Random, tokens: list[str], depth: int, indent: int) -> str:
    """One YAML value, as the text of a block mapping, sequence or scalar."""
    pad = " " * indent
    shape = rng.random()
    if depth >= 3 or shape < 0.35:
        return plain(rng.choice(tokens))
    if shape < 0.70:
        lines: list[str] = []
        for index in range(rng.randint(1, 4)):
            child = emit(rng, tokens, depth + 1, indent + 2)
            joiner = "\n" if "\n" in child or child.startswith(" ") else " "
            lines.append(f"{pad}n{depth}{index}:{joiner}{child}")
        return "\n".join(lines).lstrip() if indent == 0 else "\n" + "\n".join(lines)
    items: list[str] = []
    for _ in range(rng.randint(1, 3)):
        child = emit(rng, tokens, depth + 1, indent + 2)
        items.append(f"{pad}- {child.lstrip()}" if "\n" not in child else f"{pad}-{child}")
    return "\n".join(items).lstrip() if indent == 0 else "\n" + "\n".join(items)


def render(value: object) -> str:
    if isinstance(value, tuple) and len(value) == 2:
        kind, number = value
        return f"{number!r} ({kind})"
    return repr(value)


def resolve(name: str, fallback: list[str]) -> list[str]:
    found = shutil.which(name)
    if found is None:
        sys.exit(f"fuzz: {name} is not on PATH. Enter the dev shell, or pass its command.")
    return [found, *fallback]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=0, help="makes the run reproducible")
    parser.add_argument("--rounds", type=int, default=10, help="generated batches")
    parser.add_argument("--per-round", type=int, default=200, help="tokens in each batch")
    parser.add_argument("--structures", type=int, default=20, help="nested documents")
    parser.add_argument("--keys", action="store_true", help="also fuzz the mapping keys")
    parser.add_argument("--examples", type=int, default=3, help="examples printed per group")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    fuzzer = Fuzzer(
        python_command=resolve("ekn", ["_yamlToJson", "--yaml-version", "yaml11"]),
        go_command=resolve("ekn-yaml2json", ["--shape", "list"]),
    )

    fuzzer.scalars(CURATED, "value")
    if args.keys:
        fuzzer.scalars(CURATED, "key")

    for _ in range(args.rounds):
        tokens = [random_token(rng) for _ in range(args.per_round)]
        fuzzer.scalars(tokens, "value")
        if args.keys:
            fuzzer.scalars(sorted(set(tokens)), "key")

    for _ in range(args.structures):
        fuzzer.structure(rng, CURATED + [random_token(rng) for _ in range(20)])

    print(file=sys.stderr)
    print(f"seed {args.seed}: {fuzzer.streams} streams, {fuzzer.count()} mismatches in {len(fuzzer.findings)} groups")
    for (python_kind, go_kind), subjects in sorted(fuzzer.findings.items()):
        print(f"\n  python {python_kind}, go {go_kind} -- {len(subjects)}")
        for subject, python_value, go_value in subjects[: args.examples]:
            print(f"    {subject}\n        python {python_value}\n        go     {go_value}")
        if len(subjects) > args.examples:
            rest = ", ".join(subject for subject, _, _ in subjects[args.examples :])
            print(f"    and {len(subjects) - args.examples} more: {rest[:300]}")
    return 1 if fuzzer.findings else 0


if __name__ == "__main__":
    sys.exit(main())
