{ pkgs, ... }:
{
  config = {
    kubernetes.resources.default.ConfigMap."my-app-config" = {
      # No need to set namespace or name here
      metadata = {
        labels.app = "my-app";
      };
      stringData = {
        "config.yaml" = "some-value";
        "storePath" = toString pkgs.fishMinimal;
      };
    };
  };
}
