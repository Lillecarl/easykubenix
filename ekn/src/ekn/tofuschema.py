"""A JSON Schema for one `config.tf.json`, and the check against it.

Two inputs. `tools/tofuschema` exports OpenTofu's core schema: the blocks
OpenTofu knows without a provider, with hcl-lang's constraints. `tofu
providers schema -json` gives each provider's own. Both are normalised to one
body shape, the providers' bodies are merged over the core ones the way
opentofu-schema's SchemaMerger merges them, and the result is translated into
JSON Schema for HCL's JSON syntax (hashicorp/hcl json/spec.md).

The value rules follow what `tofu validate` accepts, measured against
OpenTofu 1.12: any string holding `${` or `%{` is a template and may stand
for any value; "1e1" converts to a number and " 2" and "0x10" do not; "1"
and "true" convert to a bool and "True" does not; a number or a bool
converts to a string.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import jsonschema_rs

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

MESSAGE = "x-ekn-message"
DEFAULT_REGISTRY = "registry.opentofu.org"
#: Names the `tf` units an object ships, comma-separated. easykubenix's
#: `ekn.lib.tf.unitsAnnotation`; an assertion there refuses an unknown name.
UNITS_ANNOTATION = "ekn.dev/tofu-units"

# `$${` and `%%{` are escapes and stay literal.
_TEMPLATE = r"(^|[^$])\$\{|(^|[^%])%\{"
_NUMBER = r"^-?[0-9]+(\.[0-9]+)?([eE][-+]?[0-9]+)?$|" + _TEMPLATE
_BOOL = r"^(true|false|1|0)$|" + _TEMPLATE

#: Root blocks whose first label names a provider-declared type. A type no
#: provider declares is an error there, not an open body.
_TYPED_BLOCKS = ("resource", "data", "ephemeral")
_PROVIDER_TABLES = {
    "resource": "resource_schemas",
    "data": "data_source_schemas",
    "ephemeral": "ephemeral_resource_schemas",
}
_DEFS_PREFIX = "#/$defs/"
_MESSAGE_KEYWORDS = frozenset({"type", "pattern", "not"})

_READ_ONLY: dict[str, Any] = {"not": {}, MESSAGE: "read-only: the provider computes this attribute"}

Body = dict[str, Any]


@dataclass(frozen=True, slots=True)
class Violation:
    #: JSON pointer into `config.tf.json`, `""` for the document itself.
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path or '/'}: {self.message}"


def provider_address(local: str, source: str | None) -> str:
    """The provider dump's key for a `required_providers` entry."""
    source = (source or f"hashicorp/{local}").lower()
    parts = source.split("/")
    if len(parts) == 2:
        return f"{DEFAULT_REGISTRY}/{source}"
    return source


# --- the provider dump, in the core export's shape ---------------------------


def _provider_attribute(attribute: Mapping[str, Any]) -> dict[str, Any]:
    nested = attribute.get("nested_type")
    if nested is None:
        constraint: dict[str, Any] = {"type": attribute.get("type", "dynamic")}
    else:
        nested = cast("Mapping[str, Any]", nested)
        inner = {
            "object": {
                name: _provider_attribute(a)
                for name, a in cast("Mapping[str, Mapping[str, Any]]", nested.get("attributes", {})).items()
            }
        }
        mode = nested.get("nesting_mode")
        bounds = {"min_items": nested.get("min_items", 0), "max_items": nested.get("max_items", 0)}
        if mode == "list":
            constraint = {"list": inner, **bounds}
        elif mode == "set":
            constraint = {"set": inner, **bounds}
        elif mode == "map":
            constraint = {"map": inner}
        else:
            constraint = inner
    return {
        "required": bool(attribute.get("required")),
        "optional": bool(attribute.get("optional")),
        "computed": bool(attribute.get("computed")),
        "constraint": constraint,
    }


def provider_body(block: Mapping[str, Any]) -> Body:
    """A `tofu providers schema -json` block, as a core-export body."""
    blocks: dict[str, Any] = {}
    for name, nested in cast("Mapping[str, Mapping[str, Any]]", block.get("block_types", {})).items():
        mode = nested.get("nesting_mode")
        blocks[name] = {
            "labels": ["name"] if mode == "map" else [],
            "nesting": "single" if mode in ("single", "group") else mode,
            "min_items": nested.get("min_items", 0),
            "max_items": nested.get("max_items", 0),
            "body": provider_body(cast("Mapping[str, Any]", nested.get("block", {}))),
        }
    return {
        "attributes": {
            name: _provider_attribute(a)
            for name, a in cast("Mapping[str, Mapping[str, Any]]", block.get("attributes", {})).items()
        },
        "blocks": blocks,
    }


# --- JSON Schema --------------------------------------------------------------


def _template_or(native: dict[str, Any], name: str) -> dict[str, Any]:
    # if/then/else rather than anyOf: an error inside `native` is then
    # reported where it is, not as "valid under none of the schemas".
    return {"if": {"type": "string"}, "then": {"pattern": _TEMPLATE}, "else": native, MESSAGE: f"expects {name}"}


def _collection(items: dict[str, Any], min_items: int = 0, max_items: int = 0) -> dict[str, Any]:
    array: dict[str, Any] = {"type": ["array", "null"], "items": items}
    if min_items:
        array["minItems"] = min_items
    if max_items:
        array["maxItems"] = max_items
    return _template_or(array, "a list or a ${...} template")


def cty_value(cty: Any) -> dict[str, Any]:
    """The JSON values `tofu validate` converts to cty type *cty*."""
    if cty == "string":
        return {"type": ["string", "number", "boolean", "null"]}
    if cty == "number":
        return {
            "type": ["number", "string", "null"],
            "pattern": _NUMBER,
            MESSAGE: "expects a number or a ${...} template",
        }
    if cty == "bool":
        return {"type": ["boolean", "string", "null"], "pattern": _BOOL, MESSAGE: "expects a bool or a ${...} template"}
    if not isinstance(cty, list) or not cty:
        return {}
    kind, *args = cast("list[Any]", cty)
    if kind in ("list", "set"):
        return _collection(cty_value(args[0]))
    if kind == "map":
        return _template_or(
            {"type": ["object", "null"], "additionalProperties": cty_value(args[0])}, "a map or a ${...} template"
        )
    if kind == "object":
        members = cast("Mapping[str, Any]", args[0])
        optional = set(cast("list[str]", args[1])) if len(args) > 1 else set()
        native: dict[str, Any] = {
            "type": ["object", "null"],
            "properties": {name: cty_value(member) for name, member in members.items()},
            "additionalProperties": False,
        }
        required = sorted(set(members) - optional)
        if required:
            native["required"] = required
        return _template_or(native, "an object or a ${...} template")
    if kind == "tuple":
        elements = cast("list[Any]", args[0])
        return _template_or(
            {"type": ["array", "null"], "prefixItems": [cty_value(e) for e in elements], "items": False},
            "a tuple or a ${...} template",
        )
    return {}


def _constraint(constraint: Mapping[str, Any]) -> dict[str, Any]:
    if "type" in constraint:
        return cty_value(constraint["type"])
    for kind in ("list", "set"):
        if kind in constraint:
            return _collection(
                _constraint(constraint[kind]), constraint.get("min_items", 0), constraint.get("max_items", 0)
            )
    if "map" in constraint:
        return _template_or(
            {"type": ["object", "null"], "additionalProperties": _constraint(constraint["map"])},
            "a map or a ${...} template",
        )
    if "object" in constraint:
        attributes = cast("Mapping[str, Mapping[str, Any]]", constraint["object"])
        native: dict[str, Any] = {
            "type": ["object", "null"],
            "properties": {name: _attribute(a) for name, a in attributes.items()},
            "additionalProperties": False,
        }
        required = sorted(name for name, a in attributes.items() if a["required"])
        if required:
            native["required"] = required
        return _template_or(native, "an object or a ${...} template")
    if "tuple" in constraint:
        return _template_or(
            {"type": ["array", "null"], "prefixItems": [_constraint(e) for e in constraint["tuple"]], "items": False},
            "a tuple or a ${...} template",
        )
    if "one_of" in constraint:
        return {"anyOf": [_constraint(option) for option in constraint["one_of"]]}
    if "keyword" in constraint or "reference" in constraint or "type_declaration" in constraint:
        # Native syntax that tofu parses out of a JSON string.
        return {"type": "string"}
    msg = f"unknown constraint {constraint!r}"
    raise ValueError(msg)


def _attribute(attribute: Mapping[str, Any]) -> dict[str, Any]:
    if attribute["computed"] and not attribute["optional"] and not attribute["required"]:
        return _READ_ONLY
    return _constraint(attribute["constraint"])


def _repeated(item: dict[str, Any], min_items: int = 0, max_items: int = 0) -> dict[str, Any]:
    """One object, or an array of them: HCL's JSON form of a body and of each label level."""
    array: dict[str, Any] = {"type": "array", "items": item}
    if min_items:
        array["minItems"] = min_items
    if max_items:
        array["maxItems"] = max_items
    return {"if": {"type": "array"}, "then": array, "else": item}


def _merged(base: Body, dependent: Body | None) -> tuple[dict[str, Any], dict[str, Any], Any]:
    """*dependent* over *base*, as hcl-lang merges a DependentBody over a Body."""
    attributes: dict[str, Any] = {}
    blocks: dict[str, Any] = {}
    any_attribute = None
    for part in [base] if dependent is None else [base, dependent]:
        attributes.update(part.get("attributes", {}))
        blocks.update(part.get("blocks", {}))
        any_attribute = part.get("any_attribute", any_attribute)
    return attributes, blocks, any_attribute


@dataclass
class _Builder:
    defs: dict[str, Any] = field(default_factory=dict)
    #: First-label bodies for a block, by the block's id, and the message for
    #: a first label outside them.
    typed: dict[int, tuple[Mapping[str, Body], str]] = field(default_factory=dict)
    _names: dict[tuple[int, int, bool], str] = field(default_factory=dict)
    # Keeps every body alive, so an id() in `_names` is never reused.
    _held: list[Any] = field(default_factory=list)

    def body_ref(self, base: Body, dependent: Body | None, *, dynamic: bool) -> dict[str, Any]:
        # A nested body is reached twice when dynamic blocks are allowed --
        # as itself and as a dynamic block's content -- so inlining it would
        # double the output at every nesting level.
        key = (id(base), id(dependent), dynamic)
        name = self._names.get(key)
        if name is None:
            name = f"b{len(self._names)}"
            self._names[key] = name
            self._held.append((base, dependent))
            self.defs[name] = self.body(base, dependent, dynamic=dynamic)
        return {"$ref": _DEFS_PREFIX + name}

    def _dynamic(self, block: Mapping[str, Any]) -> dict[str, Any]:
        """A `dynamic` block generating *block*: its content is *block*'s body."""
        return _repeated(
            {
                "type": "object",
                "properties": {
                    "for_each": {},
                    "iterator": {"type": "string"},
                    "labels": {},
                    "content": self.body_ref(block["body"], None, dynamic=True),
                },
                "required": ["for_each", "content"],
                "additionalProperties": False,
            }
        )

    def body(self, base: Body, dependent: Body | None = None, *, dynamic: bool) -> dict[str, Any]:
        attributes, blocks, any_attribute = _merged(base, dependent)
        dynamic = dynamic or bool(base.get("dynamic_blocks"))

        properties: dict[str, Any] = {"//": {}}
        required = sorted(name for name, a in attributes.items() if a["required"])
        for name, attribute in attributes.items():
            properties[name] = _attribute(attribute)
        # An attribute shadows a block of its name: opentofu-schema offers a
        # list(object) attribute as a block too, for completion only.
        own_blocks = {name: block for name, block in blocks.items() if name not in attributes}
        for name, block in own_blocks.items():
            properties[name] = self.block(block, dynamic=dynamic)
        required_blocks = sorted(name for name, block in own_blocks.items() if block.get("min_items"))
        dynamic_blocks = {name: self._dynamic(block) for name, block in own_blocks.items()} if dynamic else {}
        if base.get("count"):
            properties["count"] = cty_value("number")
        if base.get("for_each"):
            properties["for_each"] = {}
        if dynamic_blocks:
            properties["dynamic"] = {"type": "object", "properties": dynamic_blocks, "additionalProperties": False}

        out: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "additionalProperties": False if any_attribute is None else _attribute(any_attribute),
        }
        if required:
            out["required"] = required
        if required_blocks:
            # A dynamic block may produce the required ones; its count is unknown.
            out["if"] = {"not": {"required": ["dynamic"]}}
            out["then"] = {"required": required_blocks}
        return out

    def block(self, block: Mapping[str, Any], *, dynamic: bool) -> dict[str, Any]:
        """What a block's name maps to: one JSON level per label, then the body.

        A typed block's first label picks the dependent body merged under the
        block's own, and a first label with no dependent body is refused.
        """
        if id(block) in self.typed:
            typed, unknown = self.typed[id(block)]
        elif "dependent" in block:
            typed, unknown = block["dependent"], "unknown type for this block"
        else:
            typed, unknown = None, ""
        labels = cast("list[str]", block["labels"])
        nesting = block["nesting"]
        min_items = block.get("min_items", 0)
        max_items = 1 if nesting == "single" else block.get("max_items", 0) if nesting in ("list", "set") else 0

        def level(depth: int, dependent: Body | None) -> dict[str, Any]:
            if depth == len(labels):
                ref = self.body_ref(block["body"], dependent, dynamic=dynamic)
                return _repeated(ref, min_items, max_items) if dependent is None else _repeated(ref)
            return _repeated({"type": "object", "additionalProperties": level(depth + 1, dependent)})

        if typed is None or not labels:
            return level(0, None)
        return _repeated(
            {
                "type": "object",
                "properties": {label: level(1, dependent) for label, dependent in typed.items()},
                "additionalProperties": {"not": {}, MESSAGE: unknown},
            }
        )


def _typed_bodies(kind: str, providers: Mapping[str, Mapping[str, Any]], owners: Mapping[str, str]) -> dict[str, Body]:
    """Every provider's *kind* types. Where two declare one type, its owner wins.

    A type belongs to the provider whose local name is its prefix, as in
    opentofu-schema's `typeBelongsToProvider`; any other provider declaring
    it is reachable only through an explicit `provider` argument.
    """
    found: dict[str, tuple[bool, Body]] = {}
    for local, entry in providers.items():
        table = cast("Mapping[str, Mapping[str, Any]]", entry.get(_PROVIDER_TABLES[kind], {}))
        for name, schema in table.items():
            owned = owners.get(name) == local
            if name not in found or (owned and not found[name][0]):
                found[name] = (owned, provider_body(cast("Mapping[str, Any]", schema.get("block", {}))))
    return {name: body for name, (_, body) in found.items()}


def _owner(type_name: str, locals_: list[str]) -> str | None:
    matches = [local for local in locals_ if type_name == local or type_name.startswith(f"{local}_")]
    return max(matches, key=len, default=None)


def build_schema(
    core: Mapping[str, Any], dump: Mapping[str, Any], required_providers: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    """The JSON Schema for a `config.tf.json` with *required_providers*.

    *core* is `tools/tofuschema`'s export and *dump* is `tofu providers schema
    -json`. A required provider missing from *dump* is an error: the schema
    would refuse every type it declares.
    """
    schemas = cast("Mapping[str, Mapping[str, Any]]", dump.get("provider_schemas", {}))
    providers: dict[str, Mapping[str, Any]] = {}
    for local, requirement in required_providers.items():
        address = provider_address(local, cast("str | None", requirement.get("source")))
        if address not in schemas:
            msg = f"required_providers.{local}: {address} is not in the provider schemas"
            raise ValueError(msg)
        providers[local] = schemas[address]
    locals_ = sorted(providers)

    builder = _Builder()
    blocks = _with_declared_inputs(cast("Mapping[str, Mapping[str, Any]]", core["blocks"]))

    def provider_types(kind: str) -> tuple[Mapping[str, Body], str]:
        names = {
            type_name
            for entry in providers.values()
            for type_name in cast("Mapping[str, Any]", entry.get(_PROVIDER_TABLES[kind], {}))
        }
        owners = {type_name: owner for type_name in names if (owner := _owner(type_name, locals_))}
        return _typed_bodies(kind, providers, owners), f"no provider in required_providers declares this {kind} type"

    for kind in _TYPED_BLOCKS:
        if kind in blocks:
            builder.typed[id(blocks[kind])] = provider_types(kind)
    check_data = blocks.get("check", {}).get("body", {}).get("blocks", {}).get("data")
    if check_data is not None:
        builder.typed[id(check_data)] = provider_types("data")
    builder.typed[id(blocks["provider"])] = (
        {
            local: provider_body(cast("Mapping[str, Any]", entry.get("provider", {}).get("block", {})))
            for local, entry in providers.items()
        },
        "not a provider in required_providers",
    )
    root = builder.body({**core, "blocks": blocks}, dynamic=False)
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", **root, "$defs": builder.defs}


_ANY_OPTIONAL: dict[str, Any] = {
    "required": False,
    "optional": True,
    "computed": False,
    "constraint": {"type": "dynamic"},
}


def _with_declared_inputs(blocks: Mapping[str, Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    """*blocks* with what OpenTofu knows only at run time, left open.

    A variable's `default` comes from a dependent body per declared variable,
    typed by its `type`; here it takes any value. A module call's inputs come
    from the called module's variables, which no schema of this module holds,
    so a module block takes any argument.
    """
    out = dict(blocks)
    variable = out["variable"]
    body = variable["body"]
    out["variable"] = {**variable, "body": {**body, "attributes": {**body["attributes"], "default": _ANY_OPTIONAL}}}
    module = out["module"]
    out["module"] = {**module, "body": {**module["body"], "any_attribute": _ANY_OPTIONAL}}
    return out


def shipped_units(objects: Iterable[Mapping[str, Any]]) -> set[str]:
    """The `tf` units *objects* name in their `UNITS_ANNOTATION`."""
    found: set[str] = set()
    for obj in objects:
        annotations = obj.get("metadata", {}).get("annotations") or {}
        value = annotations.get(UNITS_ANNOTATION) if isinstance(annotations, dict) else None
        if isinstance(value, str):
            found.update(name for name in value.split(",") if name)
    return found


# --- the check ----------------------------------------------------------------


def _schema_nodes(schema: Mapping[str, Any], schema_path: list[str | int]) -> list[Any]:
    """The schema node at each step of *schema_path*, following `$ref`s."""
    defs = cast("Mapping[str, Any]", schema.get("$defs", {}))
    node: Any = schema
    nodes: list[Any] = [node]
    for segment in schema_path:
        if segment == "$ref" and isinstance(node, dict) and "$ref" in node:
            node = defs[cast("str", node["$ref"]).removeprefix(_DEFS_PREFIX)]
        elif isinstance(node, dict) and segment in node:
            node = cast("dict[Any, Any]", node)[segment]
        elif isinstance(node, list) and isinstance(segment, int):
            node = cast("list[Any]", node)[segment]
        else:
            return nodes
        nodes.append(node)
    return nodes


def _message(schema: Mapping[str, Any], error: jsonschema_rs.ValidationError) -> str:
    path = list(error.schema_path)
    # Only a value of the wrong kind; a missing or extra property inside an
    # object is reported as jsonschema words it.
    if not path or path[-1] not in _MESSAGE_KEYWORDS:
        return error.message
    nodes = _schema_nodes(schema, path[:-1])
    # The node holding the failed keyword, and for a failed `then` or `else`
    # branch the `if` node above it.
    candidates = [nodes[-1]]
    if len(path) >= 2 and path[-2] in ("then", "else") and len(nodes) >= 2:
        candidates.append(nodes[-2])
    for node in candidates:
        if isinstance(node, dict) and MESSAGE in node:
            return f"{error.instance!r}: {node[MESSAGE]}"
    return error.message


def validator(schema: dict[str, Any]) -> jsonschema_rs.Validator:
    return jsonschema_rs.Draft202012Validator(schema, validate_formats=False, offline=True)


def check(config: Mapping[str, Any], schema: dict[str, Any]) -> list[Violation]:
    # dict, not set: the order jsonschema reports in is kept, and one value
    # failing two branches of the same `if` is one violation.
    found = {
        Violation("".join(f"/{part}" for part in error.instance_path), _message(schema, error)): None
        for error in validator(schema).iter_errors(config)
    }
    return list(found)
