{
  pkgs,
  lib,
  easykubenix,
}:
let
  ekn = easykubenix {
    inherit pkgs;
    modules = [
      {
        kubernetes.objects = {
          default.Deployment.namedlist-demo = {
            spec.selector.matchLabels.app = "namedlist-demo";
            spec.template.metadata.labels.app = "namedlist-demo";
            spec.template.spec = {
              # Use mkNamedList for explicit _namedlist containers
              containers = ekn.lib.mkNamedList {
                main = {
                  image = "nginx:alpine";
                  ports = [{ containerPort = 80; }];
                  env = ekn.lib.mkNamedList {
                    FOO.value = "bar";
                    MODE.value = "test";
                  };
                };
                sidecar = {
                  image = "fluentd:latest";
                };
              };

              # initContainers should use _numberedlist preserving order
              initContainers = [
                { name = "init-step1"; image = "busybox"; command = ["echo" "step1"]; }
                { name = "init-step2"; image = "busybox"; command = ["echo" "step2"]; }
              ];
            };
          };

          # Test plain containers without _namedlist
          default.Deployment.plain-containers = {
            spec.selector.matchLabels.app = "plain";
            spec.template.metadata.labels.app = "plain";
            spec.template.spec.containers = [
              { name = "c1"; image = "nginx:alpine"; }
              { name = "c2"; image = "alpine:latest"; command = ["sleep" "infinity"]; }
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
    name = "namedlists";
    manifestJSON = ekn.manifestJSON;
  };
}
