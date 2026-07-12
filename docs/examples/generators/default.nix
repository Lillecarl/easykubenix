{
  pkgs,
  lib,
  easykubenix,
}:
let
  inherit (pkgs) lib;

  ekn = easykubenix {
    inherit pkgs;
    modules = [
      {
        # Generator: auto-create VPA for Deployments that have the annotation
        kubernetes.generators = [
          (
            object:
            lib.optionalAttrs
              (
                object.kind or "" == "Deployment"
                && (object.metadata.annotations.genvpa or "true") == "true"
              )
              {
                apiVersion = "autoscaling.k8s.io/v1";
                kind = "VerticalPodAutoscaler";
                metadata = {
                  name = object.metadata.name;
                  namespace = object.metadata.namespace;
                };
                spec.targetRef = {
                  apiVersion = "apps/v1";
                  kind = "Deployment";
                  name = object.metadata.name;
                };
                spec.updatePolicy.updateMode = "Auto";
              }
          )
        ];

        # Transformer: add annotations to all Services
        kubernetes.transformers = [
          (
            object:
            if object.kind == "Service" then
              lib.recursiveUpdate object {
                metadata.annotations."example.io/managed-by" = "easykubenix";
              }
            else
              object
          )
        ];

        # Filter: remove any object whose kind is Pod
        kubernetes.filters = [
          (object: object.kind or "" != "Pod")
        ];

        kubernetes.objects = {
          default.Deployment.myapp = {
            spec.selector.matchLabels.app = "myapp";
            spec.template.metadata.labels.app = "myapp";
            spec.template.spec.containers = [
              { name = "myapp"; image = "nginx:alpine"; }
            ];
          };

          default.Service.myapp = {
            spec.ports = [{ port = 80; }];
            spec.selector.app = "myapp";
          };

          # This Pod should be filtered out by the filter above
          default.Pod.should-be-filtered = {
            spec.containers = [
              { name = "test"; image = "alpine"; }
            ];
          };
        };
      }
    ];
  };
in
{
  manifestJSON = ekn.manifestJSON;
  check = import ../verify.nix {
    inherit pkgs lib;
    name = "generators-transformers-filters";
    manifestJSON = ekn.manifestJSON;
  };
}
