from __future__ import annotations

from ekn.apply import barriers

# A slice of the `ekn.resourcePriority` default (Helm's InstallOrder numbered
# in tens), with the same numbers the real option uses -- the relationship
# between these and the unlisted-kind fallback is the whole point of the tests
# below. tests/test_eval.py checks these against the actually evaluated option.
RESOURCE_PRIORITY = {
    "Namespace": 20,
    "CustomResourceDefinition": 150,
    "Deployment": 290,
    # All three intercept later requests, so they are numbered past the
    # unlisted-kind band at 1000 -- see easykubenix/ekn.nix.
    "APIService": 1010,
    "MutatingWebhookConfiguration": 1020,
    "ValidatingWebhookConfiguration": 1030,
}


def _kinds_per_barrier(kinds: list[str], priority: dict[str, int]) -> list[list[str]]:
    objects = [{"kind": kind} for kind in kinds]
    return [[str(obj["kind"]) for obj in tier] for tier in barriers(objects, priority)]


class TestBarriers:
    def test_unlisted_kinds_apply_after_the_crds_that_establish_them(self) -> None:
        """A custom resource is an unlisted kind, and must land after the
        CustomResourceDefinition that establishes it.
        """
        order = _kinds_per_barrier(["Prometheus", "CustomResourceDefinition", "Namespace"], RESOURCE_PRIORITY)
        assert order == [["Namespace"], ["CustomResourceDefinition"], ["Prometheus"]]

    def test_unlisted_kinds_apply_before_intercepting_kinds(self) -> None:
        """The reverse constraint, and the one Helm's own sorter gets wrong.

        Helm's rule is "unknown kind is last", which puts every custom
        resource behind the three kinds that intercept it. During a bootstrap
        their backend is not serving yet, so each write then costs the
        webhook's full timeout -- or, for an aggregated APIService, fails
        discovery outright. Unlisted kinds must sort ahead of all three.
        """
        order = _kinds_per_barrier(
            [
                "ValidatingWebhookConfiguration",
                "PrometheusRule",
                "MutatingWebhookConfiguration",
                "APIService",
            ],
            RESOURCE_PRIORITY,
        )
        assert order == [
            ["PrometheusRule"],
            ["APIService"],
            ["MutatingWebhookConfiguration"],
            ["ValidatingWebhookConfiguration"],
        ]

    def test_webhooks_are_ordered_last_not_skipped(self) -> None:
        """Ordering is deliberately not filtering -- the API server must still
        schema-check these like any other object.
        """
        kinds = ["MutatingWebhookConfiguration", "Deployment", "ValidatingWebhookConfiguration"]
        applied = [kind for tier in _kinds_per_barrier(kinds, RESOURCE_PRIORITY) for kind in tier]
        assert sorted(applied) == sorted(kinds)

    def test_kinds_sharing_a_priority_share_a_barrier(self) -> None:
        order = _kinds_per_barrier(["Namespace", "Prometheus", "ServiceMonitor"], RESOURCE_PRIORITY)
        assert order == [["Namespace"], ["Prometheus", "ServiceMonitor"]]

    def test_empty_priority_map_is_a_single_barrier(self) -> None:
        assert _kinds_per_barrier(["Namespace", "Deployment"], {}) == [["Namespace", "Deployment"]]
