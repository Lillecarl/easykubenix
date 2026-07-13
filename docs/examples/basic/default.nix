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
        kubernetes.objects = {
          default.ConfigMap.app-config = {
            data = {
              "app.properties" = "key=value\nmode=production";
            };
          };

          default.Secret.db-credentials = {
            stringData = {
              username = "admin";
              password = "s3cret";
            };
          };

          default.Pod.webserver = {
            spec.containers = [
              {
                name = "nginx";
                image = "nginx:alpine";
                ports = [ { containerPort = 80; } ];
              }
            ];
          };

          default.Deployment.app = {
            spec.replicas = 3;
            spec.selector.matchLabels.app = "app";
            spec.template.metadata.labels.app = "app";
            spec.template.spec.containers = [
              {
                name = "app";
                image = "nginx:alpine";
                ports = [ { containerPort = 80; } ];
              }
            ];
          };

          default.Service.app = {
            spec.ports = [
              {
                port = 80;
                targetPort = 80;
              }
            ];
            spec.selector.app = "app";
          };
        };
      }
    ];
  };
in
{
  manifestJSON = ekn.manifestJSON;
  manifestYAMLFile = ekn.manifestYAMLFile;
  check = import ../verify.nix {
    inherit pkgs lib;
    name = "basic-resources";
    manifestJSON = ekn.manifestJSON;
  };
}
