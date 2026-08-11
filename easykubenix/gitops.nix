{
  config,
  ekn,
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
        lib.types.submodule (
          # `config` is deliberately not in the pattern: the options below
          # read the *outer* one (this instance's `ekn.discriminator`,
          # `gitOps`, ...) by closure, and naming it here would shadow that
          # with the target submodule's own. `@target` reaches the
          # submodule's as `target.config` without the shadowing -- and
          # `name` still has to be named, since the module system passes
          # `_module.args` entries only to a pattern that asks for them.
          { name, ... }@target:
          {
            options = {
              path = lib.mkOption {
                type = lib.types.str;
                default = "./";
                description = "Subdirectory within deployBranch/sourceBranch where this target's manifests are stored.";
              };
              discriminator = lib.mkOption {
                type = lib.types.str;
                default = "${config.ekn.discriminator}-${name}";
                defaultText = lib.literalExpression ''"''${config.ekn.discriminator}-''${name}"'';
                description = ''
                  Prune scope for `ekn kubeapply --target ${name} --prune`.

                  Per-target rather than shared, because pruning selects by
                  this label across every namespace and every kind the apply
                  touched: with one value shared between targets, applying
                  target A with `--prune` would delete target B's objects,
                  which carry the same label but are not in A's desired set.
                  Deriving it from `ekn.discriminator` keeps the scopes
                  disjoint by construction while still letting a project
                  rename all of them at once.
                '';
              };
              modules = lib.mkOption {
                type = lib.types.listOf lib.types.deferredModule;
                default = [ ];
                description = ''
                  Modules making up a whole separate easykubenix
                  configuration, rendered into this target's `path` and
                  nowhere else. Its objects never reach this instance's
                  `kubernetes.generated`, so a plain `ekn kubeapply` (or
                  `ekn validate`) does not see them -- only
                  `ekn kubeapply --target ${name}` does.

                  That separation is the point: this is for the objects a
                  GitOps engine cannot sync because they are what makes it
                  able to sync anything -- the engine itself, its
                  credentials, its root Application. They get applied once,
                  by hand, and then have a different lifecycle from
                  everything else.

                  The nested instance is a complete easykubenix
                  configuration, not a cut-down one, so it can render a Helm
                  chart like any other. It receives its parent's evaluated
                  config as the `parent` module argument, and its
                  `ekn.discriminator` defaults to this target's
                  `discriminator`.

                  Definitions concatenate, so several modules can each add to
                  one target's list.
                '';
                example = lib.literalExpression "[ ./bootstrap/argocd.nix ]";
              };
              instance = lib.mkOption {
                type = lib.types.raw;
                internal = true;
                readOnly = true;
                description = ''
                  The evaluated nested instance -- `lib.evalModules`' result,
                  so `instance.config.kubernetes.generated` is what
                  `kubernetes.gitOpsTargets` joins into this target's
                  objects. Internal because it holds functions and a whole
                  option tree: it must never reach `kubernetes.gitOpsTargets`
                  itself, which is serialized to JSON for `ekn`.
                '';
              };
            };

            config.instance = ekn.lib.mkInstance {
              modules = [
                # At `mkDefault`, so a nested module can still set its own.
                # Without this the nested instance would default to the bare
                # `ekn.discriminator`, disagreeing with the prune scope
                # `ekn kubeapply --target ${name}` actually applies
                # under -- which reads this target's `discriminator`.
                { ekn.discriminator = lib.mkDefault target.config.discriminator; }
              ]
              ++ target.config.modules;
              specialArgs = {
                # The parent's *evaluated* config. A bootstrap configuration
                # genuinely needs it -- an ArgoCD root Application has to
                # name the branch and path it syncs, and those live up here.
                #
                # Reading a rendered output through this closes a loop and
                # evaluation hits infinite recursion rather than a readable
                # error: `parent.kubernetes.gitOpsTargets` is built *from*
                # this instance, and `generated`/`generatedWithEkn` run the
                # assertion checker that `gitOpsTargets` also runs. Read
                # inputs -- `parent.gitOps.deployBranch`,
                # `parent.ekn.discriminator`, a module's own options -- not
                # results.
                parent = config;
              };
            };
          }
        )
      );
      default = { };
      description = ''
        Named GitOps sync targets -- pure path-routing within the single
        `deployBranch`/`sourceBranch` pair for this instance. The target is
        the single source of truth for where its manifests land, and how
        many controllers exist (Argo, Flux, both, neither) is up to whatever
        object references the target, not the target itself.

        A target's objects come from either of two places, and it can use
        both at once:

        - This instance's own objects, routed by name with
          `ekn.gitOpsTarget` -- rather than embedding a reference to the
          Application/Kustomization that happens to sync them.
        - `modules`, a whole separate easykubenix configuration evaluated
          just for this target. Its objects render into the target's `path`
          and stay out of this instance's `kubernetes.generated` entirely,
          which is what makes it usable for bootstrapping a GitOps engine.
      '';
      example = lib.literalExpression ''
        {
          apps.path = "clusters/home/apps";
          bootstrap = {
            path = "bootstrap";
            modules = [ ./bootstrap/argocd.nix ];
          };
        }
      '';
    };
  };
}
