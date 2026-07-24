{ pkgs, lib, ... }:
{
  config.kubernetes.resources = {
    default.Secret."my-app-config" = {
      # No need to set namespace or name here
      metadata = {
        labels.app = "my-app";
      };
      stringData = {
        "config.yaml" = "some-value";
        "storePath" = toString pkgs.fishMinimal;
      };
    };
    default.Deployment.test-render = {
      spec = {
        replicas = 0;
        selector = {
          matchLabels = {
            app = "test-render";
          };
        };
        template = {
          metadata = {
            labels = {
              app = "test-render";
            };
          };
          spec = {
            # The attribute name becomes the `name` of the element. You do not
            # have to repeat it in the value.
            containers = lib.mkNamedList {
              main-app = {
                image = "registry.k8s.io/pause:3.9";
                env = lib.mkNamedList {
                  ASDF.value = "fdsa";
                };
              };
              sidecar = {
                image = "registry.k8s.io/pause:3.9";
              };
            };
          };
        };
      };
    };
  };
}
