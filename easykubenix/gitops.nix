{
  lib,
  ...
}:
{
  options.gitops = {
    enable = lib.mkEnableOption "GitOps workflow support";

    branch = lib.mkOption {
      type = lib.types.str;
      description = ''
        Git branch that GitOps tooling should sync from. Used by the ekn CLI
        to generate sync configuration and reference the expected deployment
        branch in commit messages and annotations.
      '';
      example = "production";
    };

    path = lib.mkOption {
      type = lib.types.str;
      default = "./";
      description = ''
        Subdirectory within the branch where rendered manifests are stored.
      '';
      example = "./clusters/my-cluster";
    };

    targets = lib.mkOption {
      type = lib.types.attrsOf (
        lib.types.submodule {
          options = {
            branch = lib.mkOption {
              type = lib.types.str;
              description = "Git branch this GitOps target syncs from.";
            };
            path = lib.mkOption {
              type = lib.types.str;
              default = "./";
              description = "Subdirectory within the branch where this target's manifests are stored.";
            };
          };
        }
      );
      default = { };
      description = ''
        Named GitOps sync targets, each a `{branch, path}` pair. Kubernetes
        objects route to one of these by name via `ekn.gitOpsTarget`, rather
        than embedding a reference to the Application/Kustomization object
        that happens to sync them -- the target is the single source of
        truth for where its manifests land, and how many controllers exist
        (Argo, Flux, both, neither) is up to whatever object references the
        target, not the target itself.
      '';
      example = {
        bootstrap = {
          branch = "deploy";
          path = "bootstrap";
        };
      };
    };
  };
}
