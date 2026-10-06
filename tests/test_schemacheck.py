from __future__ import annotations

from typing import Any

import pytest

from ekn.schemacheck import Catalog, GroupVersionKind, Origin, add_rendered_crds, check, to_json_schema
from ekn.schemasource import yannh_name

# The shapes Kubernetes' own `api/openapi-spec/v3` uses: refs wrapped in
# `allOf`, IntOrString as `format: int-or-string` over a `oneOf`, Quantity as a
# bare `oneOf`.
SPEC: dict[str, Any] = {
    "components": {
        "schemas": {
            "io.k8s.api.core.v1.Service": {
                "type": "object",
                "properties": {
                    "apiVersion": {"type": "string"},
                    "kind": {"type": "string"},
                    "metadata": {
                        "allOf": [{"$ref": "#/components/schemas/io.k8s.apimachinery.pkg.apis.meta.v1.ObjectMeta"}],
                        "default": {},
                    },
                    "spec": {"allOf": [{"$ref": "#/components/schemas/io.k8s.api.core.v1.ServiceSpec"}]},
                },
                "x-kubernetes-group-version-kind": [{"group": "", "kind": "Service", "version": "v1"}],
            },
            "io.k8s.api.core.v1.ServiceSpec": {
                "type": "object",
                "properties": {
                    "ports": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["port"],
                            "properties": {
                                "port": {"type": "integer", "format": "int32"},
                                "targetPort": {
                                    "allOf": [
                                        {"$ref": "#/components/schemas/io.k8s.apimachinery.pkg.util.intstr.IntOrString"}
                                    ]
                                },
                            },
                        },
                    },
                    "limit": {"$ref": "#/components/schemas/io.k8s.apimachinery.pkg.api.resource.Quantity"},
                },
            },
            "io.k8s.apimachinery.pkg.apis.meta.v1.ObjectMeta": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "namespace": {"type": "string"},
                    "creationTimestamp": {"type": "string", "format": "date-time"},
                    "labels": {"type": "object", "additionalProperties": {"type": "string"}},
                },
            },
            "io.k8s.apimachinery.pkg.util.intstr.IntOrString": {
                "format": "int-or-string",
                "oneOf": [{"type": "integer"}, {"type": "string"}],
            },
            "io.k8s.apimachinery.pkg.api.resource.Quantity": {"oneOf": [{"type": "string"}, {"type": "number"}]},
        }
    }
}


def _crd(properties: dict[str, Any], *, preserve: bool = False) -> dict[str, Any]:
    spec: dict[str, Any] = {"type": "object", "properties": properties}
    if preserve:
        spec["x-kubernetes-preserve-unknown-fields"] = True
    return {
        "apiVersion": "apiextensions.k8s.io/v1",
        "kind": "CustomResourceDefinition",
        "metadata": {"name": "widgets.example.com"},
        "spec": {
            "group": "example.com",
            "names": {"kind": "Widget", "plural": "widgets"},
            "scope": "Namespaced",
            "versions": [
                {
                    "name": "v1",
                    "served": True,
                    "storage": True,
                    "schema": {
                        "openAPIV3Schema": {
                            "type": "object",
                            "properties": {
                                "apiVersion": {"type": "string"},
                                "kind": {"type": "string"},
                                "metadata": {"type": "object"},
                                "spec": spec,
                            },
                        }
                    },
                }
            ],
        },
    }


def _service(port: dict[str, Any], **spec: Any) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "web", "namespace": "default"},
        "spec": {"ports": [port], **spec},
    }


def _widget(spec: dict[str, Any]) -> dict[str, Any]:
    return {"apiVersion": "example.com/v1", "kind": "Widget", "metadata": {"name": "w"}, "spec": spec}


@pytest.fixture
def catalog() -> Catalog:
    catalog = Catalog()
    catalog.add_openapi_v3(SPEC, Origin.SPEC)
    return catalog


@pytest.mark.parametrize("target", [8080, "http"])
def test_int_or_string_takes_both(catalog: Catalog, target: object) -> None:
    report = check([_service({"port": 80, "targetPort": target})], catalog)
    assert report.ok, report.violations
    assert report.checked == {Origin.SPEC: 1}


def test_int_or_string_refuses_a_list(catalog: Catalog) -> None:
    report = check([_service({"port": 80, "targetPort": [1]})], catalog)
    assert [v.path for v in report.violations] == ["/spec/ports/0/targetPort"]


@pytest.mark.parametrize("limit", ["500m", 2, 1.5])
def test_quantity_takes_string_and_number(catalog: Catalog, limit: object) -> None:
    assert check([_service({"port": 80}, limit=limit)], catalog).ok


def test_a_typo_is_a_violation(catalog: Catalog) -> None:
    report = check([_service({"port": 80, "targetprot": 8080})], catalog)
    assert [v.path for v in report.violations] == ["/spec/ports/0"]
    assert "targetprot" in report.violations[0].message


def test_a_wrong_type_is_a_violation(catalog: Catalog) -> None:
    report = check([_service({"port": "eighty"})], catalog)
    assert [v.path for v in report.violations] == ["/spec/ports/0/port"]


def test_a_missing_required_field_is_a_violation(catalog: Catalog) -> None:
    report = check([_service({"targetPort": 80})], catalog)
    assert [v.path for v in report.violations] == ["/spec/ports/0"]


def test_null_is_a_zero_value(catalog: Catalog) -> None:
    obj = _service({"port": 80})
    obj["metadata"]["creationTimestamp"] = None
    assert check([obj], catalog).ok


def test_a_free_map_stays_free(catalog: Catalog) -> None:
    obj = _service({"port": 80})
    obj["metadata"]["labels"] = {"any.key/at-all": "x"}
    assert check([obj], catalog).ok


def test_a_sops_block_is_not_a_field(catalog: Catalog) -> None:
    obj = _service({"port": 80})
    obj["sops"] = {"version": "3.9"}
    assert check([obj], catalog).ok


def test_a_kind_without_a_schema_is_reported_not_failed(catalog: Catalog) -> None:
    report = check([_widget({"size": 1})], catalog)
    assert report.ok
    assert [str(ref.gvk) for ref in report.unknown] == ["example.com/v1 Widget"]


def test_a_rendered_crd_checks_its_custom_resources(catalog: Catalog) -> None:
    crd = _crd({"size": {"type": "integer"}})
    add_rendered_crds(catalog, [crd])
    assert check([crd, _widget({"size": 1})], catalog).ok
    report = check([_widget({"size": "big"})], catalog)
    assert [v.path for v in report.violations] == ["/spec/size"]
    report = check([_widget({"sise": 1})], catalog)
    assert [v.path for v in report.violations] == ["/spec"]


def test_preserve_unknown_fields_keeps_an_object_open(catalog: Catalog) -> None:
    add_rendered_crds(catalog, [_crd({"size": {"type": "integer"}}, preserve=True)])
    assert check([_widget({"anything": {"goes": True}})], catalog).ok


def test_crd_int_or_string(catalog: Catalog) -> None:
    add_rendered_crds(
        catalog,
        [_crd({"port": {"x-kubernetes-int-or-string": True, "anyOf": [{"type": "integer"}, {"type": "string"}]}})],
    )
    assert check([_widget({"port": 1}), _widget({"port": "http"})], catalog).ok
    assert not check([_widget({"port": True})], catalog).ok


def test_a_crd_root_without_object_fields_still_takes_them(catalog: Catalog) -> None:
    crd = _crd({"size": {"type": "integer"}})
    root = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
    root["properties"] = {"spec": root["properties"]["spec"]}
    add_rendered_crds(catalog, [crd])
    assert check([_widget({"size": 1})], catalog).ok
    obj = _widget({"size": 1})
    obj["extra"] = 1
    assert [v.path for v in check([obj], catalog).violations] == [""]


def test_a_required_field_with_a_default_may_be_absent(catalog: Catalog) -> None:
    crd = _crd({"size": {"type": "integer", "default": 1}, "name": {"type": "string"}})
    crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]["required"] = ["size", "name"]
    add_rendered_crds(catalog, [crd])
    assert check([_widget({"name": "n"})], catalog).ok
    report = check([_widget({"size": 2})], catalog)
    assert [v.path for v in report.violations] == ["/spec"]
    assert "name" in report.violations[0].message


def test_an_re2_pattern(catalog: Catalog) -> None:
    add_rendered_crds(catalog, [_crd({"value": {"type": "string", "pattern": r"^\PC*$"}})])
    assert check([_widget({"value": "plain text"})], catalog).ok
    report = check([_widget({"value": "bell \x07"})], catalog)
    assert [v.path for v in report.violations] == ["/spec/value"]


def test_a_rendered_crd_wins_over_the_server() -> None:
    gvk = GroupVersionKind("example.com", "v1", "Widget")
    old = to_json_schema(
        {"type": "object", "properties": {"spec": {"type": "object", "properties": {"old": {"type": "string"}}}}}
    )
    for order in ("server first", "crd first"):
        catalog = Catalog()
        if order == "server first":
            catalog.add_schema(gvk, old, Origin.SERVER)
            add_rendered_crds(catalog, [_crd({"size": {"type": "integer"}})])
        else:
            add_rendered_crds(catalog, [_crd({"size": {"type": "integer"}})])
            catalog.add_schema(gvk, old, Origin.SERVER)
        assert catalog.origin(gvk) is Origin.RENDERED_CRD, order
        assert check([_widget({"size": 1})], catalog).ok, order
        assert not check([_widget({"old": "x"})], catalog).ok, order
        assert catalog.overridden == {gvk: Origin.SERVER}, order


def test_a_property_named_like_a_keyword_is_a_property() -> None:
    schema = to_json_schema(
        {"type": "object", "properties": {"type": {"type": "string"}, "description": {"type": "string"}}}
    )
    assert set(schema["properties"]) == {"type", "description"}
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize(
    ("gvk", "name"),
    [
        (GroupVersionKind("", "v1", "Service"), "service-v1"),
        (GroupVersionKind("apps", "v1", "Deployment"), "deployment-apps-v1"),
        (GroupVersionKind("networking.k8s.io", "v1", "Ingress"), "ingress-networking-v1"),
    ],
)
def test_yannh_name(gvk: GroupVersionKind, name: str) -> None:
    assert yannh_name(gvk) == name
