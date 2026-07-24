from __future__ import annotations

import pytest
from pydantic import ValidationError

from ekn.eval import GitOpsTargetEntry, SopsAgeIdentity
from ekn.gitops import GitOpsTarget, resolved_targets


def test_gitops_target_entry_accepts_a_well_formed_entry() -> None:
    entry = GitOpsTargetEntry.model_validate(
        {
            "target": {"path": "clusters/prod"},
            "objects": [{"kind": "ConfigMap"}],
            "rawFiles": ["/etc/some.yaml"],
        }
    )
    assert entry.target.path == "clusters/prod"
    assert entry.objects == [{"kind": "ConfigMap"}]
    assert entry.raw_files == ["/etc/some.yaml"]


def test_gitops_target_entry_defaults_raw_files_to_empty_list() -> None:
    entry = GitOpsTargetEntry.model_validate({"target": {"path": "clusters/prod"}, "objects": []})
    assert entry.raw_files == []


@pytest.mark.parametrize(
    "payload",
    [
        {"objects": []},  # missing target
        {"target": "clusters/prod", "objects": []},  # target is a bare string, not {path: ...}
        {"target": {}, "objects": []},  # missing path
        {"target": {"path": ""}, "objects": []},  # empty path
        {"target": {"path": "clusters/prod"}},  # missing objects
        {"target": {"path": "clusters/prod"}, "objects": "not-a-list"},
        {"target": {"path": "clusters/prod"}, "objects": ["not-a-dict"]},
        {"target": {"path": "clusters/prod"}, "objects": [], "rawFiles": "not-a-list"},
        {"target": {"path": "clusters/prod"}, "objects": [], "rawFiles": [123]},
    ],
)
def test_gitops_target_entry_rejects_malformed_shapes(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        GitOpsTargetEntry.model_validate(payload)


def test_resolved_targets_merges_two_names_sharing_the_same_path() -> None:
    gitops_targets = {
        "a": GitOpsTargetEntry.model_validate(
            {"target": {"path": "clusters/prod"}, "objects": [{"kind": "ConfigMap", "metadata": {"name": "a"}}]}
        ),
        "b": GitOpsTargetEntry.model_validate(
            {"target": {"path": "clusters/prod"}, "objects": [{"kind": "ConfigMap", "metadata": {"name": "b"}}]}
        ),
    }
    routed = resolved_targets(gitops_targets)
    assert list(routed.keys()) == [GitOpsTarget(path="clusters/prod")]
    assert routed[GitOpsTarget(path="clusters/prod")] == [
        {"kind": "ConfigMap", "metadata": {"name": "a"}},
        {"kind": "ConfigMap", "metadata": {"name": "b"}},
    ]


def test_sops_age_identity_accepts_a_minimal_entry_with_defaults() -> None:
    identity = SopsAgeIdentity.model_validate({"namespace": "ns", "secretName": "age-key"})
    assert identity.namespace == "ns"
    assert identity.secret_name == "age-key"
    assert identity.key == "key.txt"
    assert identity.sops_config_file is None
    assert identity.sops_files == []


def test_sops_age_identity_accepts_a_full_entry() -> None:
    identity = SopsAgeIdentity.model_validate(
        {
            "namespace": "ns",
            "secretName": "age-key",
            "key": "identity.txt",
            "sopsConfigFile": "/repo/.sops.yaml",
            "sopsFiles": ["/repo/secrets.yaml"],
        }
    )
    assert identity.key == "identity.txt"
    assert identity.sops_config_file == "/repo/.sops.yaml"
    assert identity.sops_files == ["/repo/secrets.yaml"]


@pytest.mark.parametrize(
    "payload",
    [
        {"secretName": "age-key"},  # missing namespace
        {"namespace": "", "secretName": "age-key"},  # empty namespace
        {"namespace": "ns"},  # missing secretName
        {"namespace": "ns", "secretName": "age-key", "key": ""},
        {"namespace": "ns", "secretName": "age-key", "sopsFiles": "not-a-list"},
        {"namespace": "ns", "secretName": "age-key", "sopsFiles": [123]},
    ],
)
def test_sops_age_identity_rejects_malformed_shapes(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SopsAgeIdentity.model_validate(payload)
