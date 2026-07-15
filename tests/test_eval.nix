let
  compat = import ../nix/compat.nix;
  pkgs = import compat.inputs.nixpkgs { };
  easy = import ../. {
    inherit pkgs;
    modules = [
      ({ config, ... }: {
        kubernetes.objects.argocd.Application.apps = {
          apiVersion = "argoproj.io/v1alpha1";
          spec.source = {
            repoURL = "ssh://git@example.test/platform.git";
            targetRevision = "deploy";
            path = "clusters/home/apps";
          };
        };
        kubernetes.objects.default.Deployment.api = {
          ekn.argo = [
            config.kubernetes.objects.argocd.Application.apps
          ];
          apiVersion = "apps/v1";
        };
      })
    ];
  };
in
{
  eknRouting = {
    inherit (easy.config.kubernetes) generatedByPath eknByPath;
  };
}
