{
  lib,
  ...
}:
{
  options.gitOps = {
    enable = lib.mkEnableOption "GitOps workflow support";

    deployBranch = lib.mkOption {
      type = lib.types.str;
      description = ''
        Git branch that rendered manifests are committed to, and that
        GitOps tooling (ArgoCD/Flux) should sync from. One branch per
        easykubenix instance -- an "environment" is just whichever
        instance you evaluate, same as one NixOS system is one
        environment.
      '';
      example = "production";
    };

    sourceBranch = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = ''
        Git branch the exact source tree (working copy, including
        uncommitted edits) is snapshotted to at deploy time, paired 1:1
        with `deployBranch` via two-parent commits -- every deploy commit
        also points at the source commit that produced it. Null disables
        this snapshot/dual-commit behavior, falling back to a plain
        single-branch commit.
      '';
      example = "production-source";
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
            path = lib.mkOption {
              type = lib.types.str;
              default = "./";
              description = "Subdirectory within deployBranch/sourceBranch where this target's manifests are stored.";
            };
          };
        }
      );
      default = { };
      description = ''
        Named GitOps sync targets, each just a `{path}` -- pure
        path-routing within the single `deployBranch`/`sourceBranch` pair
        for this instance. Kubernetes objects route to one of these by
        name via `ekn.gitOpsTarget`, rather than embedding a reference to
        the Application/Kustomization object that happens to sync them --
        the target is the single source of truth for where its manifests
        land, and how many controllers exist (Argo, Flux, both, neither)
        is up to whatever object references the target, not the target
        itself.
      '';
      example = {
        bootstrap = {
          path = "bootstrap";
        };
      };
    };
  };
}
