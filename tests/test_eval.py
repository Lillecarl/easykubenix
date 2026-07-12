from __future__ import annotations

from pathlib import Path

import nanopynix
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def ekn_root() -> str:
    return str(PROJECT_ROOT)


class TestSimpleEval:
    async def test_eval_literal(self) -> None:
        async with (
            nanopynix.Session(experimental_features=["flakes", "nix-command"]) as session,
            session.store() as store,
            session.eval(store) as eval_,
        ):
            root = await eval_.string('{ x = 1; y = { z = "hello"; }; }')
            result = await root.force_json()
        assert result == {"x": 1, "y": {"z": "hello"}}

    async def test_eval_list(self) -> None:
        async with (
            nanopynix.Session(experimental_features=["flakes", "nix-command"]) as session,
            session.store() as store,
            session.eval(store) as eval_,
        ):
            root = await eval_.string("[ 1 2 3 ]")
            result = await root.force_json()
        assert result == [1, 2, 3]


class TestEknModule:
    async def test_generated_by_path(self, ekn_root: str) -> None:
        nix = f"""
        let
          compat = import {ekn_root}/nix/compat.nix;
          pkgs = import compat.inputs.nixpkgs {{}};
          easy = import {ekn_root} {{
            inherit pkgs;
            modules = [{{
              kubernetes.objects.default.ConfigMap.test = {{ data.key = "hello"; }};
            }}];
          }};
        in
        easy.config.kubernetes.generatedByPath
        """
        async with (
            nanopynix.Session(experimental_features=["flakes", "nix-command"]) as session,
            session.store() as store,
            session.eval(store) as eval_,
        ):
            root = await eval_.string(nix)
            result = await root.force_json()
        assert isinstance(result, dict)
        assert "default" in result
        assert "ConfigMap" in result["default"]
        assert "test" in result["default"]["ConfigMap"]

    async def test_gitops_branch(self, ekn_root: str) -> None:
        nix = f"""
        let
          compat = import {ekn_root}/nix/compat.nix;
          pkgs = import compat.inputs.nixpkgs {{}};
          easy = import {ekn_root} {{
            inherit pkgs;
            modules = [{{
              gitops.enable = true;
              gitops.branch = "production";
            }}];
          }};
        in
        easy.config.gitops.branch
        """
        async with (
            nanopynix.Session(experimental_features=["flakes", "nix-command"]) as session,
            session.store() as store,
            session.eval(store) as eval_,
        ):
            root = await eval_.string(nix)
            result = await root.force_json()
        assert result == "production"

    async def test_manifest_yaml_list(self, ekn_root: str) -> None:
        nix = f"""
        let
          compat = import {ekn_root}/nix/compat.nix;
          pkgs = import compat.inputs.nixpkgs {{}};
          easy = import {ekn_root} {{
            inherit pkgs;
            modules = [{{
              kubernetes.objects.default.ConfigMap.test = {{ data.key = "hello"; }};
            }}];
          }};
        in
        easy.config.internal.manifestYAMLList
        """
        async with (
            nanopynix.Session(experimental_features=["flakes", "nix-command"]) as session,
            session.store() as store,
            session.eval(store) as eval_,
        ):
            root = await eval_.string(nix)
            result = await root.force_json()
        assert isinstance(result, str)
        assert "ConfigMap" in result
        assert "hello" in result
