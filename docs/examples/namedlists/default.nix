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
              # `mkNamedList` gives an explicit named list. The attribute name
              # becomes the `name` of the container. `pkgs.lib` here is the
              # extended lib, which holds easykubenix' own helpers.
              containers = pkgs.lib.mkNamedList {
                main = {
                  image = "nginx:alpine";
                  ports = [ { containerPort = 80; } ];
                  env = pkgs.lib.mkNamedList {
                    FOO.value = "bar";
                    MODE.value = "test";
                  };
                };
                sidecar = {
                  image = "fluentd:latest";
                };
              };

              # A plain list keeps its order. Use `mkNumberedList` to override
              # one entry of it by index.
              initContainers = [
                {
                  name = "init-step1";
                  image = "busybox";
                  command = [
                    "echo"
                    "step1"
                  ];
                }
                {
                  name = "init-step2";
                  image = "busybox";
                  command = [
                    "echo"
                    "step2"
                  ];
                }
              ];
            };
          };

          # A control case: plain containers with no marker at all.
          default.Deployment.plain-containers = {
            spec.selector.matchLabels.app = "plain";
            spec.template.metadata.labels.app = "plain";
            spec.template.spec.containers = [
              {
                name = "c1";
                image = "nginx:alpine";
              }
              {
                name = "c2";
                image = "alpine:latest";
                command = [
                  "sleep"
                  "infinity"
                ];
              }
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
