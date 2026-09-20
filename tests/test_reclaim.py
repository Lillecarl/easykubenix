"""`rewrite_managed_fields`, which is where the ownership move happens.

The rest of `ekn.reclaim` is a GET and a PATCH. This function is the decision:
which records move, which are left alone, and what happens when the manager
being moved onto already has a record of its own.
"""

from __future__ import annotations

from ekn.reclaim import rewrite_managed_fields


def _apply(manager: str, fields: dict, api_version: str = "apps/v1") -> dict:
    return {
        "manager": manager,
        "operation": "Apply",
        "apiVersion": api_version,
        "fieldsV1": fields,
        "time": "2026-01-01T00:00:00Z",
    }


_PROBE = {"f:spec": {"f:template": {"f:spec": {"f:livenessProbe": {"f:tcpSocket": {}}}}}}
_REPLICAS = {"f:spec": {"f:replicas": {}}}


class TestWhatMoves:
    def test_the_old_manager_s_apply_record_is_renamed(self):
        """Renamed, not deleted. A deleted record leaves its fields unowned,
        and nothing ever removes an unowned field -- that is the bug, not the
        fix."""
        out = rewrite_managed_fields([_apply("ekn", _PROBE)], old=["ekn"], new="argocd")

        assert out is not None
        assert [e["manager"] for e in out] == ["argocd"]
        assert out[0]["fieldsV1"] == _PROBE

    def test_an_update_record_is_left_alone(self):
        """`kube-controller-manager`'s `status` is one of these, and
        server-side apply does not consult an Update record for removal, so
        renaming it would claim ownership this cannot act on."""
        entries = [
            _apply("ekn", _PROBE),
            {
                "manager": "kube-controller-manager",
                "operation": "Update",
                "apiVersion": "apps/v1",
                "subresource": "status",
            },
        ]

        out = rewrite_managed_fields(entries, old=["ekn"], new="argocd")

        assert out is not None
        assert {e["manager"] for e in out} == {"argocd", "kube-controller-manager"}
        untouched = next(e for e in out if e["operation"] == "Update")
        assert untouched["manager"] == "kube-controller-manager"

    def test_a_manager_nobody_named_is_left_alone(self):
        entries = [_apply("ekn", _PROBE), _apply("some-operator", _REPLICAS)]

        out = rewrite_managed_fields(entries, old=["ekn"], new="argocd")

        assert out is not None
        assert {e["manager"] for e in out} == {"argocd", "some-operator"}


class TestWhenNothingShouldHappen:
    def test_no_match_answers_none(self):
        """So a caller can skip the write. Patching an object to the value it
        already has is a write nobody asked for."""
        assert rewrite_managed_fields([_apply("argocd", _PROBE)], old=["ekn"], new="argocd") is None

    def test_reclaiming_onto_itself_answers_none(self):
        assert rewrite_managed_fields([_apply("ekn", _PROBE)], old=["ekn"], new="ekn") is None

    def test_an_empty_old_set_answers_none(self):
        assert rewrite_managed_fields([_apply("ekn", _PROBE)], old=[], new="argocd") is None


class TestTheMerge:
    """The case that would otherwise write a shape the API server never
    produces: two records with one manager, operation and apiVersion."""

    def test_two_records_in_one_slot_become_one(self):
        entries = [_apply("ekn", _PROBE), _apply("argocd", _REPLICAS)]

        out = rewrite_managed_fields(entries, old=["ekn"], new="argocd")

        assert out is not None
        assert len(out) == 1
        assert out[0]["manager"] == "argocd"

    def test_the_merged_record_owns_both_field_sets(self):
        """The negative control for the test above: a merge that dropped one
        side would still answer one record."""
        entries = [_apply("ekn", _PROBE), _apply("argocd", _REPLICAS)]

        out = rewrite_managed_fields(entries, old=["ekn"], new="argocd")

        assert out is not None
        fields = out[0]["fieldsV1"]
        assert fields["f:spec"]["f:replicas"] == {}
        assert fields["f:spec"]["f:template"]["f:spec"]["f:livenessProbe"]["f:tcpSocket"] == {}

    def test_a_different_api_version_is_a_different_slot(self):
        """The API server keys a record by manager, operation, apiVersion and
        subresource. Merging across apiVersions would fold two field sets
        that describe different schemas."""
        entries = [_apply("ekn", _PROBE, "apps/v1"), _apply("argocd", _REPLICAS, "apps/v1beta1")]

        out = rewrite_managed_fields(entries, old=["ekn"], new="argocd")

        assert out is not None
        assert len(out) == 2
        assert {e["apiVersion"] for e in out} == {"apps/v1", "apps/v1beta1"}

    def test_the_input_is_not_mutated(self):
        """`reclaim_one` logs the live owners after calling this, and would
        report the new name for all of them if this edited in place."""
        entries = [_apply("ekn", _PROBE)]

        rewrite_managed_fields(entries, old=["ekn"], new="argocd")

        assert entries[0]["manager"] == "ekn"
