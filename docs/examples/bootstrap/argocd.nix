# The bootstrap instance: a whole separate easykubenix configuration, attached
# to `gitOps.targets.bootstrap` by cluster.nix. Its objects render into that
# target's path and never reach the parent's `kubernetes.generated`.
#
# This is the chicken-and-egg half of GitOps. Everything here is what makes
# ArgoCD able to sync, so none of it can be synced by ArgoCD: the controller
# itself, its CRDs, and the root Application that points it at the branch the
# parent commits to. It goes down once with `ekn kubeapply --target bootstrap`
# and has a different lifecycle from everything else afterwards.
{
  pkgs,
  parent,
  ...
}:
{
  # `Application` is a custom resource, so it is not in the default
  # `apiMappingFile`. Declared here, in the instance that uses it -- the parent
  # never renders one and has no reason to know the mapping.
  kubernetes.apiMappings.Application = "argoproj.io/v1alpha1";

  # `ekn kubeapply` applies barriers in `ekn.resourcePriority` order, and a
  # Namespace sorts at 20 -- before everything in here that goes into it. The
  # chart does not create it, and neither does anything else: this is a
  # bootstrap, so nothing has run before it.
  kubernetes.objects.none.Namespace.argocd = { };

  helm.releases.argo-cd = {
    chart = pkgs.fetchHelm {
      repo = "https://argoproj.github.io/argo-helm";
      chart = "argo-cd";
      version = "7.7.11";
      sha256 = "sha256-QpF9lpJ61OL7FhM8TDhkKThHGFjDmeOo0hAhKfptDyo=";
    };
    namespace = "argocd";
    # The CRDs are the point of applying this by hand. `Application` below is
    # an instance of one of them, and it applies in a later barrier than the
    # CRDs that establish it -- which `ekn kubeapply` waits for.
    includeCRDs = true;
    noHooks = true;
    values = {
      # A bootstrap wants the controller and the repo-server, not the extras.
      dex.enabled = false;
      notifications.enabled = false;
      applicationSet.enabled = false;
    };
  };

  # The root Application: the handover point. Everything past this is synced by
  # ArgoCD from the branch `ekn commit` writes, so it never needs applying by
  # hand again.
  #
  # `parent` is the outer instance's evaluated config, which is the whole
  # reason a bootstrap config needs it -- the branch and path it must sync are
  # declared up there, and repeating them here by hand is exactly the drift
  # this avoids. `clusterRepoURL` is an option cluster.nix declares itself, so
  # this reaches the user's own options too, not just easykubenix' built-ins.
  #
  # Read the parent's *inputs* like these. Reading its rendered outputs
  # (`kubernetes.generated`, `kubernetes.gitOpsTargets`) closes a loop back
  # through this instance and recurses.
  kubernetes.objects.argocd.Application.root = {
    spec = {
      project = "default";
      source = {
        repoURL = parent.clusterRepoURL;
        targetRevision = parent.gitOps.deployBranch;
        path = parent.gitOps.targets.apps.path;
      };
      destination = {
        server = "https://kubernetes.default.svc";
        namespace = "default";
      };
      syncPolicy.automated = {
        prune = true;
        selfHeal = true;
      };
    };
  };
}
