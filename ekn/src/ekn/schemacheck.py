"""Check rendered objects against Kubernetes JSON schemas, with no API server.

Three places hold a schema for a kind, and `Catalog` layers them:

- a Kubernetes OpenAPI v3 document, from a live API server or from the
  Kubernetes source tree (`api/openapi-spec/v3`), the same format either way;
- a per-kind JSON schema from yannh/kubernetes-json-schema, when no server
  answers;
- a CustomResourceDefinition in the manifest itself. It wins over every other
  source, because it is what the apply installs.

OpenAPI is not JSON Schema. `to_json_schema` closes the gap the way kubeconform's
strict mode does, so a typo'd field fails here as the API server's strict field
validation fails it.
"""

from __future__ import annotations

import enum
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import jsonschema_rs

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping

#: OpenAPI v3 keeps definitions here; draft 4 resolves `#/definitions/...`.
COMPONENTS_PREFIX = "#/components/schemas/"
DEFINITIONS_PREFIX = "#/definitions/"

DRAFT4 = "http://json-schema.org/draft-04/schema#"

#: Keywords whose value is one schema.
_SCHEMA_KEYWORDS = frozenset({"items", "not", "additionalProperties", "additionalItems"})
#: Keywords whose value is a list of schemas.
_SCHEMA_LIST_KEYWORDS = frozenset({"allOf", "anyOf", "oneOf"})
#: Keywords whose value maps names to schemas.
_SCHEMA_MAP_KEYWORDS = frozenset({"properties", "patternProperties", "definitions"})
#: Prose. Dropped, because the documents are large and the validator never reads it.
_DROPPED = frozenset({"description"})

_RE2 = jsonschema_rs.RegexOptions()

PRESERVE_UNKNOWN = "x-kubernetes-preserve-unknown-fields"
OBJECT_META = "io.k8s.apimachinery.pkg.apis.meta.v1.ObjectMeta"
INT_OR_STRING = "x-kubernetes-int-or-string"


class Origin(enum.Enum):
    """Where a kind's schema came from, in rising precedence."""

    SPEC = "Kubernetes source tree"
    YANNH = "yannh/kubernetes-json-schema"
    SERVER = "API server"
    RENDERED_CRD = "rendered CRD"


@dataclass(frozen=True, slots=True, order=True)
class GroupVersionKind:
    group: str
    version: str
    kind: str

    @classmethod
    def of(cls, obj: Mapping[str, Any]) -> GroupVersionKind:
        group, _, version = str(obj.get("apiVersion", "")).rpartition("/")
        return cls(group, version, str(obj.get("kind", "")))

    @property
    def api_version(self) -> str:
        return f"{self.group}/{self.version}" if self.group else self.version

    def __str__(self) -> str:
        return f"{self.api_version} {self.kind}"


@dataclass(frozen=True, slots=True, order=True)
class ObjectRef:
    gvk: GroupVersionKind
    namespace: str | None
    name: str

    @classmethod
    def of(cls, obj: Mapping[str, Any]) -> ObjectRef:
        metadata = cast("Mapping[str, Any]", obj.get("metadata") or {})
        namespace = metadata.get("namespace")
        return cls(GroupVersionKind.of(obj), str(namespace) if namespace else None, str(metadata.get("name", "")))

    def __str__(self) -> str:
        where = f"{self.namespace}/" if self.namespace else ""
        return f"{self.gvk.kind} {where}{self.name} ({self.gvk.api_version})"


def _with_null(types: Any) -> list[Any]:
    listed = list(cast("list[Any]", types)) if isinstance(types, list) else [types]
    return listed if "null" in listed else [*listed, "null"]


def _single_types(branches: list[Any]) -> list[str] | None:
    """The types of a `oneOf` whose every branch is just `{"type": T}`, else None."""
    types: list[str] = []
    for branch in branches:
        if not isinstance(branch, dict) or set(cast("dict[str, Any]", branch)) != {"type"}:
            return None
        # A branch is already converted, so its type may be a list.
        for name in _with_null(cast("dict[str, Any]", branch)["type"]):
            if name not in types:
                types.append(str(name))
    return types


def to_json_schema(node: Any) -> Any:
    """One OpenAPI schema node as a strict draft-4 JSON schema node.

    - An object with `properties` and no `additionalProperties` gets
      `additionalProperties: false`, unless it says
      `x-kubernetes-preserve-unknown-fields`. This is what catches a typo.
    - Every typed node also accepts `null`. The API server decodes a JSON
      null as the field's zero value, and Helm output carries them
      (`creationTimestamp: null`).
    - int-or-string, spelled `format: int-or-string` in the built-in specs and
      `x-kubernetes-int-or-string` in CRDs, accepts both.
    - A `oneOf` of bare types (Quantity's string-or-number) becomes one type
      list, so a value that is both a string and a pattern match is not
      "valid under more than one".
    - `$ref`s move from `#/components/schemas/` to `#/definitions/`.
    """
    if isinstance(node, list):
        return [to_json_schema(item) for item in cast("list[Any]", node)]
    if not isinstance(node, dict):
        return node
    source = cast("dict[str, Any]", node)
    out = _convert_keywords(source)
    _widen_types(source, out)
    _drop_defaulted_required(source, out)
    if "type" in out:
        types = _with_null(out["type"])
        if (
            "object" in types
            and "properties" in out
            and "additionalProperties" not in out
            and source.get(PRESERVE_UNKNOWN) is not True
        ):
            out["additionalProperties"] = False
        out["type"] = types
    return out


def _convert_keywords(source: dict[str, Any]) -> dict[str, Any]:
    """*source*'s keywords, each subschema converted, prose dropped."""
    out: dict[str, Any] = {}
    for key, value in source.items():
        if key in _DROPPED:
            continue
        if key in _SCHEMA_KEYWORDS and isinstance(value, dict):
            out[key] = to_json_schema(value)
        elif key in _SCHEMA_LIST_KEYWORDS and isinstance(value, list):
            out[key] = [to_json_schema(item) for item in cast("list[Any]", value)]
        elif key in _SCHEMA_MAP_KEYWORDS and isinstance(value, dict):
            out[key] = {name: to_json_schema(sub) for name, sub in cast("dict[str, Any]", value).items()}
        elif key == "$ref" and isinstance(value, str) and value.startswith(COMPONENTS_PREFIX):
            out[key] = DEFINITIONS_PREFIX + value.removeprefix(COMPONENTS_PREFIX)
        else:
            out[key] = value
    return out


def _widen_types(source: dict[str, Any], out: dict[str, Any]) -> None:
    """int-or-string to both types, and a `oneOf` of bare types to a type list."""
    if source.get("format") == "int-or-string" or source.get(INT_OR_STRING) is True:
        for key in ("type", "format", "oneOf", "anyOf"):
            out.pop(key, None)
        out["type"] = ["integer", "string"]
    elif "oneOf" in out and "type" not in out:
        types = _single_types(cast("list[Any]", out["oneOf"]))
        if types is not None:
            del out["oneOf"]
            out["type"] = types


def _drop_defaulted_required(source: dict[str, Any], out: dict[str, Any]) -> None:
    properties = cast("dict[str, Any]", source.get("properties") or {})
    if "required" in out and properties:
        # The API server fills a default before it validates, so a required
        # field with one can never be missing.
        out["required"] = [
            name
            for name in cast("list[str]", out["required"])
            if "default" not in cast("dict[str, Any]", properties.get(name) or {})
        ]
    if out.get("required") == []:
        # Draft 4 requires at least one entry.
        del out["required"]


@dataclass(frozen=True, slots=True)
class Violation:
    obj: ObjectRef
    #: JSON pointer into the object, `""` for the object itself.
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.obj}: {self.path or '/'}: {self.message}"


@dataclass(slots=True)
class Report:
    checked: Counter[Origin] = field(default_factory=Counter)
    #: Objects whose kind no source describes.
    unknown: list[ObjectRef] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


@dataclass(frozen=True, slots=True)
class _Document:
    """One draft-4 document holding many definitions, compiled on first use."""

    schema: dict[str, Any]
    validators: dict[str, jsonschema_rs.Validator] = field(default_factory=dict)

    def validator(self, definition: str) -> jsonschema_rs.Validator:
        found = self.validators.get(definition)
        if found is None:
            found = jsonschema_rs.Draft4Validator(
                {"$schema": DRAFT4, "$ref": DEFINITIONS_PREFIX + definition, "definitions": self.schema["definitions"]},
                validate_formats=False,
                offline=True,
                # Kubernetes patterns are Go RE2, which the regex crate speaks and
                # ECMA 262's `format: regex` in the draft-4 meta-schema refuses
                # (a CRD's `^\PC*$`). The API server compiles them with Go's
                # regexp; this is the nearest engine.
                validate_schema=False,
                pattern_options=_RE2,
            )
            self.validators[definition] = found
        return found


@dataclass(frozen=True, slots=True)
class _Entry:
    origin: Origin
    document: _Document
    definition: str
    #: The schema types `metadata` as a bare object, as a CRD's root does.
    #: The API server still decodes it as ObjectMeta, so `check` does too.
    bare_metadata: bool = False


def _crd_versions(crd: Mapping[str, Any]) -> Iterator[tuple[GroupVersionKind, dict[str, Any]]]:
    spec = cast("Mapping[str, Any]", crd.get("spec") or {})
    group = str(spec.get("group", ""))
    kind = str(cast("Mapping[str, Any]", spec.get("names") or {}).get("kind", ""))
    for version in cast("list[Mapping[str, Any]]", spec.get("versions") or []):
        schema = cast("Mapping[str, Any]", version.get("schema") or {}).get("openAPIV3Schema")
        if isinstance(schema, dict):
            yield GroupVersionKind(group, str(version.get("name", "")), kind), cast("dict[str, Any]", schema)


def _with_object_fields(schema: dict[str, Any]) -> dict[str, Any]:
    """A CRD's root schema, with the fields every object has.

    The API server accepts `apiVersion`, `kind` and `metadata` on any custom
    resource, whether the CRD's schema lists them or not.
    """
    properties = cast("dict[str, Any]", schema.get("properties"))
    if properties is None:
        return schema
    implicit = {"apiVersion": {"type": "string"}, "kind": {"type": "string"}, "metadata": {"type": "object"}}
    return {**schema, "properties": implicit | properties}


def is_crd(obj: Mapping[str, Any]) -> bool:
    return obj.get("kind") == "CustomResourceDefinition" and str(obj.get("apiVersion", "")).startswith(
        "apiextensions.k8s.io/"
    )


@dataclass(slots=True)
class Catalog:
    """Kind to schema. A later `add_*` of a kind replaces an earlier one only
    when its `Origin` ranks at least as high, so the call order does not
    decide which source wins."""

    _entries: dict[GroupVersionKind, _Entry] = field(default_factory=dict)
    #: Kinds a rendered CRD took over from another source.
    overridden: dict[GroupVersionKind, Origin] = field(default_factory=dict)
    #: The first OpenAPI document that defines ObjectMeta.
    _object_meta: _Document | None = None

    def _put(self, gvk: GroupVersionKind, entry: _Entry) -> None:
        held = self._entries.get(gvk)
        if held is not None:
            ranks = list(Origin)
            keep, lose = (held, entry) if ranks.index(held.origin) > ranks.index(entry.origin) else (entry, held)
            if keep.origin is Origin.RENDERED_CRD and lose.origin is not Origin.RENDERED_CRD:
                self.overridden[gvk] = lose.origin
            if keep is held:
                return
        self._entries[gvk] = entry

    def __contains__(self, gvk: object) -> bool:
        return gvk in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def origin(self, gvk: GroupVersionKind) -> Origin | None:
        entry = self._entries.get(gvk)
        return entry.origin if entry else None

    def add_openapi_v3(self, document: Mapping[str, Any], origin: Origin) -> None:
        """Every kind an OpenAPI v3 document defines, by its
        `x-kubernetes-group-version-kind`."""
        components = cast("Mapping[str, Any]", document.get("components") or {})
        schemas = cast("dict[str, Any]", components.get("schemas") or {})
        compiled = _Document({"definitions": to_json_schema({"definitions": schemas})["definitions"]})
        if self._object_meta is None and OBJECT_META in schemas:
            self._object_meta = compiled
        for name, schema in schemas.items():
            for gvk in cast(
                "list[Mapping[str, Any]]",
                cast("Mapping[str, Any]", schema).get("x-kubernetes-group-version-kind") or [],
            ):
                self._put(
                    GroupVersionKind(str(gvk.get("group", "")), str(gvk["version"]), str(gvk["kind"])),
                    _Entry(origin, compiled, name),
                )

    def add_crd(self, crd: Mapping[str, Any], origin: Origin = Origin.RENDERED_CRD) -> None:
        for gvk, schema in _crd_versions(crd):
            document = _Document({"definitions": {"root": to_json_schema(_with_object_fields(schema))}})
            self._put(gvk, _Entry(origin, document, "root", bare_metadata=True))

    def add_schema(self, gvk: GroupVersionKind, schema: dict[str, Any], origin: Origin) -> None:
        """A self-contained JSON schema for one kind, used as it stands."""
        self._put(gvk, _Entry(origin, _Document({"definitions": {"root": schema}}), "root"))

    def validator(self, gvk: GroupVersionKind) -> tuple[Origin, jsonschema_rs.Validator] | None:
        entry = self._entries.get(gvk)
        if entry is None:
            return None
        return entry.origin, entry.document.validator(entry.definition)

    def metadata_validator(self, gvk: GroupVersionKind) -> jsonschema_rs.Validator | None:
        """ObjectMeta's validator, for a kind whose own schema leaves `metadata`
        open. None when the kind's schema covers it, or no OpenAPI document
        has been read."""
        entry = self._entries.get(gvk)
        if entry is None or not entry.bare_metadata or self._object_meta is None:
            return None
        return self._object_meta.validator(OBJECT_META)


def strip_for_check(obj: Mapping[str, Any]) -> dict[str, Any]:
    """*obj* without the parts the API server never sees.

    A `sops:` block is SOPS' own metadata; `ekn` decrypts and drops it before
    any apply, so it is not a field of the object.
    """
    return {key: value for key, value in obj.items() if key != "sops"}


def check(objects: Iterable[Mapping[str, Any]], catalog: Catalog) -> Report:
    report = Report()
    for obj in objects:
        ref = ObjectRef.of(obj)
        found = catalog.validator(ref.gvk)
        if found is None:
            report.unknown.append(ref)
            continue
        origin, validator = found
        report.checked[origin] += 1
        report.violations.extend(
            Violation(ref, "".join(f"/{part}" for part in error.instance_path), error.message)
            for error in validator.iter_errors(strip_for_check(obj))
        )
        metadata = obj.get("metadata")
        meta_validator = catalog.metadata_validator(ref.gvk)
        if meta_validator is not None and isinstance(metadata, dict):
            report.violations.extend(
                Violation(ref, "/metadata" + "".join(f"/{part}" for part in error.instance_path), error.message)
                for error in meta_validator.iter_errors(metadata)
            )
    return report


def add_rendered_crds(catalog: Catalog, objects: Iterable[Mapping[str, Any]]) -> None:
    for obj in objects:
        if is_crd(obj):
            catalog.add_crd(obj)
