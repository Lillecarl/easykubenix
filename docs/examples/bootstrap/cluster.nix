# The outer instance: everything ArgoCD syncs, plus the bootstrap target that
# gets ArgoCD running in the first place.
{ lib, ... }:
{
  options.clusterRepoURL = lib.mkOption {
    type = lib.types.str;
    description = ''
      Git remote the deploy branch is pushed to. Not an easykubenix option --
      declared here to show that a bootstrap instance reaches its parent's
      whole config through `parent`, including options the user's own modules
      add.
    '';
  };

  config = {
    clusterRepoURL = "https://github.com/example/cluster.git";

    gitOps = {
      enable = true;
      deployBranch = "deploy";

      targets = {
        # The ordinary target: ArgoCD syncs this one, and nothing here is ever
        # applied by hand.
        apps.path = "clusters/example/apps";

        # The bootstrap target: its own easykubenix instance, rendered into
        # `bootstrap/` and applied once with
        # `ekn kubeapply --target bootstrap`. Its objects stay out of this
        # instance's `kubernetes.generated`, so a plain `ekn kubeapply` and
        # `ekn validate` never touch them.
        bootstrap = {
          path = "bootstrap";
          modules = [ ./argocd.nix ];
        };
      };
    };

    # An ordinary application, routed to the `apps` target the root Application
    # in argocd.nix points at. This is the half that never needs a human.
    kubernetes.objects.default.ConfigMap.hello = {
      ekn.gitOpsTarget = "apps";
      data.greeting = "synced by argocd";
    };
  };
}
