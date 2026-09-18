"""A seeded object is not written to a branch another applier reads.

Issue #14. A Secret rendered by a deployment unit's own `modules` reached
`kubernetes.deploymentUnits.<name>.objects` carrying `$ekn:env:VARNAME`,
and `ekn commit` wrote it to the deploy branch. Confirmed live by the
solid-kubernetes project: a committed `Secret` whose `stringData.apiToken`
was exactly `len("$ekn:env:") + len("SOLID_CLOUDFLARE_TOKEN")` characters.

**It is not a disclosure bug.** The value never enters Nix; the file holds
a reference and nothing else. The hazard runs the other way: a controller
syncing that file applies the sentinel as the literal value, over the
credential `ekn kubeapply` had already delivered. Both sides believe they
are right, because a string sentinel is schema-valid by design.

`kubernetes.generatedExportable` already states the rule. The gap was that
`deploymentUnits` reads `generated` instead, and the `seededGitOpsObjects`
assertion only fires on an object *routed* to a unit -- which a unit's own
modules never do.
"""

from __future__ import annotations

from typing import Any

import pytest
import structlog.testing

from ekn.eval import GitOpsManifestsResult
from ekn.gitops import file_groups, without_seeded

_SEEDED = {
    "apiVersion": "v1",
    "kind": "Secret",
    "metadata": {
        "name": "cloudflare-api-token",
        "namespace": "cert-manager",
        "annotations": {"ekn.dev/env-0": "SOLID_CLOUDFLARE_TOKEN"},
    },
    "stringData": {"apiToken": "$ekn:env:SOLID_CLOUDFLARE_TOKEN"},
}

_PLAIN = {
    "apiVersion": "v1",
    "kind": "ConfigMap",
    "metadata": {"name": "settings", "namespace": "cert-manager"},
    "data": {"key": "value"},
}


def _result(objects: list[dict[str, Any]], path: str = "bootstrap/") -> GitOpsManifestsResult:
    return GitOpsManifestsResult.model_validate(
        {
            "config": {
                "kubernetes": {
                    "deploymentUnits": {
                        "bootstrap": {
                            "target": {"path": path},
                            "objects": objects,
                        }
                    }
                },
                "deployment": {"deployBranch": "deploy"},
            }
        }
    )


class TestWithoutSeeded:
    def test_a_seeded_object_is_withheld_and_named(self) -> None:
        committable, withheld = without_seeded([_SEEDED, _PLAIN], "bootstrap/")

        assert committable == [_PLAIN]
        assert withheld == ["bootstrap/: cert-manager/Secret/cloudflare-api-token"]

    def test_an_object_with_no_annotation_is_committable(self) -> None:
        """The annotation is what says "seeded", not the string's shape. A
        `data` value that merely looks like a reference is somebody's literal
        and not ours to drop."""
        lookalike = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "docs", "namespace": "default"},
            "data": {"example": "$ekn:env:NOT_A_SEED"},
        }

        committable, withheld = without_seeded([lookalike], "bootstrap/")

        assert committable == [lookalike]
        assert withheld == []

    def test_nothing_seeded_withholds_nothing(self) -> None:
        assert without_seeded([_PLAIN], "bootstrap/") == ([_PLAIN], [])


class TestFileGroups:
    def test_the_seeded_secret_reaches_no_file(self) -> None:
        files = dict(file_groups(_result([_SEEDED, _PLAIN])))

        assert "bootstrap/cert-manager/Secret/cloudflare-api-token.yaml" not in files
        assert "bootstrap/cert-manager/ConfigMap/settings.yaml" in files

    def test_the_sentinel_is_in_no_committed_byte(self) -> None:
        """The assertion that survives a change of file layout: whatever the
        paths end up being, `$ekn:env:` is in none of the content."""
        files = file_groups(_result([_SEEDED, _PLAIN]))

        assert not [path for path, content in files if "$ekn:env:" in content]

    def test_it_is_not_in_the_kustomization_either(self) -> None:
        """A resource listed by kustomize but never written is a sync error
        for whoever reads the branch, which is a different bug from the one
        being fixed."""
        files = dict(file_groups(_result([_SEEDED, _PLAIN])))
        kustomization = files["bootstrap/kustomization.yaml"]

        assert "cloudflare-api-token" not in kustomization
        assert "settings.yaml" in kustomization

    def test_it_says_so(self) -> None:
        """Silently dropping it is the same class of bug as committing it:
        the branch is rewritten from this list, so an object left out is
        *deleted* from the branch, not merely not-added."""
        with structlog.testing.capture_logs() as logs:
            file_groups(_result([_SEEDED, _PLAIN]))

        warnings = [entry for entry in logs if entry.get("log_level") == "warning"]
        assert warnings, "withholding an object from a commit is not something to do quietly"
        said = str(warnings[0].get("event", ""))
        assert "cert-manager/Secret/cloudflare-api-token" in said

    def test_a_unit_of_only_seeded_objects_commits_nothing(self) -> None:
        """The whole-unit case, which is the shape a bootstrap unit has. It
        must not raise: `ekn kubeapply --target bootstrap` still applies
        these, and delivering that credential is why seeds exist."""
        files = dict(file_groups(_result([_SEEDED])))

        assert not [path for path in files if path.endswith("Secret/cloudflare-api-token.yaml")]


@pytest.mark.parametrize("path", ["bootstrap/", "clusters/demo/bootstrap/"])
def test_the_report_names_the_target_path(path: str) -> None:
    """Two units can render the same namespace/kind/name to different paths,
    so the path is what tells the reader which one to look at."""
    _committable, withheld = without_seeded([_SEEDED], path)

    assert withheld == [f"{path}: cert-manager/Secret/cloudflare-api-token"]
