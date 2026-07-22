let
  compat = import ../nix/compat.nix;
  pkgs = import compat.inputs.nixpkgs { };
  easy = import ../. {
    inherit pkgs;
    modules = [
      ({ config, ... }: {
        gitops.targets.apps = {
          branch = "deploy";
          path = "clusters/home/apps";
        };
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
  # Regression test: kubeListsToAttrs's `initContainers` order-preservation
  # exclusion was silently dead (`currentKey` always evaluated to `null`).
  # Mirrors how helm.nix/importyaml.nix actually feed raw chart/manifest
  # output through kubeListsToAttrs as a transformer, before kubeAttrsToLists
  # (always run at the end of generatedWithEkn) converts it back to a list.
  easyInitContainers = import ../. {
    inherit pkgs;
    modules = [
      (
        { lib, ... }:
        {
          kubernetes.transformers = [
            (object: (lib.walkWithPath (lib.kubeListsToAttrs object)) object)
          ];
          kubernetes.objects.default.Pod.test = {
            spec.initContainers = [
              {
                name = "first";
                image = "a";
              }
              {
                name = "second";
                image = "b";
              }
              {
                name = "third";
                image = "c";
              }
            ];
          };
        }
      )
    ];
  };
in
{
  eknRouting = {
    inherit (easy.config.kubernetes) generatedByPath gitopsTargets;
  };
  labelsAnnotationsCoercion = easyCoercion.config.kubernetes.generated;
  labelsAnnotationsCoercionDisabled = easyCoercionDisabled.config.kubernetes.generated;
  labelsAnnotationsCoercionDisabledThrows = easyCoercionDisabledThrows.config.kubernetes.generated;
  initContainersOrder = easyInitContainers.config.kubernetes.generated;
}
