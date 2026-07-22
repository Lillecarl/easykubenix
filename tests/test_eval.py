from __future__ import annotations

from pathlib import Path

import nanopynix
import pytest

from ekn.eval import evaluate_file

PROJECT_ROOT = Path(__file__).resolve().parent.parent
NIX_TEST_FILE = PROJECT_ROOT / "tests/test_eval.nix"
TEMPLATES_NIX_TEST_FILE = PROJECT_ROOT / "tests/test_templates.nix"


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
    async def test_adios_template_creates_a_resource(self) -> None:
        result = await evaluate_file(TEMPLATES_NIX_TEST_FILE, "templates")
        assert result == {
            "apiVersion": "bitnami.com/v1alpha1",
            "kind": "SealedSecret",
            "metadata": {"name": "database", "namespace": "default"},
            "spec": {
                "encryptedData": {"password": "AgByEncrypted"},
                "template": {"metadata": {"labels": {"app": "api"}}},
            },
        }

    async def test_adios_template_checks_arguments(self) -> None:
        with pytest.raises(nanopynix.NixError, match="encryptedData"):
            await evaluate_file(TEMPLATES_NIX_TEST_FILE, "invalidTemplateArguments")

    async def test_ekn_routing_is_stripped_from_manifests(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "eknRouting")
        assert isinstance(result, dict)

        deployment = result["generatedByPath"]["default"]["Deployment"]["api"]
        assert "ekn" not in deployment
        target = result["gitopsTargets"]["apps"]
        assert target["target"] == {"branch": "deploy", "path": "clusters/home/apps"}
        assert target["objects"][0]["metadata"]["name"] == "api"

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


class TestValidationConfig:
    async def test_validation_config_builds(self) -> None:
        from ekn.eval import evaluate_validation_config

        flake = str(PROJECT_ROOT / "docs/examples/example-flake")
        cfg = await evaluate_validation_config(flake, "myapp")
        c = cfg["config"]
        assert c["kubernetes"]["package"]["version"]
        assert c["kubernetes"]["package"]["outPath"].startswith("/nix/store/")
        assert c["validation"]["etcdPackage"]["outPath"].startswith("/nix/store/")
        assert c["validation"]["kubeconformPackage"]["outPath"].startswith("/nix/store/")
        assert isinstance(c["kluctl"]["resourcePriority"], dict)
        assert c["kluctl"]["discriminator"]
        assert c["internal"]["manifestJSONFile"]["outPath"].startswith("/nix/store/")
