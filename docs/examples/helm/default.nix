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
        helm.releases."hello-world" = {
          chart = pkgs.fetchHelm {
            repo = "https://kubernetes.github.io/ingress-nginx";
            chart = "ingress-nginx";
            version = "4.12.1";
            sha256 = "sha256-GEzgtcwJ+lZ9ymCDey54bD7BZkCpXoDcIQU0MjzAxcA=";
          };
          namespace = "ingress-nginx";
          values = {
            controller.admissionWebhooks.enabled = false;
            controller.ingressClassResource.enabled = false;
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
    name = "helm-chart";
    manifestJSON = ekn.manifestJSON;
  };
}
