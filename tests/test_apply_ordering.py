from __future__ import annotations

from ekn.apply import barriers

# The tail of the `ekn.resourcePriority` default (Helm's InstallOrder numbered
# in fives), with the same numbers the real option uses -- the relationship
# between these and the unlisted-kind fallback is the whole point of the tests
# below. tests/test_eval.py checks these against the actually evaluated option.
HELM_ORDER_TAIL = {
    "Namespace": 5,
    "CustomResourceDefinition": 70,
    "Deployment": 140,
    "APIService": 175,
    # Moved past the unlisted-kind band (1000) -- see easykubenix/ekn.nix.
    "MutatingWebhookConfiguration": 1005,
    "ValidatingWebhookConfiguration": 1010,
}


def _kinds_per_barrier(kinds: list[str], priority: dict[str, int]) -> list[list[str]]:
    objects = [{"kind": kind} for kind in kinds]
    return [[str(obj["kind"]) for obj in tier] for tier in barriers(objects, priority)]


class TestBarriers:
    def test_unlisted_kinds_apply_after_the_crds_that_establish_them(self) -> None:
        """A custom resource is an unlisted kind, and must land after the
        CustomResourceDefinition that establishes it.
        """
        order = _kinds_per_barrier(["Prometheus", "CustomResourceDefinition", "Namespace"], HELM_ORDER_TAIL)
        assert order == [["Namespace"], ["CustomResourceDefinition"], ["Prometheus"]]

    def test_unlisted_kinds_apply_before_admission_webhooks(self) -> None:
        """The reverse constraint, and the one Helm's own sorter gets wrong.

        Helm's rule is "unknown kind is last", which puts every custom
        resource behind the webhook configurations that intercept it. During
        a bootstrap the webhook's backend is not serving yet, so each write
        then costs the webhook's full timeout. Unlisted kinds must sort
        ahead of both webhook kinds.
        """
        order = _kinds_per_barrier(
            ["ValidatingWebhookConfiguration", "PrometheusRule", "MutatingWebhookConfiguration"],
            HELM_ORDER_TAIL,
        )
        assert order == [
            ["PrometheusRule"],
            ["MutatingWebhookConfiguration"],
            ["ValidatingWebhookConfiguration"],
        ]

    def test_webhooks_are_ordered_last_not_skipped(self) -> None:
        """Ordering is deliberately not filtering -- the API server must still
        schema-check these like any other object.
        """
        kinds = ["MutatingWebhookConfiguration", "Deployment", "ValidatingWebhookConfiguration"]
        applied = [kind for tier in _kinds_per_barrier(kinds, HELM_ORDER_TAIL) for kind in tier]
        assert sorted(applied) == sorted(kinds)

    def test_kinds_sharing_a_priority_share_a_barrier(self) -> None:
        order = _kinds_per_barrier(["Namespace", "Prometheus", "ServiceMonitor"], HELM_ORDER_TAIL)
        assert order == [["Namespace"], ["Prometheus", "ServiceMonitor"]]

    def test_empty_priority_map_is_a_single_barrier(self) -> None:
        assert _kinds_per_barrier(["Namespace", "Deployment"], {}) == [["Namespace", "Deployment"]]
