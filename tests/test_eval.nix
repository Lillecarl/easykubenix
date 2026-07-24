let
  compat = import ../nix/compat.nix;
  pkgs = import compat.inputs.nixpkgs { };
  easy = import ../. {
    inherit pkgs;
    modules = [
      ({ config, ... }: {
        gitOps.deployBranch = "deploy";
        gitOps.targets.apps.path = "clusters/home/apps";
        kubernetes.objects.default.Deployment.api = {
          ekn.gitOpsTarget = "apps";
          apiVersion = "apps/v1";
        };
      })
    ];
  };
  easyCoercion = import ../. {
    inherit pkgs;
    modules = [
      {
        kubernetes.objects.default.ConfigMap.coerced = {
          metadata.labels.enabled = true;
          metadata.labels.replicas = 3;
          metadata.annotations.disabled = false;
          data.key = "value";
        };
      }
    ];
  };
  easyCoercionDisabled = import ../. {
    inherit pkgs;
    modules = [
      {
        kubernetes.coerceLabelsAndAnnotations = false;
        kubernetes.objects.default.ConfigMap.uncoerced = {
          metadata.labels.enabled = "true";
          data.key = "value";
        };
      }
    ];
  };
  # Top-level `metadata.labels`/`metadata.annotations` are the typed
  # `labelValueType` option -- with coercion disabled that's plain
  # `types.str`, so a bool there is rejected by the module system itself.
  easyCoercionDisabledThrows = import ../. {
    inherit pkgs;
    modules = [
      {
        kubernetes.coerceLabelsAndAnnotations = false;
        kubernetes.objects.default.ConfigMap.uncoerced = {
          metadata.labels.enabled = true;
          data.key = "value";
        };
      }
    ];
  };
  # A transformer runs after the option merge and returns a plain value that no
  # option type sees. Thus a transformer can put a marker into an object, and
  # the pipeline must change that marker back into a list. The order of the init
  # containers is important, so the numbered marker must keep it. This is the
  # case that `needsMarkerPass` in kubernetes.nix guards.
  easyInitContainers = import ../. {
    inherit pkgs;
    modules = [
      (
        { lib, ... }:
        {
          kubernetes.transformers = [
            (
              object:
              object
              // {
                spec = object.spec // {
                  initContainers = lib.mkNumberedList {
                    "0" = {
                      name = "first";
                      image = "a";
                    };
                    "1" = {
                      name = "second";
                      image = "b";
                    };
                    "2" = {
                      name = "third";
                      image = "c";
                    };
                  };
                };
              }
            )
          ];
          kubernetes.objects.default.Pod.test = {
            spec.initContainers = [ ];
          };
        }
      )
    ];
  };
  # `mkNamedList` inside `kubernetes.objects` with no other definition at the
  # same path. `ekn.lib.kubeValueType` must resolve it on its own, so the
  # rendered manifest holds a real list and no `_type` marker. Nothing converts
  # markers after the merge unless a generator or a transformer runs.
  easyLoneNamedList = import ../. {
    inherit pkgs;
    modules = [
      (
        { lib, ... }:
        {
          kubernetes.objects.default.Pod.solo.spec.containers = lib.mkNamedList {
            main.image = "nginx";
          };
        }
      )
    ];
  };

  # Two modules define the same object. One gives a plain rendered list, the
  # same shape a Helm chart produces. The other patches one entry by name and
  # adds a second entry. This is the merge that removes the need for a
  # pre-conversion pass in helm.nix and importyaml.nix.
  easyNamedListAcrossModules = import ../. {
    inherit pkgs;
    modules = [
      {
        kubernetes.objects.default.Pod.web.spec.containers = [
          {
            name = "app";
            image = "v1";
          }
          {
            name = "log";
            image = "fluentd";
          }
        ];
      }
      (
        { lib, ... }:
        {
          kubernetes.objects.default.Pod.web.spec.containers = lib.mkNamedList {
            app.image = lib.mkForce "v2";
            metrics.image = "exporter";
          };
        }
      )
    ];
  };

  # A module declares its own option with the recursive kube type, then hoists
  # the result into `kubernetes.objects`. This is the shape helm.nix and
  # importyaml.nix use. A marker must survive the hoist and still resolve.
  easyHoistedFromSubmodule = import ../. {
    inherit pkgs;
    modules = [
      (
        {
          config,
          lib,
          ekn,
          ...
        }:
        {
          options.myApps = lib.mkOption {
            type = lib.types.attrsOf (lib.types.submodule { freeformType = ekn.lib.kubeValueType; });
            default = { };
          };
          config.myApps.api = {
            apiVersion = "v1";
            kind = "Pod";
            metadata.name = "api";
            spec.containers = lib.mkNamedList { main.image = "api:1"; };
          };
          # The same one-key-attrset plus mkMerge lift that helm.nix uses.
          config.kubernetes.objects = lib.mkMerge (
            lib.mapAttrsToList (_: object: {
              ${object.metadata.namespace or "none"}.${object.kind}.${object.metadata.name} = object;
            }) config.myApps
          );
        }
      )
    ];
  };

  # `kubernetes.crds` goes around the type on purpose, so nothing there can
  # resolve a marker. Forcing this must throw rather than write a manifest with
  # a literal `_type` field.
  easyCrdMarkerThrows =
    (import ../. {
      inherit pkgs;
      modules = [
        (
          { lib, ... }:
          {
            kubernetes.crds = [
              {
                apiVersion = "apiextensions.k8s.io/v1";
                kind = "CustomResourceDefinition";
                metadata.name = "widgets.example.com";
                spec.versions = lib.mkNamedList { v1.served = true; };
              }
            ];
          }
        )
      ];
    }).config.kubernetes.generated;
in
{
  eknRouting = {
    inherit (easy.config.kubernetes) generatedByPath gitOpsTargets;
  };
  labelsAnnotationsCoercion = easyCoercion.config.kubernetes.generated;
  labelsAnnotationsCoercionDisabled = easyCoercionDisabled.config.kubernetes.generated;
  labelsAnnotationsCoercionDisabledThrows = easyCoercionDisabledThrows.config.kubernetes.generated;
  initContainersOrder = easyInitContainers.config.kubernetes.generated;
  loneNamedList = easyLoneNamedList.config.kubernetes.generated;
  namedListAcrossModules = easyNamedListAcrossModules.config.kubernetes.generated;
  hoistedFromSubmodule = easyHoistedFromSubmodule.config.kubernetes.generated;
  # Forcing this one must throw -- exposed as a thunk so the test can assert on
  # the error without eagerly evaluating it above.
  crdMarkerThrows = easyCrdMarkerThrows;
}
