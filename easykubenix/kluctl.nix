{
  config,
  options,
  pkgs,
  lib,
  ekn,
  ...
}:
let
  cfg = config.kluctl;

  # The options a user writes by hand. `deployment`/`projectDir`/`script` are
  # left out on purpose: kluctl.nix's own `config` block defines all three, so
  # `isDefined` is true for them no matter what the user did, and warning on
  # them would fire for everybody.
  deprecatedUserOptions = [
    "excludeGitopsTargets"
    "files"
    "package"
    "postDeployScript"
    "preDeployScript"
    "project"
  ];
  # Not `isDefined`: the module system feeds an option's own `default` in as a
  # definition at `mkOptionDefault` priority (1500), so `isDefined` is true for
  # every option that has a default and the warning would fire for everyone.
  # A definition written by a user comes in at 100, or 1000 via `mkDefault`.
  optionDefaultPriority = (lib.mkOptionDefault null).priority;
  definedDeprecated = lib.filter (
    name: options.kluctl.${name}.highestPrio < optionDefaultPriority
  ) deprecatedUserOptions;

  # Objects routed (via kubernetes.gitOpsTargets / ekn.gitOpsTarget) to one of
  # cfg.excludeGitopsTargets already have their own deployment path -- an
  # ArgoCD Application/Flux Kustomization applying them from a git branch,
  # plus a one-time bootstrap apply for whatever's needed to get that
  # controller running in the first place. kluctl must not also manage them,
  # or the two reconcilers fight over the same objects (prune wars, drift
  # resets). Everything NOT in excludeGitopsTargets -- including objects
  # routed to GitOps targets that aren't live yet -- keeps deploying via
  # kluctl as before, so a GitOps migration can move one target at a time.
  excludedGitopsKeys =
    lib.genAttrs
      (lib.concatMap (
        name:
        map (
          object: "${object.metadata.namespace or "none"}/${object.kind}/${object.metadata.name}"
        ) config.kubernetes.gitOpsTargets.${name}.objects
      ) (lib.filter (name: config.kubernetes.gitOpsTargets ? ${name}) cfg.excludeGitopsTargets))
      (_: true);
  isExcludedFromKluctl =
    object:
    (
      excludedGitopsKeys ? "${object.metadata.namespace or "none"}/${object.kind}/${object.metadata.name}"
    )
    # kluctl has no SOPS awareness at all -- it would apply the raw,
    # still-encrypted `sops:` blob as the object's literal content. Any
    # object carrying one is meant for a ksops/`ekn kubeapply`-style
    # decrypt-at-apply path instead, regardless of which GitOps target
    # it's routed to, so exclude it here unconditionally rather than
    # requiring it to live in an already-excluded target.
    || (object.sops or null) != null;
  kluctlGenerated = lib.filter (object: !(isExcludedFromKluctl object)) config.kubernetes.generated;
in
{
  # Both options moved out of `kluctl.*` because neither is kluctl's any more:
  # `ekn`'s own `apply_and_prune` reads them (see ekn/src/ekn/apply.py) to
  # order barriers and to scope pruning, and it does so whether or not kluctl
  # is used at all. `mkRenamedOptionModule` keeps existing definitions working
  # and warns with the new path, so a config only has to move once.
  imports = [
    (lib.mkRenamedOptionModule [ "kluctl" "discriminator" ] [ "ekn" "discriminator" ])
    (lib.mkRenamedOptionModule [ "kluctl" "resourcePriority" ] [ "ekn" "resourcePriority" ])
  ];

  options = {
    kluctl = {
      package = lib.mkPackageOption pkgs "kluctl" { };
      excludeGitopsTargets = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [ ];
        description = ''
          Names of `gitOps.targets` whose objects kluctl should not deploy.
          Use this once a target has its own deployment path (e.g. a
          one-time bootstrap apply plus the GitOps controller syncing itself
          from there) to avoid kluctl and that controller fighting over the
          same objects. Objects routed to a target *not* listed here still
          deploy via kluctl as normal, so a GitOps migration can move one
          target at a time instead of all-or-nothing.
        '';
        example = [ "bootstrap" ];
      };
      preDeployScript = lib.mkOption {
        type = lib.types.lines;
        description = ''
          Bash script that runs just before deploying, useful to push manifests to
          a binary cache. JSON manifest file is passed as first argument
        '';
        default = "";
      };
      postDeployScript = lib.mkOption {
        type = lib.types.lines;
        description = ''
          Bash script that runs just after deploying
        '';
        default = "";
      };
      project = lib.mkOption {
        type = ekn.lib.kubeValueType;
        description = "Anything to be rendered into .kluctl.yaml";
        default = {
          targets = [ { name = "local"; } ];
        };
      };
      deployment = lib.mkOption {
        type = ekn.lib.kubeValueType;
        description = "Anything to be rendered into deployment.yaml";
        default = { };
      };
      files = lib.mkOption {
        type = lib.types.attrsOf lib.types.str;
        description = "Attribute set where name is filename and value is string to be put into the file";
        default = { };
      };
      projectDir = lib.mkOption {
        type = lib.types.package;
        internal = true;
      };
      script = lib.mkOption {
        type = lib.types.package;
        internal = true;
      };
    };
  };
  config = {
    # Everything still under `kluctl.*` is deprecated -- see
    # https://github.com/Lillecarl/easykubenix/issues/2. `ekn kubeapply` covers
    # the same ground natively, and `validation.nix` no longer deploys through
    # kluctl either, so nothing in this repository consumes the generated
    # project. It keeps working; it just is not where new work goes.
    #
    # Warn on definition rather than on use: there is no way to detect that
    # something read `kluctl.projectDir`, but a config that sets any of these
    # has made a deliberate choice worth flagging once.
    warnings = lib.optional (definedDeprecated != [ ]) ''
      The kluctl integration is deprecated (easykubenix issue #2); these options
      still work but will be removed: ${
        lib.concatMapStringsSep ", " (name: "kluctl.${name}") definedDeprecated
      }.

      `ekn kubeapply` applies and prunes natively, using `ekn.resourcePriority`
      for ordering and `ekn.discriminator` (or a GitOps target's own) for prune
      scope.
    '';

    kluctl.deployment = {
      deployments =
        # Create barrier deployments for prioritized resource kinds
        (lib.pipe config.ekn.resourcePriority [
          lib.attrValues
          (lib.sort (a: b: a < b))
          lib.unique
          (lib.map (v: {
            path = "prio-${toString v}";
            barrier = true;
            skipDeleteIfTags = true;
          }))
        ])
        ++ [
          # Default resource kinds go into "default"
          {
            path = "default";
            skipDeleteIfTags = true;
          }
        ];
    };
    kluctl.projectDir = pkgs.writeMultipleFiles {
      name = "kluctlProject";
      files = {
        ".templateignore" = {
          content = ''
            *.yaml
            *.json
          '';
        };
        ".kluctl.yaml" = {
          content = builtins.toJSON config.kluctl.project;
        };
        "deployment.yaml" = {
          content = builtins.toJSON config.kluctl.deployment;
        };
        # Don't apply prioritized resources again.
        "default/easykubenix.yaml" = {
          content = builtins.toJSON {
            apiVersion = "v1";
            kind = "List";
            items = lib.filter (
              v: !lib.elem v.kind (lib.attrNames config.ekn.resourcePriority)
            ) kluctlGenerated;
          };
        };
      }
      # Prioritized resources
      // (lib.mapAttrs' (n: v: {
        name = "prio-${toString v}/${n}.yaml";
        value = builtins.toJSON {
          apiVersion = "v1";
          kind = "List";
          items = lib.filter (v: v.kind == n) kluctlGenerated;
        };
      }) config.ekn.resourcePriority)
      # Other user-supplied files
      // cfg.files;
    };
    kluctl.script =
      pkgs.writeScriptBin "kubenixDeploy" # bash
        ''
          #! ${pkgs.runtimeShell}
          set -euo pipefail
          set -x
          ${cfg.preDeployScript}
          ${lib.getExe cfg.package} \
            deploy \
              --no-update-check \
              --target local \
              --discriminator ${config.ekn.discriminator} \
              --project-dir ${cfg.projectDir} \
              $@ # --dry-run? --yes? --prune!
          ${cfg.postDeployScript}
        '';
  };
}
