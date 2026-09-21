# SPDX-License-Identifier: MIT
"""`ekn.clusterUid` as the module system sees it, and as `ekn` reads it.

A real instance, because the type is half the design: the option refuses
anything that is not uid-shaped, so a pasted cluster *name* or a truncated
uid fails during evaluation rather than after the API call that would have
told the operator nothing useful.
"""

from __future__ import annotations

import pathlib

import pytest
from nanopynix.exceptions import NixError
from test_pre_apply import PROJECT_ROOT

from ekn.eval import evaluate_kubeapply_config

LIVE = "3b2a1f0e-9d8c-4b7a-8e6f-5d4c3b2a1f0e"


def _instance(tmp_path: pathlib.Path, ekn_lines: str) -> pathlib.Path:
    f = tmp_path / "instance.nix"
    f.write_text(f"""
        let
          sources = import {PROJECT_ROOT}/nix/sources.nix;
          pkgs = import sources.nixpkgs {{ }};
        in
        import {PROJECT_ROOT} {{
          inherit pkgs;
          modules = [
            ({{ pkgs, ... }}: {{
              ekn.environment = "easykubenix";
              {ekn_lines}
              kubernetes.objects.default.ConfigMap.c.data.key = "value";
            }})
          ];
        }}
    """)
    return f


class TestTheOptionReachesPython:
    async def test_a_declared_uid_arrives(self, tmp_path: pathlib.Path) -> None:
        f = _instance(tmp_path, f'ekn.clusterUid = "{LIVE}";')

        cfg = await evaluate_kubeapply_config(f, None, None, None, None)

        assert cfg.cluster_uid == LIVE

    async def test_undeclared_is_none(self, tmp_path: pathlib.Path) -> None:
        """Which the fence reads as "unknown cluster" and refuses -- the same
        answer an easykubenix older than the option gives."""
        cfg = await evaluate_kubeapply_config(_instance(tmp_path, ""), None, None, None, None)

        assert cfg.cluster_uid is None


class TestTheTypeRefusesWhatIsNotAUid:
    @pytest.mark.parametrize(
        ("value", "why"),
        [
            ("nixlab2", "a cluster name, which is what someone reaches for first"),
            ("3b2a1f0e-9d8c-4b7a-8e6f", "truncated"),
            ("3B2A1F0E-9D8C-4B7A-8E6F-5D4C3B2A1F0E", "upper case; Kubernetes emits lower"),
            ("", "empty, which would otherwise read as declared-and-blank"),
        ],
    )
    async def test_it_fails_at_evaluation(self, tmp_path: pathlib.Path, value: str, why: str) -> None:
        f = _instance(tmp_path, f'ekn.clusterUid = "{value}";')

        with pytest.raises(NixError):
            await evaluate_kubeapply_config(f, None, None, None, None)
