# The configuration `tests/test_seeds_integration.py` boots a real
# etcd+kube-apiserver against.
#
# It is deliberately small: one seeded Secret shaped like an ArgoCD
# repository credential (four `stringData` keys, one of them a reference,
# plus the label ArgoCD discovers it by), and one ordinary object beside it
# so the exportable split has something to keep.
let
  sources = import ../nix/sources.nix;
  pkgs = import sources.nixpkgs { };
in
import ../. {
  inherit pkgs;
  modules = [
    (
      { ekn, ... }:
      {
        # Required, and this value is the prune scope for the throwaway
        # cluster the test boots. Nothing else deploys under it.
        ekn.discriminator = "seed-integration";

        kubernetes.objects.default.Secret.repo-creds = ekn.envSeeded {
          metadata.labels."argocd.argoproj.io/secret-type" = "repository";
          stringData = {
            type = "git";
            url = "https://example.com/group/repo.git";
            username = "ci-token";
            password = ekn.envSeed "EKN_TEST_SEED_PASSWORD";
          };
        };

        kubernetes.objects.default.ConfigMap.plain.data.key = "value";
      }
    )
  ];
}
