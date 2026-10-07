from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import celpy
import httpx
import kr8s
import pytest
from celpy.adapter import json_to_cel
from kr8s.asyncio import Api
from kr8s.asyncio.objects import APIObject

from ekn.schemacel import lazy_cel
from ekn.schemacheck import Catalog, Origin
from ekn.schemapolicy import check_policies
from ekn.schemasource import load_installed


def _post(group: str, kind: str) -> dict[str, Any]:
    return {
        "post": {
            "x-kubernetes-action": "post",
            "x-kubernetes-group-version-kind": {"group": group, "version": "v1", "kind": kind},
        }
    }


@pytest.fixture
def catalog() -> Catalog:
    catalog = Catalog()
    catalog.add_openapi_v3(
        {
            "paths": {
                "/api/v1/namespaces/{namespace}/services": _post("", "Service"),
                "/api/v1/namespaces": _post("", "Namespace"),
                "/apis/apiextensions.k8s.io/v1/customresourcedefinitions": _post(
                    "apiextensions.k8s.io", "CustomResourceDefinition"
                ),
            }
        },
        Origin.SPEC,
    )
    return catalog


def _rule(group: str, resource: str, operations: list[str] | None = None) -> dict[str, Any]:
    return {
        "apiGroups": [group],
        "apiVersions": ["v1"],
        "operations": operations or ["CREATE", "UPDATE"],
        "resources": [resource],
    }


def _policy(
    validations: list[dict[str, Any]], *, rules: list[dict[str, Any]] | None = None, **spec: Any
) -> dict[str, Any]:
    return {
        "apiVersion": "admissionregistration.k8s.io/v1",
        "kind": "ValidatingAdmissionPolicy",
        "metadata": {"name": "p"},
        "spec": {
            "matchConstraints": {"resourceRules": rules or [_rule("", "services")]},
            "validations": validations,
            **spec,
        },
    }


def _binding(actions: list[str] | None = None, **spec: Any) -> dict[str, Any]:
    return {
        "apiVersion": "admissionregistration.k8s.io/v1",
        "kind": "ValidatingAdmissionPolicyBinding",
        "metadata": {"name": "b"},
        "spec": {"policyName": "p", "validationActions": actions or ["Deny"], **spec},
    }


def _service(name: str = "web", namespace: str = "default", **labels: str) -> dict[str, Any]:
    return {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": namespace, "labels": labels}}


def _namespace(name: str, **labels: str) -> dict[str, Any]:
    return {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name, "labels": labels}}


NAMED_WEB = {"expression": "object.metadata.name == 'web'", "message": "must be web"}


def test_a_false_validation_denies(catalog: Catalog) -> None:
    result = check_policies([_policy([NAMED_WEB]), _binding(), _service("web"), _service("api")], catalog)
    assert result.admitted == 2
    assert result.evaluated == 2
    assert [(v.obj.name, v.message) for v in result.violations] == [
        ("api", "ValidatingAdmissionPolicy 'p' with binding 'b' denied: must be web")
    ]


def test_a_policy_without_a_binding_is_inert(catalog: Catalog) -> None:
    result = check_policies([_policy([NAMED_WEB]), _service("api")], catalog)
    assert result.admitted == 0
    assert not result.violations


def test_a_binding_without_its_policy_is_undecided(catalog: Catalog) -> None:
    result = check_policies([_binding(), _service("api")], catalog)
    assert result.undecided == {"binding's policy not in the render": 1}


def test_a_resource_the_rules_do_not_name_is_not_admitted(catalog: Catalog) -> None:
    policy = _policy([NAMED_WEB], rules=[_rule("", "configmaps")])
    assert check_policies([policy, _binding(), _service("api")], catalog).admitted == 0


def test_a_delete_only_rule_never_sees_an_apply(catalog: Catalog) -> None:
    policy = _policy([NAMED_WEB], rules=[_rule("", "services", ["DELETE"])])
    assert check_policies([policy, _binding(), _service("api")], catalog).admitted == 0


def test_the_binding_narrows_the_policy(catalog: Catalog) -> None:
    binding = _binding(matchResources={"objectSelector": {"matchLabels": {"checked": "yes"}}})
    objects = [_policy([NAMED_WEB]), binding, _service("api"), _service("other", checked="yes")]
    result = check_policies(objects, catalog)
    assert [v.obj.name for v in result.violations] == ["other"]


def test_exclude_resource_rules(catalog: Catalog) -> None:
    policy = _policy([NAMED_WEB])
    policy["spec"]["matchConstraints"]["excludeResourceRules"] = [_rule("", "services") | {"resourceNames": ["api"]}]
    result = check_policies([policy, _binding(), _service("api"), _service("db")], catalog)
    assert [v.obj.name for v in result.violations] == ["db"]


def test_warn_is_a_warning_not_a_violation(catalog: Catalog) -> None:
    result = check_policies([_policy([NAMED_WEB]), _binding(["Warn"]), _service("api")], catalog)
    assert not result.violations
    assert [v.obj.name for v in result.warnings] == ["api"]


def test_an_error_is_a_violation_under_fail(catalog: Catalog) -> None:
    error = {"expression": "object.spec.ports.size() > 0"}
    result = check_policies([_policy([error]), _binding(), _service()], catalog)
    assert len(result.violations) == 1
    assert "failed to evaluate" in result.violations[0].message


def test_an_error_passes_under_ignore(catalog: Catalog) -> None:
    error = {"expression": "object.spec.ports.size() > 0"}
    result = check_policies([_policy([error], failurePolicy="Ignore"), _binding(), _service()], catalog)
    assert not result.violations
    assert result.skipped == {"error under failurePolicy: Ignore": 1}


def test_match_conditions_gate_the_validations(catalog: Catalog) -> None:
    conditions = [{"name": "team", "expression": "has(object.metadata.labels.team)"}]
    objects = [_policy([NAMED_WEB], matchConditions=conditions), _binding(), _service("api"), _service("db", team="x")]
    result = check_policies(objects, catalog)
    assert result.admitted == 1
    assert [v.obj.name for v in result.violations] == ["db"]


def test_variables(catalog: Catalog) -> None:
    variables = [{"name": "name", "expression": "object.metadata.name"}]
    validation = {"expression": "variables.name == 'web'", "message": "must be web"}
    result = check_policies([_policy([validation], variables=variables), _binding(), _service("api")], catalog)
    assert [v.obj.name for v in result.violations] == ["api"]


def test_a_variable_that_cannot_be_judged_skips_its_readers(catalog: Catalog) -> None:
    variables = [{"name": "user", "expression": "request.userInfo.username"}]
    validations = [{"expression": "variables.user != ''"}, NAMED_WEB]
    result = check_policies([_policy(validations, variables=variables), _binding(), _service("api")], catalog)
    assert result.skipped == {"unsupported: request": 1}
    assert len(result.violations) == 1


@pytest.mark.parametrize(
    ("expression", "reason"),
    [
        ("request.userInfo.username != ''", "unsupported: request"),
        (
            "authorizer.serviceAccount('a', 'b').group('').resource('r').check('get').allowed()",
            "unsupported: authorizer",
        ),
    ],
)
def test_what_a_render_cannot_know_is_skipped(catalog: Catalog, expression: str, reason: str) -> None:
    result = check_policies([_policy([{"expression": expression}]), _binding(), _service()], catalog)
    assert not result.violations
    assert result.skipped == {reason: 1}


def test_a_create_has_no_old_object(catalog: Catalog) -> None:
    validation = {"expression": "oldObject == null || oldObject.metadata.name == object.metadata.name"}
    result = check_policies([_policy([validation]), _binding(), _service()], catalog)
    assert result.evaluated == 1
    assert not result.violations


def test_an_update_only_policy_cannot_read_the_old_object(catalog: Catalog) -> None:
    validations = [{"expression": "oldObject.metadata.name == object.metadata.name"}, NAMED_WEB]
    policy = _policy(validations, rules=[_rule("", "services", ["UPDATE"])])
    result = check_policies([policy, _binding(), _service("api")], catalog)
    assert result.skipped == {"unsupported: oldObject": 1}
    assert len(result.violations) == 1


def test_namespace_selector_reads_the_rendered_namespace(catalog: Catalog) -> None:
    binding = _binding(matchResources={"namespaceSelector": {"matchLabels": {"strict": "yes"}}})
    objects = [
        _policy([NAMED_WEB]),
        binding,
        _namespace("lax"),
        _namespace("strict", strict="yes"),
        _service("api", "lax"),
        _service("api", "strict"),
        _service("api", "elsewhere"),
    ]
    result = check_policies(objects, catalog)
    assert [v.obj.namespace for v in result.violations] == ["strict"]
    assert result.undecided == {"namespace not in the render": 1}


def test_namespace_object(catalog: Catalog) -> None:
    validation = {"expression": "namespaceObject.metadata.labels.tier == 'prod'", "message": "prod only"}
    objects = [
        _policy([validation]),
        _binding(),
        _namespace("a", tier="dev"),
        _service("web", "a"),
        _service("web", "b"),
    ]
    result = check_policies(objects, catalog)
    assert [v.obj.namespace for v in result.violations] == ["a"]
    assert result.skipped == {"unsupported: namespaceObject": 1}


def test_message_expression(catalog: Catalog) -> None:
    validation = NAMED_WEB | {"messageExpression": "'name ' + object.metadata.name + ' is not web'"}
    result = check_policies([_policy([validation]), _binding(), _service("api")], catalog)
    assert result.violations[0].message.endswith("denied: name api is not web")


def test_a_property_named_like_a_keyword_reads_both_ways(catalog: Catalog) -> None:
    validations = [
        {"expression": "object.metadata.namespace == 'default'"},
        {"expression": "object.metadata.__namespace__ == 'default'"},
    ]
    result = check_policies([_policy(validations), _binding(), _service()], catalog)
    assert result.evaluated == 2
    assert not result.violations


def test_a_resource_with_no_known_name_is_undecided(catalog: Catalog) -> None:
    policy = _policy([NAMED_WEB], rules=[_rule("example.com", "widgets")])
    widget = {"apiVersion": "example.com/v1", "kind": "Widget", "metadata": {"name": "w"}}
    result = check_policies([policy, _binding(), widget], catalog)
    assert result.undecided == {"no resource name for example.com/v1 Widget": 1}
    policy = _policy([NAMED_WEB], rules=[_rule("example.com", "*")])
    assert len(check_policies([policy, _binding(), widget], catalog).violations) == 1


# The shape of Gateway API's safe-upgrades policy, which nixlab3 renders.
SAFE_UPGRADES = [
    {
        "expression": "object.spec.group != 'gateway.networking.k8s.io' || oldObject == null || false",
        "message": "experimental over standard",
    },
    {
        "expression": "object.spec.group != 'gateway.networking.k8s.io' || (has(object.metadata.annotations) "
        "&& object.metadata.annotations.exists(k, k == 'gateway.networking.k8s.io/bundle-version') "
        "&& !matches(object.metadata.annotations['gateway.networking.k8s.io/bundle-version'], 'v1.[0-4].\\\\d+'))",
        "message": "before v1.5.0",
    },
]


@pytest.mark.parametrize(("version", "denied"), [("v1.5.0", []), ("v1.2.0", ["before v1.5.0"])])
def test_gateway_safe_upgrades(catalog: Catalog, version: str, denied: list[str]) -> None:
    policy = _policy(SAFE_UPGRADES, rules=[_rule("apiextensions.k8s.io", "*")])
    crd = {
        "apiVersion": "apiextensions.k8s.io/v1",
        "kind": "CustomResourceDefinition",
        "metadata": {
            "name": "gateways.gateway.networking.k8s.io",
            "annotations": {"gateway.networking.k8s.io/bundle-version": version},
        },
        "spec": {"group": "gateway.networking.k8s.io"},
    }
    result = check_policies([policy, _binding(), crd], catalog)
    assert result.evaluated == 2
    assert [v.message.rpartition(": ")[2] for v in result.violations] == denied


LAZY_DOCUMENT = {"a": {"b": [{"c": 1}, {"c": 2}], "m": {"k1": "v", "k2": None}}, "s": "x"}


@pytest.mark.parametrize(
    "expression",
    [
        "object.a.b[1].c == 2",
        "has(object.a.m)",
        "has(object.nope)",
        "object.a.m.exists(k, k == 'k2')",
        "size(object.a.m) == 2",
        "'k1' in object.a.m",
        "object.a.m == {'k1': 'v', 'k2': null}",
        "{'k1': 'v', 'k2': null} == object.a.m",
        "object.a.b.all(x, x.c > 0)",
        "object.a.m.k2 == null",
        "object.a.b.map(x, x.c) == [1, 2]",
        "object.a.m.filter(k, object.a.m[k] == 'v') == ['k1']",
    ],
)
def test_lazy_cel_reads_as_json_to_cel(expression: str) -> None:
    environment = celpy.Environment()
    program = environment.program(environment.compile(expression))
    expected = program.evaluate({"object": json_to_cel(LAZY_DOCUMENT)})
    assert program.evaluate({"object": lazy_cel(LAZY_DOCUMENT)}) == expected


def test_lazy_cel_leaves_its_source_alone() -> None:
    document = {"a": {"b": 1}}
    environment = celpy.Environment()
    environment.program(environment.compile("object.a.b == 1")).evaluate({"object": lazy_cel(document)})
    assert document == {"a": {"b": 1}}


def test_an_installed_policy_checks_the_render(catalog: Catalog) -> None:
    catalog.installed = [_policy([NAMED_WEB]), _binding(), _service("installed")]
    result = check_policies([_service("api")], catalog)
    assert [v.obj.name for v in result.violations] == ["api"]


def test_a_rendered_policy_replaces_the_installed_one(catalog: Catalog) -> None:
    catalog.installed = [_policy([NAMED_WEB]), _binding()]
    permissive = _policy([{"expression": "true"}])
    assert not check_policies([permissive, _service("api")], catalog).violations


def test_an_installed_namespace_answers_the_selector(catalog: Catalog) -> None:
    catalog.installed = [_namespace("strict", strict="yes")]
    binding = _binding(matchResources={"namespaceSelector": {"matchLabels": {"strict": "yes"}}})
    result = check_policies([_policy([NAMED_WEB]), binding, _service("api", "strict")], catalog)
    assert [v.obj.namespace for v in result.violations] == ["strict"]
    assert not result.undecided


class _ListingApi(Api):
    """Answers `async_get` for a kind class from *served*; any other is a 404,
    as for a cluster older than admissionregistration.k8s.io/v1's policies."""

    def __init__(self, served: dict[str, list[dict[str, Any]]], status: int = 404) -> None:
        self.served = served
        self.status = status

    async def async_get(
        self,
        kind: str | type,
        *names: str,
        namespace: str | None = None,
        label_selector: str | dict[str, str] | None = None,
        field_selector: str | dict[str, str] | None = None,
        as_object: type[APIObject] | None = None,
        allow_unknown_type: bool = True,
        raw: bool = False,
        **kwargs: object,
    ) -> AsyncGenerator[APIObject | dict[Any, Any]]:
        if not isinstance(kind, type) or not issubclass(kind, APIObject):
            raise TypeError(kind)
        if kind.kind not in self.served:
            raise kr8s.ServerError("refused", response=httpx.Response(self.status))
        for item in self.served[kind.kind]:
            yield kind(item)


async def test_load_installed_reads_policies_and_namespaces() -> None:
    catalog = Catalog()
    api = _ListingApi(
        {"ValidatingAdmissionPolicy": [{"metadata": {"name": "p"}}], "Namespace": [{"metadata": {"name": "a"}}]}
    )
    assert await load_installed(catalog, api) == 2
    assert [(obj["apiVersion"], obj["kind"]) for obj in catalog.installed] == [
        ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicy"),
        ("v1", "Namespace"),
    ]


async def test_load_installed_raises_what_is_not_a_404() -> None:
    with pytest.raises(kr8s.ServerError):
        await load_installed(Catalog(), _ListingApi({}, status=403))
