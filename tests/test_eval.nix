let
  compat = import ../nix/compat.nix;
  pkgs = import compat.inputs.nixpkgs { };
  easy = import ../. {
    inherit pkgs;
    modules = [
      ({ config, ... }: {
        gitops.targets.apps = {
          branch = "deploy";
          path = "clusters/home/apps";
        };
        kubernetes.objects.default.Deployment.api = {
          ekn.gitOpsTarget = "apps";
          apiVersion = "apps/v1";
        };
      })
    ];
  };
in
{
  eknRouting = {
    inherit (easy.config.kubernetes) generatedByPath gitopsTargets;
  };
}
