from __future__ import annotations

from pathlib import Path

import anyio
import nanopynix
import pytest
from nanopynix.rpc import Session
from nanopynix_helpers.eval_target import EvaluationTargetError

from ekn.apply import _DEFAULT_BARRIER_PRIORITY
from ekn.eval import evaluate_file, realise_attr

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
            Session(experimental_features=["flakes", "nix-command"]) as session,
            session.store() as store,
            session.eval(store) as eval_,
        ):
            root = await eval_.string('{ x = 1; y = { z = "hello"; }; }')
            result = await root.to_python()
        assert result == {"x": 1, "y": {"z": "hello"}}

    async def test_eval_list(self) -> None:
        async with (
            Session(experimental_features=["flakes", "nix-command"]) as session,
            session.store() as store,
            session.eval(store) as eval_,
        ):
            root = await eval_.string("[ 1 2 3 ]")
            result = await root.to_python()
        assert result == [1, 2, 3]


class TestRealiseAttr:
    async def test_builds_and_returns_store_path(self, tmp_path: Path) -> None:
        compat_path = PROJECT_ROOT / "nix/compat.nix"
        f = tmp_path / "drv.nix"
        f.write_text(f"""
            let
              compat = import {compat_path};
              pkgs = import compat.inputs.nixpkgs {{ }};
            in
            {{ thing = pkgs.writeText "ekn-realise-test" "hello from ekn"; }}
        """)

        path = await realise_attr(f, None, "thing")

        built = anyio.Path(path)
        assert await built.is_file()
        assert await built.read_text() == "hello from ekn"

    async def test_rejects_empty_segment(self, tmp_path: Path) -> None:
        f = tmp_path / "drv.nix"
        # `foo` must exist. select_attr validates each component as it walks,
        # so a missing first component would mask the empty one.
        f.write_text("{ foo = { }; }")

        with pytest.raises(EvaluationTargetError, match="empty component"):
            await realise_attr(f, None, "foo..bar")

    async def test_requires_file_or_flake(self) -> None:
        with pytest.raises(ValueError, match="specify --file or --flake"):
            await realise_attr(None, None, "thing")


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
        target = result["gitOpsTargets"]["apps"]
        assert target["target"]["path"] == "clusters/home/apps"
        # Derived from `ekn.discriminator`, per target, so pruning one target
        # cannot reach another's objects -- see gitops.nix.
        assert target["target"]["discriminator"] == "easykubenix-apps"
        assert target["objects"][0]["metadata"]["name"] == "api"

    async def test_labels_annotations_are_coerced_by_default(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "labelsAnnotationsCoercion")
        assert isinstance(result, list)
        metadata = result[0]["metadata"]
        assert metadata["labels"] == {"enabled": "true", "replicas": "3"}
        assert metadata["annotations"] == {"disabled": "false"}

    async def test_labels_annotations_coercion_can_be_disabled(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "labelsAnnotationsCoercionDisabled")
        assert isinstance(result, list)
        assert result[0]["metadata"]["labels"] == {"enabled": "true"}

    async def test_labels_annotations_coercion_disabled_rejects_bool(self) -> None:
        # Top-level metadata.labels/annotations are the typed labelValueType
        # option -- with coercion disabled, that's plain types.str, so the
        # module system itself rejects the bool.
        with pytest.raises(nanopynix.NixError, match="is not of type"):
            await evaluate_file(NIX_TEST_FILE, "labelsAnnotationsCoercionDisabledThrows")

    async def test_init_containers_preserve_order(self) -> None:
        # A transformer runs after the option merge, so it can introduce a
        # marker that no type ever sees. The pipeline still has to convert
        # that one back into a list, in order.
        result = await evaluate_file(NIX_TEST_FILE, "initContainersOrder")
        assert isinstance(result, list)
        names = [c["name"] for c in result[0]["spec"]["initContainers"]]
        assert names == ["first", "second", "third"]

    async def test_lone_named_list_resolves_without_a_conversion_pass(self) -> None:
        # No generator and no transformer here, so nothing converts markers
        # after the merge. The type has to do it on its own.
        result = await evaluate_file(NIX_TEST_FILE, "loneNamedList")
        assert isinstance(result, list)
        assert result[0]["spec"]["containers"] == [{"name": "main", "image": "nginx"}]

    async def test_named_list_override_across_modules(self) -> None:
        # One module gives a plain rendered list, the shape a Helm chart
        # produces. Another patches one entry by name and adds a second.
        # The patched entry keeps its position, and the added entry gets a
        # `name` from its key.
        result = await evaluate_file(NIX_TEST_FILE, "namedListAcrossModules")
        assert isinstance(result, list)
        containers = result[0]["spec"]["containers"]
        assert containers == [
            {"name": "app", "image": "v2"},
            {"name": "log", "image": "fluentd"},
            {"name": "metrics", "image": "exporter"},
        ]

    async def test_named_list_survives_hoist_from_submodule(self) -> None:
        # A module declares its own option with the recursive kube type and
        # then lifts the result into kubernetes.objects, the way helm.nix and
        # importyaml.nix do.
        result = await evaluate_file(NIX_TEST_FILE, "hoistedFromSubmodule")
        assert isinstance(result, list)
        assert result[0]["spec"]["containers"] == [{"name": "main", "image": "api:1"}]

    async def test_marker_in_crds_is_rejected(self) -> None:
        # kubernetes.crds goes around the type for speed, so nothing there
        # resolves a marker. Fail instead of writing `_type` into a manifest.
        with pytest.raises(nanopynix.NixError, match=r"kubernetes\.crds"):
            await evaluate_file(NIX_TEST_FILE, "crdMarkerThrows")

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
            Session(experimental_features=["flakes", "nix-command"]) as session,
            session.store() as store,
            session.eval(store) as eval_,
        ):
            root = await eval_.string(nix)
            result = await root.to_python()
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
            Session(experimental_features=["flakes", "nix-command"]) as session,
            session.store() as store,
            session.eval(store) as eval_,
        ):
            root = await eval_.string(nix)
            result = await root.to_python()
        assert isinstance(result, str)
        assert "ConfigMap" in result
        assert "hello" in result


class TestValidationConfig:
    async def test_validation_config_builds(self) -> None:
        from ekn.eval import evaluate_validation_config

        flake = str(PROJECT_ROOT / "docs/examples/example-flake")
        cfg = await evaluate_validation_config(flake, "myapp")
        c = cfg.config
        assert c.kubernetes.package.version
        assert c.kubernetes.package.out_path.startswith("/nix/store/")
        assert c.validation.etcd_package.out_path.startswith("/nix/store/")
        assert c.validation.kubeconform_package.out_path.startswith("/nix/store/")
        assert isinstance(c.ekn.resource_priority, dict)
        # Helm's InstallOrder, so CRDs must sort before the custom resources
        # they establish -- and PriorityClass, being first, must be 0.
        assert c.ekn.resource_priority["PriorityClass"] == 0
        assert c.ekn.resource_priority["CustomResourceDefinition"] < c.ekn.resource_priority["Deployment"]

        # The one relationship that spans Nix and Python: an unlisted kind
        # (every custom resource) must sort *above* everything else in the
        # map and *below* the admission webhooks, or CR writes land behind
        # webhooks whose backend the same apply has not brought up yet. The
        # numbers live in easykubenix/ekn.nix, the fallback in ekn/apply.py;
        # nothing but this assertion holds them together.
        webhook_kinds = ["MutatingWebhookConfiguration", "ValidatingWebhookConfiguration"]
        others = [priority for kind, priority in c.ekn.resource_priority.items() if kind not in webhook_kinds]
        assert max(others) < _DEFAULT_BARRIER_PRIORITY
        assert all(c.ekn.resource_priority[kind] > _DEFAULT_BARRIER_PRIORITY for kind in webhook_kinds)
        assert c.ekn.discriminator
        assert c.internal.manifest_json_file.out_path.startswith("/nix/store/")
