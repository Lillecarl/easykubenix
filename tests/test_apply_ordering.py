from __future__ import annotations

from ekn.apply import barriers
from ekn.validation import VALIDATION_DEFERRED_KINDS, validation_resource_priority

# A stand-in for the `ekn.resourcePriority` default (Helm's InstallOrder
# numbered in fives). Only the entries these tests reason about, with the same
# relative order and the same top end.
HELM_ORDER_SUBSET = {
    "Namespace": 5,
    "ServiceAccount": 35,
    "CustomResourceDefinition": 70,
    "Deployment": 140,
    "MutatingWebhookConfiguration": 180,
    "ValidatingWebhookConfiguration": 185,
}


def _kinds_per_barrier(objects: list[dict[str, object]], priority: dict[str, int]) -> list[list[str]]:
    return [[str(obj["kind"]) for obj in tier] for tier in barriers(objects, priority)]


class TestBarriers:
    def test_unlisted_kinds_apply_after_every_listed_kind(self) -> None:
        """A custom resource must land after the CRD that establishes it.

        This is the whole reason the fallback priority sits above the top of
        the configured range rather than in the middle of it: `Prometheus` is
        not in Helm's InstallOrder, so it only sorts correctly relative to
        `CustomResourceDefinition` (70) if unlisted kinds go last.
        """
        objects = [
            {"kind": "Prometheus"},
            {"kind": "CustomResourceDefinition"},
            {"kind": "ValidatingWebhookConfiguration"},
            {"kind": "Namespace"},
        ]
        assert _kinds_per_barrier(objects, HELM_ORDER_SUBSET) == [
            ["Namespace"],
            ["CustomResourceDefinition"],
            ["ValidatingWebhookConfiguration"],
            ["Prometheus"],
        ]

    def test_kinds_sharing_a_priority_share_a_barrier(self) -> None:
        objects = [{"kind": "Namespace"}, {"kind": "Prometheus"}, {"kind": "ServiceMonitor"}]
        assert _kinds_per_barrier(objects, HELM_ORDER_SUBSET) == [
            ["Namespace"],
            ["Prometheus", "ServiceMonitor"],
        ]

    def test_empty_priority_map_is_a_single_barrier(self) -> None:
        objects = [{"kind": "Namespace"}, {"kind": "Deployment"}]
        assert _kinds_per_barrier(objects, {}) == [["Namespace", "Deployment"]]


class TestValidationResourcePriority:
    def test_webhook_configurations_are_deferred_past_unlisted_kinds(self) -> None:
        """The validation harness has no kubelet, so a registered admission
        webhook can never be answered and every matching write afterwards
        stalls for its full timeout. Deferring them past even the unlisted
        kinds means nothing they could intercept is applied after them.
        """
        deferred = validation_resource_priority(HELM_ORDER_SUBSET)
        objects = [
            {"kind": "ValidatingWebhookConfiguration"},
            {"kind": "MutatingWebhookConfiguration"},
            {"kind": "PrometheusRule"},
            {"kind": "Deployment"},
        ]
        order = _kinds_per_barrier(objects, deferred)
        assert order[0] == ["Deployment"]
        assert order[1] == ["PrometheusRule"]
        assert order[-1] == ["ValidatingWebhookConfiguration", "MutatingWebhookConfiguration"]

    def test_webhook_configurations_are_still_applied(self) -> None:
        """Deferring is deliberately not skipping -- the API server must still
        schema-check these objects like any other.
        """
        deferred = validation_resource_priority(HELM_ORDER_SUBSET)
        objects = [{"kind": kind} for kind in VALIDATION_DEFERRED_KINDS]
        applied = [kind for tier in barriers(objects, deferred) for kind in (str(o["kind"]) for o in tier)]
        assert sorted(applied) == sorted(VALIDATION_DEFERRED_KINDS)

    def test_other_kinds_are_left_alone(self) -> None:
        deferred = validation_resource_priority(HELM_ORDER_SUBSET)
        untouched = {k: v for k, v in HELM_ORDER_SUBSET.items() if k not in VALIDATION_DEFERRED_KINDS}
        assert all(deferred[kind] == value for kind, value in untouched.items())
