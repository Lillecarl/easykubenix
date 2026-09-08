# The outer instance: everything ArgoCD syncs, plus the bootstrap target that
# gets ArgoCD running in the first place.
{ ekn, lib, ... }:
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

    # Required, and no default. It is the prune scope, and it also names each
    # GitOps target: `gitOps.targets.<name>.discriminator` is
    # "${ekn.discriminator}-${name}".
    ekn.discriminator = "bootstrap-example";

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

          # Apply as the controller that takes over, not as `ekn`. SSA only
          # drops a field when its *owning* manager stops declaring it, and a
          # bootstrap apply never runs again -- so as a distinct manager `ekn`
          # would keep owning every field ArgoCD does not declare, forever.
          # Same manager makes the handover complete instead, and removes the
          # need for `Force=true` on the Application.
          fieldManager = "argocd-controller";

          # ...and make ArgoCD consider the objects its own at all, which is a
          # separate mechanism from field ownership. `argocd` below is the
          # Application in argocd.nix that syncs this very folder once ArgoCD
          # is up: the annotation is what lets it adopt the hand-applied
          # objects rather than see them as strangers it will never prune.
          #
          # A per-object function, not a constant, because the tracking-id
          # encodes each object's own identity -- and a tracking-id naming a
          # different object is worse than none at all, since ArgoCD then
          # silently treats the object as belonging to something else.
          annotations."argocd.argoproj.io/tracking-id" = ekn.lib.argocdTrackingId {
            app = "argocd";
            namespace = "argocd";
          };
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
