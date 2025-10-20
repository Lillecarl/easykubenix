{ pkgs, ... }:
{
  config.kubernetes.resources = {
    default.ConfigMap."my-app-config" = {
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
            containers = {
              _namedlist = true;
              main-app = {
                name = "main-app";
                image = "registry.k8s.io/pause:3.9";
                env = {
                  _namedlist = true;
                  ASDF.value = "fdsa";
                };
              };
              sidecar = {
                name = "sidecar";
                image = "registry.k8s.io/pause:3.9";
              };
            };
          };
        };
      };
    };
  };
}
