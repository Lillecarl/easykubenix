from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import nanopynix
import pytest
from nanopynix.rpc import Session
from nanopynix_helpers.eval_target import EvaluationTargetError

from ekn.apply import DEFAULT_BARRIER_PRIORITY, DEFAULT_FIELD_MANAGER
from ekn.eval import (
    evaluate_file,
    evaluate_gitops_manifests,
    evaluate_kubeapply_config,
    realise_attr,
)

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
        sources_path = PROJECT_ROOT / "nix/sources.nix"
        f = tmp_path / "drv.nix"
        f.write_text(f"""
            let
              sources = import {sources_path};
              pkgs = import sources.nixpkgs {{ }};
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

    async def test_gitops_target_submodule_renders_into_its_target_only(self) -> None:
        """A target's `modules` are a separate easykubenix instance whose
        objects belong to that target and to nothing else.

        The separation is the feature: bootstrap objects are what makes a
        GitOps engine able to sync, so they cannot be synced by it. Leaking
        them into `kubernetes.generated` would put them in the path of a
        plain `ekn kubeapply` and of `ekn validate`, which is exactly the
        lifecycle they need to stay out of.
        """
        result = await evaluate_file(NIX_TEST_FILE, "gitOpsSubmodule")
        assert isinstance(result, dict)
        targets = result["gitOpsTargets"]

        # A target with only submodule objects and no routed ones still has
        # to appear -- that is the normal shape for a bootstrap target.
        assert sorted(targets) == ["apps", "bootstrap"]

        bootstrap = targets["bootstrap"]["objects"]
        assert [obj["metadata"]["name"] for obj in bootstrap] == ["root"]
        # Read through the `parent` module argument, so the nested instance
        # can point a root Application at the branch the parent syncs.
        assert bootstrap[0]["data"] == {"branch": "deploy"}

        # ...and nowhere else. `routed` is the parent's own object, and the
        # only one in `generated`.
        assert [obj["metadata"]["name"] for obj in result["generated"]] == ["routed"]
        assert [obj["metadata"]["name"] for obj in targets["apps"]["objects"]] == ["routed"]

    async def test_gitops_target_exposes_only_serializable_fields(self) -> None:
        """`target` must stay exactly `{path, discriminator, fieldManager}`.

        It is serialized to JSON for `ekn` (eval.py's `_GitOpsTargetRef` and
        `_unpack_gitops_target` read those three), while the target submodule
        itself also holds `modules` and `instance` -- module functions and a
        whole evaluated option tree -- plus `labels`/`annotations`, whose
        values may be functions. Passing the submodule through whole would
        try to serialize all of that, so this asserts the key set rather than
        just the values.
        """
        result = await evaluate_file(NIX_TEST_FILE, "gitOpsSubmodule")
        assert isinstance(result, dict)

        for name, entry in result["gitOpsTargets"].items():
            assert sorted(entry["target"]) == ["discriminator", "fieldManager", "path"], name

        assert result["gitOpsTargets"]["bootstrap"]["target"] == {
            "path": "bootstrap",
            "discriminator": "easykubenix-bootstrap",
            "fieldManager": "ekn",
        }
        # The nested instance agrees with the prune scope a
        # `--target bootstrap` apply will actually use.
        assert result["nestedDiscriminator"] == "easykubenix-bootstrap"

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

    async def test_a_kluctl_script_may_name_the_manifest(self) -> None:
        # `kluctl.preDeployScript = "... ${config.internal.manifestJSONFile}"`
        # is what you write to push a manifest before deploying it, and it
        # used to make the whole configuration unevaluatable: the kluctl
        # deprecation notice read the option's *priority*, which forces its
        # value, closing a loop back through `kubernetes.generated`. nixkube
        # writes exactly this and could not be evaluated at all.
        #
        # The notice is `lib.warn` on `kluctl.projectDir` now, so it fires
        # when somebody builds a kluctl project rather than when the module
        # system looks at a definition.
        result = await evaluate_file(NIX_TEST_FILE, "kluctlScriptReadsManifest")
        assert isinstance(result, list)
        assert result[0]["kind"] == "ConfigMap"

    async def test_conditional_definitions_patch_without_creating(self) -> None:
        # `kubernetes.objects` is a `conditionalAttrsOf` at the namespace, the
        # Kind and the object name. A `lib.mkIfExists` definition at any of
        # the three joins what is there and creates nothing that is not.
        result = await evaluate_file(NIX_TEST_FILE, "conditionalObjects")
        assert isinstance(result, list)
        assert [(o["kind"], o["metadata"]["name"]) for o in result] == [
            ("ConfigMap", "added"),
            ("Deployment", "api"),
            ("Deployment", "extra"),
        ]
        api = result[1]
        # Both marker forms reached the object that exists.
        assert api["spec"]["replicas"] == 3
        assert api["metadata"]["annotations"]["patched"] == "true"

    async def test_a_manifest_renders_without_a_discriminator(self) -> None:
        # `ekn.discriminator` has no default. Rendering must not read it:
        # a manifest is a file, and a file prunes nothing.
        result = await evaluate_file(NIX_TEST_FILE, "noDiscriminatorRenders")
        assert isinstance(result, list)
        assert result[0]["kind"] == "ConfigMap"

    async def test_a_deploy_without_a_discriminator_is_refused(self) -> None:
        # The other half, and the reason there is no default. A default is a
        # value every project shares until somebody changes it, and two
        # projects sharing one on a cluster prune each other's objects --
        # each sees objects under its own label that its own apply did not
        # produce. Nothing inside one project can detect that, so the name
        # has to be a decision, and this is where it gets asked for.
        with pytest.raises(nanopynix.NixError, match=r"ekn\.discriminator"):
            await evaluate_file(NIX_TEST_FILE, "noDiscriminatorDeployThrows")

    async def test_marker_in_crds_is_rejected(self) -> None:
        # kubernetes.crds goes around the type for speed, so nothing there
        # resolves a marker. Fail instead of writing `_type` into a manifest.
        with pytest.raises(nanopynix.NixError, match=r"kubernetes\.crds"):
            await evaluate_file(NIX_TEST_FILE, "crdMarkerThrows")

    async def test_if_exists_marker_in_crds_is_rejected(self) -> None:
        # `mkIfExists` leaks the same way. `conditionalAttrsOf` resolves it,
        # and a CRD goes around that type too.
        with pytest.raises(nanopynix.NixError, match=r"kubernetes\.crds"):
            await evaluate_file(NIX_TEST_FILE, "crdIfExistsMarkerThrows")

    async def test_generated_by_path(self, ekn_root: str) -> None:
        nix = f"""
        let
          easy = import {ekn_root} {{
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
          easy = import {ekn_root} {{
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


class TestGitOpsTargetMetadata:
    """A bootstrap target impersonating the controller that takes over.

    Two independent mechanisms, deliberately kept apart: `fieldManager` is
    apply-time only and decides who owns each *field*, while
    `labels`/`annotations` are baked into the rendered manifests and decide
    whether the GitOps engine considers the *object* its own at all.
    """

    @staticmethod
    def _by_name(entry: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {obj["metadata"]["name"]: obj for obj in entry["objects"]}

    async def test_field_manager_is_apply_time_only(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "gitOpsTargetMetadata")
        assert isinstance(result, dict)

        assert result["bootstrap"]["target"]["fieldManager"] == "argocd-controller"
        # An ordinary target keeps the default, so a normal apply stays
        # honest about who wrote what.
        assert result["apps"]["target"]["fieldManager"] == "ekn"

        # Never in the manifests: it is a property of who applied an object,
        # not of the object. `ekn commit` writes these to git.
        for obj in result["bootstrap"]["objects"]:
            assert "fieldManager" not in obj
            assert "fieldManager" not in obj["metadata"]

    async def test_metadata_covers_routed_and_submodule_objects_alike(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "gitOpsTargetMetadata")
        assert isinstance(result, dict)
        objects = self._by_name(result["bootstrap"])

        # `routed` comes from the parent via `ekn.gitOpsTarget`, `root` from
        # the target's own `modules`. A target's metadata is about the target,
        # so both sources get it.
        assert sorted(objects) == ["argocd", "root", "routed", "widgets.example.com"]
        for name in ("routed", "root"):
            assert objects[name]["metadata"]["labels"]["ekn.dev/kind"] == "ConfigMap", name

    async def test_target_labels_win_over_the_object_s_own(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "gitOpsTargetMetadata")
        assert isinstance(result, dict)
        root = self._by_name(result["bootstrap"])["root"]

        # `root` declares `app.kubernetes.io/instance = from-chart`, the way a
        # Helm chart sets it to its release name. That is exactly the key a
        # GitOps engine may read to decide ownership, so losing this fight
        # would defeat the stamp.
        assert root["metadata"]["labels"]["app.kubernetes.io/instance"] == "argocd"

    async def test_tracking_id_encodes_each_object_s_own_identity(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "gitOpsTargetMetadata")
        assert isinstance(result, dict)
        objects = self._by_name(result["bootstrap"])

        def tracking(name: str) -> str | None:
            return objects[name]["metadata"].get("annotations", {}).get("argocd.argoproj.io/tracking-id")

        # A constant would be wrong on all but one object -- and wrong here
        # fails silently, because ArgoCD reads a non-self-referencing id as
        # naming something else and then never prunes the object.
        assert tracking("root") == "argocd:/ConfigMap:argocd/root"
        # Cluster-scoped (`none` namespace): no `metadata.namespace`, so it
        # falls back to the Application's destination namespace, as ArgoCD
        # itself does.
        assert tracking("argocd") == "argocd:/Namespace:argocd/argocd"
        # apiextensions.k8s.io is a real group, unlike the core group above.
        assert objects["widgets.example.com"]["apiVersion"] == "apiextensions.k8s.io/v1"

    async def test_crds_are_declined_rather_than_stamped(self) -> None:
        """A function returning `null` leaves that one object alone.

        ArgoCD's repo-server skips CRDs when it stamps rendered manifests
        (`!kube.IsCRD(target)`), and `Normalize` skips them again on the live
        side. A tracking annotation ArgoCD's desired state never contains but
        the live object does is a diff on every sync, forever -- on exactly
        the objects a bootstrap target is most likely applying.
        """
        result = await evaluate_file(NIX_TEST_FILE, "gitOpsTargetMetadata")
        assert isinstance(result, dict)
        crd = self._by_name(result["bootstrap"])["widgets.example.com"]

        assert "argocd.argoproj.io/tracking-id" not in crd["metadata"].get("annotations", {})
        # Declining one value must not suppress the others, and must not
        # leave an empty `annotations: {}` behind either.
        assert "annotations" not in crd["metadata"]
        assert crd["metadata"]["labels"]["ekn.dev/kind"] == "CustomResourceDefinition"

    async def test_a_target_declaring_no_metadata_stamps_none(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "gitOpsTargetMetadata")
        assert isinstance(result, dict)
        unstamped = self._by_name(result["apps"])["unstamped"]

        # The control for everything above: being in a target is not what
        # stamps an object, declaring the metadata is. `metadata` here is
        # untouched -- no empty `labels: {}` either, which would show up as a
        # spurious field in every committed manifest.
        assert sorted(unstamped["metadata"]) == ["name", "namespace"]


class TestGitOpsTargetSubmoduleEndToEnd:
    """The two CLI paths a bootstrap target is actually used through, against
    a real evaluation rather than a hand-built shape.

    Both are unchanged Python: the join happens in `kubernetes.gitOpsTargets`,
    so `ekn commit` and `ekn kubeapply --target` see submodule objects through
    the same fields they already read. These tests are what says so.
    """

    @staticmethod
    def _probe(tmp_path: Path) -> Path:
        probe = tmp_path / "bootstrap.nix"
        probe.write_text(f"""
            import {PROJECT_ROOT} {{
              modules = [{{
                # No default for this, and every target derives its own
                # prune scope from it -- see easykubenix/ekn.nix.
                ekn.discriminator = "easykubenix";
                gitOps.deployBranch = "deploy";
                gitOps.targets.bootstrap = {{
                  path = "bootstrap";
                  fieldManager = "argocd-controller";
                  labels."app.kubernetes.io/instance" = "argocd";
                  modules = [{{
                    kubernetes.objects.argocd.ConfigMap.root.data.key = "value";
                  }}];
                }};
              }}];
            }}
        """)
        return probe

    async def test_commit_renders_submodule_objects_into_the_target_path(self, tmp_path: Path) -> None:
        from ekn.gitops import branches, file_groups

        result = await evaluate_gitops_manifests(self._probe(tmp_path), None, None, None)

        assert branches(result) == ("deploy", None)
        rendered = dict(file_groups(result))
        assert "bootstrap/argocd/ConfigMap/root.yaml" in rendered
        manifest = rendered["bootstrap/argocd/ConfigMap/root.yaml"]
        assert "key: value" in manifest
        # The target's labels are baked in, so the committed YAML and what a
        # `--target bootstrap` apply sends are the same object. Whether a
        # GitOps engine adopts it is decided by what is in git.
        assert "app.kubernetes.io/instance: argocd" in manifest
        # ...and `fieldManager` is not, because it says who applied an object
        # rather than anything about the object.
        assert "fieldManager" not in manifest

    async def test_kubeapply_target_applies_submodule_objects(self, tmp_path: Path) -> None:
        cfg = await evaluate_kubeapply_config(self._probe(tmp_path), None, None, None, "bootstrap")

        assert [obj["metadata"]["name"] for obj in cfg.objects] == ["root"]
        # The target's own prune scope, not the instance-wide one -- pruning
        # a bootstrap apply must not be able to reach anything else.
        assert cfg.discriminator == "easykubenix-bootstrap"
        # Applied as the controller that takes over, so SSA hands the fields
        # across instead of leaving `ekn` owning them permanently.
        assert cfg.field_manager == "argocd-controller"
        assert cfg.objects[0]["metadata"]["labels"] == {"app.kubernetes.io/instance": "argocd"}

    async def test_field_manager_defaults_without_a_target(self, tmp_path: Path) -> None:
        # No `--target`: the objects come from `kubernetes.generated`, which
        # belongs to no target and so has nobody to name a manager.
        probe = tmp_path / "plain.nix"
        probe.write_text(f"""
            import {PROJECT_ROOT} {{
              modules = [{{
                # A kubeapply config carries the prune scope, so it has to
                # name one -- this test is about the field manager.
                ekn.discriminator = "easykubenix";
                kubernetes.objects.default.ConfigMap.plain.data.key = "value";
              }}];
            }}
        """)

        cfg = await evaluate_kubeapply_config(probe, None, None, None, None)

        assert cfg.field_manager == DEFAULT_FIELD_MANAGER


class TestAssertionsAndWarnings:
    """`assertions` and `warnings` are collected from every module, but a plain
    `lib.evalModules` has nothing playing the part NixOS' top-level.nix plays,
    so unless a rendered output forces them they are gathered and discarded.
    `kubernetes.nix` routes its outputs through `lib.asserts.checkAssertWarn`
    to close that; these are the tests that it stays closed.
    """

    @staticmethod
    def _probe(tmp_path: Path, module_body: str) -> Path:
        probe = tmp_path / "probe.nix"
        probe.write_text(f"""
            (import {PROJECT_ROOT} {{
              modules = [{{
                {module_body}
                kubernetes.objects.default.ConfigMap.test.data.key = "hello";
              }}];
            }}).config
        """)
        return probe

    async def test_warning_reaches_stderr(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """The regression this guards is silence, not a wrong value.

        A Nix evaluation warning reaches this side only as a log event, and
        the worker filters events by level *before* they hit the session bus
        -- so at the old default verbosity of "error" every `config.warnings`
        entry and every `mkRenamedOptionModule` deprecation notice was
        discarded inside Nix. Manifests came out perfectly correct and the
        user was told nothing, while `nix build` on the same config printed
        the warning.
        """
        probe = self._probe(tmp_path, 'warnings = [ "deliberate probe warning" ];')

        result = await evaluate_file(probe, "kubernetes.generated")

        assert isinstance(result, list)
        assert len(result) == 1
        assert "deliberate probe warning" in capsys.readouterr().err

    @pytest.mark.parametrize("attr", ["kubernetes.generated", "kubernetes.generatedWithEkn"])
    async def test_failing_assertion_aborts(self, tmp_path: Path, attr: str) -> None:
        """Both rendered outputs, not just `generated`. `generatedWithEkn` is
        the same objects with their `ekn` routing still attached, so reading it
        must not be a way around a config's own assertions.
        """
        probe = self._probe(
            tmp_path,
            'assertions = [{ assertion = false; message = "deliberate probe failure"; }];',
        )

        with pytest.raises(nanopynix.NixError, match="deliberate probe failure"):
            await evaluate_file(probe, attr)

    async def test_a_nested_instances_assertions_still_fire(self, tmp_path: Path) -> None:
        """A GitOps target's `modules` are their own instance with their own
        `assertions`. Nothing plumbs them up to the parent -- the nested
        `kubernetes.nix` runs its own checker when its `generated` is forced,
        and forcing the parent's `gitOpsTargets` is what forces that.
        """
        probe = tmp_path / "nested.nix"
        probe.write_text(f"""
            (import {PROJECT_ROOT} {{
              modules = [{{
                gitOps.deployBranch = "deploy";
                gitOps.targets.bootstrap.modules = [{{
                  assertions = [{{ assertion = false; message = "deliberate nested failure"; }}];
                  kubernetes.objects.argocd.ConfigMap.root.data.key = "value";
                }}];
              }}];
            }}).config
        """)

        with pytest.raises(nanopynix.NixError, match="deliberate nested failure"):
            await evaluate_file(probe, "kubernetes.gitOpsTargets")


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
        # Helm's InstallOrder in tens, so PriorityClass leads at 10 and CRDs
        # sort before the custom resources they establish.
        assert c.ekn.resource_priority["PriorityClass"] == 10
        assert c.ekn.resource_priority["CustomResourceDefinition"] < c.ekn.resource_priority["Deployment"]

        # The one relationship that spans Nix and Python. An unlisted kind --
        # every custom resource -- sorts at DEFAULT_BARRIER_PRIORITY, which
        # must land above every ordinary kind and below the three that
        # intercept later requests. Get it wrong and CR writes queue behind a
        # webhook or aggregated APIService whose backend this same apply has
        # not finished bringing up. The numbers live in easykubenix/ekn.nix
        # and the fallback in ekn/apply.py; nothing but this holds them
        # together.
        intercepting = ["APIService", "MutatingWebhookConfiguration", "ValidatingWebhookConfiguration"]
        ordinary = [priority for kind, priority in c.ekn.resource_priority.items() if kind not in intercepting]
        assert max(ordinary) < DEFAULT_BARRIER_PRIORITY
        assert all(c.ekn.resource_priority[kind] > DEFAULT_BARRIER_PRIORITY for kind in intercepting)
        assert c.ekn.discriminator
        assert c.internal.manifest_json_file.out_path.startswith("/nix/store/")
