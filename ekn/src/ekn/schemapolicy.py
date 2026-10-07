"""Rendered ValidatingAdmissionPolicies, evaluated against the objects they admit.

A policy does nothing until a binding names it, so each binding is one check:
its policy's `matchConstraints`, narrowed by its own `matchResources`, picks
the objects; `variables`, `matchConditions` and `validations` are CEL, run
with the same engine as a CRD's rules.

An apply is a CREATE, or an UPDATE when the object exists, and this has no
stored object to compare with. So a policy that admits CREATE is evaluated as
one, with `oldObject` null. One that admits only UPDATE is evaluated as an
update, and an expression that reads `oldObject` there is skipped.

What a render cannot know stays unbound, and an expression that reads it is
skipped and counted, never guessed: `request`, `authorizer`, `params` when the
policy declares a `paramKind`, and `namespaceObject` when the namespace is not
in the render.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from celpy import celtypes
from celpy.evaluation import CELEvalError

from ekn.schemacel import CelChecker, Unsupported, cel_view, lazy_cel
from ekn.schemacheck import GroupVersionKind, ObjectRef, Violation, strip_for_check

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from ekn.schemacheck import Catalog, Resource

ADMISSION_GROUP = "admissionregistration.k8s.io"
POLICY = "ValidatingAdmissionPolicy"
BINDING = "ValidatingAdmissionPolicyBinding"

_VARIABLE = re.compile(r"\bvariables\.(\w+)")
#: What an admission expression may read that this binds only sometimes.
_ADMISSION_NAMES = re.compile(r"(?<![.\w])(request|authorizer|oldObject|namespaceObject|params)\b")


@dataclass(slots=True)
class PolicyResult:
    #: Binding and object pairs whose CEL ran.
    admitted: int = 0
    evaluated: int = 0
    #: Expressions not evaluated, by reason.
    skipped: Counter[str] = field(default_factory=Counter)
    #: Objects a binding cannot decide whether it matches, by reason.
    undecided: Counter[str] = field(default_factory=Counter)
    #: Failures under `Deny`.
    violations: list[Violation] = field(default_factory=list)
    #: Failures under `Warn` or `Audit` only.
    warnings: list[Violation] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Undecided:
    reason: str


def _as_dict(value: Any) -> dict[str, Any]:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return cast("list[Any]", value) if isinstance(value, list) else []


def _metadata(obj: Mapping[str, Any]) -> dict[str, Any]:
    return _as_dict(obj.get("metadata"))


def _is_admission(obj: Mapping[str, Any], kind: str) -> bool:
    return obj.get("kind") == kind and str(obj.get("apiVersion", "")).startswith(f"{ADMISSION_GROUP}/")


def selector_matches(selector: Mapping[str, Any] | None, labels: Mapping[str, Any]) -> bool:
    """A `metav1.LabelSelector` against *labels*. An empty one matches all."""
    if selector is None:
        return True
    for key, value in _as_dict(selector.get("matchLabels")).items():
        if labels.get(key) != value:
            return False
    for expression in _as_list(selector.get("matchExpressions")):
        requirement = _as_dict(expression)
        key = str(requirement.get("key", ""))
        values = _as_list(requirement.get("values"))
        operator = requirement.get("operator")
        present = key in labels
        if operator == "In" and not (present and labels[key] in values):
            return False
        if operator == "NotIn" and present and labels[key] in values:
            return False
        if operator == "Exists" and not present:
            return False
        if operator == "DoesNotExist" and present:
            return False
    return True


@dataclass(frozen=True, slots=True)
class _Request:
    operation: str
    gvk: GroupVersionKind
    resource: Resource | None
    name: str


def _rule_matches(rule: Mapping[str, Any], request: _Request, *, exact: bool) -> bool | _Undecided:
    """*rule*, a `NamedRuleWithOperations`, against one request."""
    gvk, resource = request.gvk, request.resource
    operations = _as_list(rule.get("operations"))
    if "*" not in operations and request.operation not in operations:
        return False
    groups = _as_list(rule.get("apiGroups"))
    versions = _as_list(rule.get("apiVersions"))
    if "*" not in groups and gvk.group not in groups:
        return False
    # Under `matchPolicy: Equivalent`, the default, the API server converts the
    # object to the version the rule names. The same resource in another
    # version matches.
    if exact and "*" not in versions and gvk.version not in versions:
        return False
    names = _as_list(rule.get("resourceNames"))
    if names and request.name not in names:
        return False
    # `pods/*` is pods' subresources, not pods; `*/*` is everything.
    resources = {str(entry) for entry in _as_list(rule.get("resources"))}
    if resource is None:
        return True if resources & {"*", "*/*"} else _Undecided(f"no resource name for {gvk}")
    if not resources & {"*", "*/*", resource.name}:
        return False
    scope = rule.get("scope", "*")
    return scope == "*" or (scope == "Namespaced") == resource.namespaced


def _rules_match(rules: Iterable[Any], request: _Request, *, exact: bool) -> bool | _Undecided:
    undecided: _Undecided | None = None
    for rule in rules:
        found = _rule_matches(_as_dict(rule), request, exact=exact)
        if found is True:
            return True
        if isinstance(found, _Undecided):
            undecided = found
    return undecided or False


@dataclass(slots=True)
class _Target:
    obj: dict[str, Any]
    ref: ObjectRef
    resource: Resource | None
    #: The CRD's schema, for a custom resource whose CRD is known.
    schema: Mapping[str, Any] | None
    #: The object's Namespace, from the render; None when it has none, or
    #: the render does not hold it.
    namespace: dict[str, Any] | None
    _cel: Any = None

    def cel(self) -> Any:
        """The object as CEL sees it, built when a binding first matches it."""
        if self._cel is None:
            view = cel_view(self.schema, self.obj) if self.schema is not None else self.obj
            self._cel = lazy_cel(view)
        return self._cel


@dataclass(slots=True)
class _Binding:
    binding: dict[str, Any]
    policy: dict[str, Any]

    @property
    def name(self) -> str:
        return str(_metadata(self.binding).get("name", ""))

    @property
    def policy_name(self) -> str:
        return str(_metadata(self.policy).get("name", ""))

    @property
    def spec(self) -> dict[str, Any]:
        return _as_dict(self.policy.get("spec"))

    def operation(self, target: _Target) -> str | _Undecided | None:
        """The operation an apply of *target* is evaluated as: CREATE when the
        binding admits it, else UPDATE, else None when it matches neither."""
        for operation in ("CREATE", "UPDATE"):
            found = self.matches(target, operation)
            if found is not False:
                return operation if found is True else found
        return None

    def matches(self, target: _Target, operation: str) -> bool | _Undecided:
        for match in (
            _as_dict(self.spec.get("matchConstraints")),
            _as_dict(_as_dict(self.binding.get("spec")).get("matchResources")),
        ):
            found = _match_resources(match, target, operation)
            if found is not True:
                return found
        return True


def _match_resources(match: Mapping[str, Any], target: _Target, operation: str) -> bool | _Undecided:
    """One `MatchResources` against *target*. An empty one, as a binding
    without `matchResources` has, matches all."""
    exact = match.get("matchPolicy") == "Exact"
    gvk = target.ref.gvk
    request = _Request(operation, gvk, target.resource, target.ref.name)
    rules = _as_list(match.get("resourceRules"))
    if rules:
        found = _rules_match(rules, request, exact=exact)
        if found is not True:
            return found
    excluded = _rules_match(_as_list(match.get("excludeResourceRules")), request, exact=exact)
    if excluded is True:
        return False
    if isinstance(excluded, _Undecided):
        return excluded
    labels = _as_dict(_metadata(target.obj).get("labels"))
    if not selector_matches(cast("Mapping[str, Any] | None", match.get("objectSelector")), labels):
        return False
    return _namespace_matches(cast("Mapping[str, Any] | None", match.get("namespaceSelector")), target)


def _namespace_matches(selector: Mapping[str, Any] | None, target: _Target) -> bool | _Undecided:
    if not selector or not (selector.get("matchLabels") or selector.get("matchExpressions")):
        return True
    gvk = target.ref.gvk
    if gvk == GroupVersionKind("", "v1", "Namespace"):
        return selector_matches(selector, _as_dict(_metadata(target.obj).get("labels")))
    if target.resource is None:
        return _Undecided(f"no resource name for {gvk}")
    if not target.resource.namespaced:
        # The API server never skips a cluster-scoped object on its namespace.
        return True
    if target.namespace is None:
        return _Undecided("namespace not in the render")
    return selector_matches(selector, _as_dict(_metadata(target.namespace).get("labels")))


@dataclass(slots=True)
class _Evaluation:
    """One binding against one object."""

    cel: CelChecker
    binding: _Binding
    activation: dict[str, Any]
    result: PolicyResult
    #: Variables that did not evaluate, and why.
    broken: dict[str, CELEvalError | Unsupported] = field(default_factory=dict)

    def run(self, text: str) -> Any:
        for name in _VARIABLE.findall(text):
            if name in self.broken:
                return self.broken[name]
        outcome = self.cel.run(text, self.activation)
        if isinstance(outcome, Unsupported):
            # celpy names the first undeclared thing it meets, which for
            # `authorizer.serviceAccount(...).allowed()` is `allowed`.
            unbound = [name for name in _ADMISSION_NAMES.findall(text) if name not in self.activation]
            if unbound:
                return Unsupported(f"unsupported: {unbound[0]}")
        return outcome

    def variables(self) -> None:
        values: dict[celtypes.StringType, Any] = {}
        self.activation["variables"] = celtypes.MapType(values)
        for variable in _as_list(self.binding.spec.get("variables")):
            spec = _as_dict(variable)
            name = str(spec.get("name", ""))
            outcome = self.run(str(spec.get("expression", "")))
            if isinstance(outcome, CELEvalError | Unsupported):
                self.broken[name] = outcome
            else:
                values[celtypes.StringType(name)] = outcome
            self.activation["variables"] = celtypes.MapType(values)


def _message(evaluation: _Evaluation, validation: Mapping[str, Any], expression: str) -> str:
    if "messageExpression" in validation:
        outcome = evaluation.run(str(validation["messageExpression"]))
        # celpy's `+` on two strings gives a plain `str`.
        if isinstance(outcome, str) and outcome.strip():
            return str(outcome)
    return str(validation.get("message") or f"failed expression: {expression}")


def _deny(evaluation: _Evaluation, target: _Target, message: str, *, failure: bool) -> None:
    """Record a failed check where its binding's actions send it.

    *failure* marks an expression that did not evaluate: `failurePolicy:
    Ignore` lets that through, as the API server does.
    """
    binding = evaluation.binding
    if failure and binding.spec.get("failurePolicy") == "Ignore":
        evaluation.result.skipped["error under failurePolicy: Ignore"] += 1
        return
    violation = Violation(
        target.ref,
        "",
        f"ValidatingAdmissionPolicy '{binding.policy_name}' with binding '{binding.name}' denied: {message}",
    )
    actions = _as_list(_as_dict(binding.binding.get("spec")).get("validationActions"))
    if "Deny" in actions:
        evaluation.result.violations.append(violation)
    elif actions:
        evaluation.result.warnings.append(violation)


def _admits(evaluation: _Evaluation, target: _Target) -> bool:
    """Whether every `matchCondition` holds. A condition that cannot be
    judged decides nothing, so the binding is undecided for *target*."""
    for condition in _as_list(evaluation.binding.spec.get("matchConditions")):
        spec = _as_dict(condition)
        expression = str(spec.get("expression", ""))
        outcome = evaluation.run(expression)
        if isinstance(outcome, Unsupported):
            evaluation.result.undecided[f"matchCondition {outcome.reason}"] += 1
            return False
        evaluation.result.evaluated += 1
        if isinstance(outcome, CELEvalError):
            _deny(
                evaluation,
                target,
                f"matchCondition {spec.get('name', '')!r} failed to evaluate: {outcome.args[0]}",
                failure=True,
            )
            return False
        if outcome != celtypes.BoolType(True):
            return False
    return True


def _validate(evaluation: _Evaluation, target: _Target) -> None:
    for validation in _as_list(evaluation.binding.spec.get("validations")):
        spec = _as_dict(validation)
        expression = str(spec.get("expression", ""))
        outcome = evaluation.run(expression)
        if isinstance(outcome, Unsupported):
            evaluation.result.skipped[outcome.reason] += 1
            continue
        evaluation.result.evaluated += 1
        if isinstance(outcome, CELEvalError):
            _deny(evaluation, target, f"expression {expression!r} failed to evaluate: {outcome.args[0]}", failure=True)
        elif outcome != celtypes.BoolType(True):
            _deny(evaluation, target, _message(evaluation, spec, expression), failure=False)


def _activation(binding: _Binding, target: _Target, operation: str) -> dict[str, Any]:
    activation: dict[str, Any] = {"object": target.cel()}
    if operation == "CREATE":
        activation["oldObject"] = None
    if "paramKind" not in binding.spec:
        activation["params"] = None
    if target.resource is not None and not target.resource.namespaced:
        activation["namespaceObject"] = None
    elif target.namespace is not None:
        activation["namespaceObject"] = lazy_cel(target.namespace)
    return activation


def _bindings(objects: Iterable[Mapping[str, Any]], result: PolicyResult) -> list[_Binding]:
    policies: dict[str, dict[str, Any]] = {}
    bindings: list[dict[str, Any]] = []
    for obj in objects:
        if _is_admission(obj, POLICY):
            policies[str(_metadata(obj).get("name", ""))] = dict(obj)
        elif _is_admission(obj, BINDING):
            bindings.append(dict(obj))
    found: list[_Binding] = []
    for binding in bindings:
        policy = policies.get(str(_as_dict(binding.get("spec")).get("policyName", "")))
        if policy is None:
            result.undecided["binding's policy not in the render"] += 1
        else:
            found.append(_Binding(binding, policy))
    return found


def _target(obj: Mapping[str, Any], catalog: Catalog, namespaces: Mapping[str, dict[str, Any]]) -> _Target:
    ref = ObjectRef.of(obj)
    namespace = namespaces.get(ref.namespace) if ref.namespace else None
    return _Target(strip_for_check(obj), ref, catalog.resources.get(ref.gvk), catalog.crd_schema(ref.gvk), namespace)


def check_policies(objects: Iterable[Mapping[str, Any]], catalog: Catalog) -> PolicyResult:
    """Every rendered binding against every rendered object it matches."""
    listed = list(objects)
    result = PolicyResult()
    bindings = _bindings(listed, result)
    if not bindings:
        return result
    namespaces = {
        str(_metadata(obj).get("name", "")): strip_for_check(obj)
        for obj in listed
        if GroupVersionKind.of(obj) == GroupVersionKind("", "v1", "Namespace")
    }
    targets = [_target(obj, catalog, namespaces) for obj in listed]
    for binding in bindings:
        for target in targets:
            operation = binding.operation(target)
            if isinstance(operation, _Undecided):
                result.undecided[operation.reason] += 1
                continue
            if operation is None:
                continue
            evaluation = _Evaluation(catalog.cel, binding, _activation(binding, target, operation), result)
            evaluation.variables()
            if _admits(evaluation, target):
                result.admitted += 1
                _validate(evaluation, target)
    return result
