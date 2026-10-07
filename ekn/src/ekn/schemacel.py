"""A rendered CRD's CEL rules (`x-kubernetes-validations`), evaluated with cel-python.

The API server evaluates each rule with `self` bound to the value at the
schema node that declares it. This walks the CRD's schema and the object
together and does the same, for the rules it can:

- A rule that reads `oldSelf` is a transition rule. It runs only on an
  update, against the stored object, so it is skipped.
- A rule cel-python cannot parse, or that calls a Kubernetes CEL extension
  it does not have, is skipped and counted: there is nothing to judge with.

A rule that evaluates to `false` is a violation, and so is one that fails at
run time, as it is on the API server. Schema defaults are filled first, the
way the API server fills them, so a field it would default is not missing.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import celpy
from celpy import celtypes
from celpy.adapter import json_to_cel
from celpy.celparser import CELParseError
from celpy.evaluation import CELEvalError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from celpy.evaluation import Result

VALIDATIONS = "x-kubernetes-validations"

#: CEL keywords. A property with one of these names is `__name__` in a rule.
RESERVED = frozenset(
    {
        "true",
        "false",
        "null",
        "in",
        "as",
        "break",
        "const",
        "continue",
        "else",
        "for",
        "function",
        "if",
        "import",
        "let",
        "loop",
        "package",
        "namespace",
        "return",
        "var",
        "void",
        "while",
    }
)

_OLD_SELF = re.compile(r"\boldSelf\b")
_UNDECLARED = re.compile(r"undeclared reference to '([^']+)'")


def escape(name: str) -> str:
    """A property name as a CEL rule spells it, by Kubernetes' escaping rules.

    `namespace` is a CEL keyword, so a rule reads `self.__namespace__`.
    """
    if name in RESERVED:
        return f"__{name}__"
    return (
        name.replace("__", "__underscores__").replace(".", "__dot__").replace("-", "__dash__").replace("/", "__slash__")
    )


def cel_view(schema: Mapping[str, Any], value: Any) -> Any:
    """*value* as a rule sees it: defaults filled, declared properties renamed
    by `escape`.

    The API server applies a schema's defaults before it evaluates CEL, so a
    rule may read a field the manifest never set (Gateway API's
    `backendRefs[].kind`, defaulted to `Service`). Only declared properties
    are renamed: a map's keys (`additionalProperties`) are data, and a rule
    indexes them as `self['key']`, unescaped.
    """
    if isinstance(value, dict):
        properties = cast("Mapping[str, Any]", schema.get("properties") or {})
        defaults = {
            key: cast("Mapping[str, Any]", sub)["default"]
            for key, sub in properties.items()
            if isinstance(sub, dict) and "default" in sub
        }
        items = defaults | cast("dict[str, Any]", value)
        additional = schema.get("additionalProperties")
        out: dict[str, Any] = {}
        for key, child in items.items():
            if key in properties:
                out[escape(key)] = cel_view(cast("Mapping[str, Any]", properties[key]), child)
            elif isinstance(additional, dict):
                out[key] = cel_view(cast("Mapping[str, Any]", additional), child)
            else:
                out[key] = child
        return out
    if isinstance(value, list):
        sub = schema.get("items")
        sub_schema = cast("Mapping[str, Any]", sub) if isinstance(sub, dict) else {}
        return [cel_view(sub_schema, child) for child in cast("list[Any]", value)]
    return value


class _LazyMap(celtypes.MapType):
    """A CEL map that converts a value when an expression first reads it.

    `json_to_cel` of nixlab3's 201 CRDs took 0.62s, for policies that read
    three fields of each. celpy reads a map through `__getitem__` and `get`;
    the keys are real from the start, so `in`, `size` and the macros see them.

    A key named like a CEL keyword is also offered escaped, `__namespace__`
    beside `namespace`. Kubernetes escapes a declared property so, and with
    no schema for a built-in kind, which keys are properties is unknown.
    """

    __slots__ = ("_raw",)

    def __init__(self, raw: Mapping[str, Any]) -> None:
        super().__init__()
        self._raw = dict(raw)
        for key in RESERVED.intersection(raw):
            self._raw.setdefault(f"__{key}__", raw[key])
        dict.update(self, dict.fromkeys(map(celtypes.StringType, self._raw)))

    def __getitem__(self, key: Any) -> Any:
        value = super().__getitem__(key)
        if value is None and key in self._raw:
            value = lazy_cel(self._raw.pop(key))
            dict.__setitem__(self, key, value)
        return value

    def get(self, key: Any, default: Any = None) -> Any:
        if key in self:
            return self[key]
        return super().get(key, default)


def lazy_cel(value: Any) -> Any:
    """`json_to_cel`, with each map a `_LazyMap`."""
    if isinstance(value, dict):
        return _LazyMap(cast("dict[str, Any]", value))
    if isinstance(value, list):
        return celtypes.ListType([lazy_cel(child) for child in cast("list[Any]", value)])
    return json_to_cel(value)


@dataclass(frozen=True, slots=True)
class CelFailure:
    #: JSON pointer into the object, `""` for the object itself.
    path: str
    message: str


@dataclass(slots=True)
class CelResult:
    evaluated: int = 0
    #: Rules not evaluated, by reason: "oldSelf" or "unsupported: <what>".
    skipped: Counter[str] = field(default_factory=Counter)
    failures: list[CelFailure] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Unsupported:
    """An expression with nothing to judge it by."""

    #: "unsupported: syntax", or "unsupported: <the undeclared name>".
    reason: str


@dataclass(slots=True)
class CelChecker:
    _env: celpy.Environment = field(default_factory=celpy.Environment)
    #: Compiled once per rule text. A rule that does not compile maps to its
    #: skip reason.
    _programs: dict[str, celpy.Runner | str] = field(default_factory=dict)

    def _program(self, rule: str) -> celpy.Runner | str:
        found = self._programs.get(rule)
        if found is None:
            try:
                found = self._env.program(self._env.compile(rule))
            except CELParseError:
                found = "unsupported: syntax"
            self._programs[rule] = found
        return found

    def run(self, text: str, activation: Mapping[str, Any]) -> Result | CELEvalError | Unsupported:
        """*text* over *activation*, whose values are CEL values: the result,
        the error it failed with, or why it cannot be judged."""
        program = self._program(text)
        if isinstance(program, str):
            return Unsupported(program)
        try:
            outcome = program.evaluate(dict(activation))
        except CELEvalError as exc:
            outcome = exc
        if isinstance(outcome, CELEvalError):
            undeclared = _UNDECLARED.search(str(outcome))
            if undeclared:
                return Unsupported(f"unsupported: {undeclared[1]}")
        return outcome

    def _evaluate(
        self, rule: Mapping[str, Any], schema: Mapping[str, Any], value: Any, path: str, result: CelResult
    ) -> None:
        text = str(rule.get("rule", ""))
        if _OLD_SELF.search(text):
            result.skipped["oldSelf"] += 1
            return
        outcome = self.run(text, {"self": json_to_cel(cel_view(schema, value))})
        field_path = str(rule.get("fieldPath", "")).replace(".", "/")
        if isinstance(outcome, Unsupported):
            result.skipped[outcome.reason] += 1
            return
        if isinstance(outcome, CELEvalError):
            result.evaluated += 1
            result.failures.append(
                CelFailure(path + field_path, f"rule {text!r} failed to evaluate: {outcome.args[0]}")
            )
            return
        result.evaluated += 1
        if outcome == celtypes.BoolType(False):
            message = str(rule.get("message") or f"failed rule: {text}")
            result.failures.append(CelFailure(path + field_path, message))

    def walk(self, schema: Mapping[str, Any], value: Any, path: str, result: CelResult) -> None:
        """Every rule in *schema* against *value*, then the same for each child."""
        if value is None:
            return
        for rule in cast("list[Mapping[str, Any]]", schema.get(VALIDATIONS) or []):
            self._evaluate(rule, schema, value, path, result)
        if isinstance(value, dict):
            items = cast("dict[str, Any]", value)
            properties = cast("Mapping[str, Any]", schema.get("properties") or {})
            additional = schema.get("additionalProperties")
            for key, child in items.items():
                sub = properties.get(key, additional if isinstance(additional, dict) else None)
                if isinstance(sub, dict):
                    self.walk(cast("Mapping[str, Any]", sub), child, f"{path}/{key}", result)
        elif isinstance(value, list):
            sub = schema.get("items")
            if isinstance(sub, dict):
                for index, child in enumerate(cast("list[Any]", value)):
                    self.walk(cast("Mapping[str, Any]", sub), child, f"{path}/{index}", result)
