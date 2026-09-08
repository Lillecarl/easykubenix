{
  pkgs,
  lib,
  easykubenix,
}:
let
  # Stand-in for something you do not own: a chart, a vendored module, a
  # shared platform module. It ships one Deployment today. It can stop.
  upstream = {
    kubernetes.objects.default.Deployment.web = {
      spec.replicas = 1;
      spec.selector.matchLabels.app = "web";
      spec.template.metadata.labels.app = "web";
      spec.template.spec.containers = pkgs.lib.mkNamedList {
        main.image = "nginx:alpine";
      };
    };
  };

  # The patch. Every definition here is conditional, so none of it creates an
  # object. An ordinary definition would: writing
  # `kubernetes.objects.default.Deployment.cache.spec.replicas = 5` renders a
  # Deployment with that one field and nothing else, the moment nobody else
  # defines `cache`.
  overlay =
    { lib, ... }:
    {
      kubernetes.objects = lib.mkMerge [
        # The path form. All three of the namespace, the Kind and the name
        # must already exist for the content to land.
        (lib.mkIfExistsAtPath "default.Deployment.web" {
          spec.replicas = lib.mkForce 3;
          metadata.annotations."example.com/patched" = "true";
        })

        # Nothing named `cache` exists, so this adds nothing at all.
        (lib.mkIfExistsAtPath "default.Deployment.cache" {
          spec.replicas = lib.mkForce 5;
        })

        {
          # `mkIfExists` conditions the entry it is written on, and the rule
          # applies again at each level below it.
          default = lib.mkIfExists {
            # The namespace exists, so a child of it is added normally.
            ConfigMap.web-config.data.mode = "production";
            # This Kind does not exist, so nothing under it is created.
            CronJob = lib.mkIfExists {
              nightly.spec.schedule = "0 0 * * *";
            };
          };
        }
      ];
    };

  ekn = easykubenix {
    inherit pkgs;
    modules = [
      upstream
      overlay
    ];
  };
in
{
  manifestJSON = ekn.manifestJSON;
  check = import ../verify.nix {
    inherit pkgs lib;
    name = "conditional";
    manifestJSON = ekn.manifestJSON;
  };
}
