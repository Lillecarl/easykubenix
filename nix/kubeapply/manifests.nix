# What the integration test applies, as easykubenix module sets.
#
# Four evaluations of this repository's own modules, so the thing under test
# is `kubernetes.generated` -> `internal.manifestJSONFile` -> `_applyManifest`
# -> a real API server. Hand-written JSON would test the last arrow only.
#
# Three generations of one configuration, plus two sets that no prune of
# those three may touch:
#
#   gen1    everything, plus two objects the next generation drops
#   gen2    gen1 without them, so the apply has to prune exactly those two
#   gen3    gen2 without its last WidgetPart, which is the documented
#           prune gap -- see `apply_and_prune`'s docstring
#   other   a second environment in a namespace of its own
#   unit    a deployment unit of the *same* environment, separated from the
#           three above only by its `ekn.dev/deployment-unit` label
#
# `WidgetPart` is deliberate. Its singular carries a hyphen, so it is the
# multus shape that `ekn.apply.discover` exists for: kr8s' own
# `async_lookup_kind` lowercases the Kind and matches that against the
# plural, the Kind, the singular and the short names, and
# `widgetpart` is none of `widget-parts`, `WidgetPart` or `widget-part`.
# tests/test_apply.py describes that against a double. This describes it
# against apiextensions.
{
  workloadImage,
}:
let
  namespace = "ekn-kubeapply";
  otherNamespace = "ekn-kubeapply-other";

  group = "ekn.example.com";
  apiVersion = "${group}/v1";

  # `kubernetes.crds` and not `kubernetes.objects`: a CRD is already a
  # complete object, and that option skips the per-leaf value checking an
  # OpenAPI schema makes expensive. See kubernetes.nix.
  widgetPartCrd = {
    apiVersion = "apiextensions.k8s.io/v1";
    kind = "CustomResourceDefinition";
    metadata.name = "widget-parts.${group}";
    spec = {
      inherit group;
      scope = "Namespaced";
      names = {
        plural = "widget-parts";
        singular = "widget-part";
        kind = "WidgetPart";
        listKind = "WidgetPartList";
      };
      versions = [
        {
          name = "v1";
          served = true;
          storage = true;
          schema.openAPIV3Schema = {
            type = "object";
            properties.spec = {
              type = "object";
              properties.size.type = "integer";
            };
          };
        }
      ];
    };
  };

  # The kind is served by the CRD above and by nothing in
  # `apiResources/v1.33.json`, so this configuration has to say what its
  # apiVersion is. That is what `apiMappings` is for.
  widgetPartMapping = {
    kubernetes.apiMappings.WidgetPart = apiVersion;
  };

  # What every generation carries. Five kinds over four barriers: the
  # Namespace first, the CRD before anything of its kind, and the
  # WidgetParts last at DEFAULT_BARRIER_PRIORITY.
  common = {
    imports = [ widgetPartMapping ];

    ekn.environment = "kubeapply";

    kubernetes.crds = [ widgetPartCrd ];

    kubernetes.objects = {
      none.Namespace.${namespace} = { };

      ${namespace} = {
        ConfigMap.settings.data.greeting = "hello";

        # A workload, so the apply is answered by a kubelet and not only by
        # etcd. `imagePullPolicy: Never` because there is no registry behind
        # `workloadImage`; modules/k8s.nix imports it into containerd at boot.
        Deployment.probe.spec = {
          replicas = 1;
          selector.matchLabels.app = "probe";
          template = {
            metadata.labels.app = "probe";
            spec.containers = [
              {
                name = "probe";
                image = workloadImage;
                imagePullPolicy = "Never";
                command = [
                  "/bin/sleep"
                  "3600"
                ];
              }
            ];
          };
        };
      };
    };
  };
  # A configuration whose objects all live in one deployment unit. Same
  # environment and same namespace as the three generations above -- only the
  # rendered `ekn.dev/deployment-unit` label separates them, which is the
  # whole point. Its objects are read from `kubernetes.deploymentUnits` (see
  # ./default.nix), because that is where the unit label is stamped.
  unitWith = objects: {
    ekn.environment = "kubeapply";
    deployment.enable = true;
    deployment.deployBranch = "deploy";
    deployment.units.bootstrap.modules = [ { kubernetes.objects.${namespace} = objects; } ];
  };
in
{
  inherit namespace otherNamespace;

  environment = "kubeapply";
  otherEnvironment = "kubeapply-other";
  unit = "bootstrap";

  modules = {
    gen1 = {
      imports = [ common ];
      kubernetes.objects.${namespace} = {
        # The two objects gen2 drops: one builtin kind and one custom kind.
        # The custom one is the point -- pruning lists a kind back through
        # the class the apply built for it, and only a CRD gets that class
        # from `discover`.
        ConfigMap.stale.data.greeting = "goodbye";
        WidgetPart.beta.spec.size = 2;
        WidgetPart.alpha.spec.size = 1;
      };
    };

    gen2 = {
      imports = [ common ];
      kubernetes.objects.${namespace}.WidgetPart.alpha.spec.size = 1;
    };

    # No WidgetPart at all, so the prune scan never looks at that kind.
    gen3 = {
      imports = [ common ];
    };

    other = {
      imports = [ widgetPartMapping ];
      ekn.environment = "kubeapply-other";
      kubernetes.objects = {
        none.Namespace.${otherNamespace} = { };
        ${otherNamespace}.WidgetPart.gamma.spec.size = 3;
      };
      # No CRD here. The test applies gen1 first, which establishes it, and
      # a second apply of the same CRD would only move its field ownership
      # and its environment label around for no reason.
    };

    # The same environment as the three generations, and the same namespace.
    # Only the rendered `ekn.dev/deployment-unit` label separates them, which
    # is the whole point: this stands in for the bootstrap unit a whole-
    # instance prune must never reach, and whose own prune must reach nothing
    # else. Its objects are read from `kubernetes.deploymentUnits` (see
    # ./default.nix), because that is where the unit label is stamped.
    unit = unitWith {
      ConfigMap.bootstrap.data.greeting = "from-unit";
      # Dropped by `unitReduced`, so the unit's own prune has something to
      # delete inside its own scope.
      ConfigMap.bootstrap-extra.data.greeting = "also-from-unit";
    };
    unitReduced = unitWith {
      ConfigMap.bootstrap.data.greeting = "from-unit";
    };
  };
}
