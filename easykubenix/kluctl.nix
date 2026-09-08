{
  config,
  pkgs,
  lib,
  ekn,
  ...
}:
let
  cfg = config.kluctl;

  # The notice, printed when something forces one of the two outputs below.
  #
  # **On use, and not on definition.** This used to read
  # `options.kluctl.<name>.highestPrio` to find out which deprecated options a
  # configuration had written. That closes a loop, because reading a priority
  # forces the definition's *value*: `filterOverrides` asks every definition
  # for `value._type` to see whether it is an `mkOverride`, and a string
  # answers that question only by evaluating itself.
  #
  # So a `kluctl.preDeployScript` that names a rendered output -- pushing the
  # manifest to a cache before deploying it, which is the obvious thing to
  # write there -- went: `internal.manifestJSONFile` -> `kubernetes.generated`
  # -> `checked` -> `warnings` -> this priority -> the script -> back to
  # `manifestJSONFile`. The configuration died with `error: infinite
  # recursion`, pointing at internal.nix, naming neither kluctl nor the option
  # at fault. nixkube hit it and could not be evaluated at all.
  #
  # kubernetes.nix's `checked` documents the rule this broke: nothing that
  # defines a warning may read a rendered output to build it. A priority looks
  # like metadata and is not.
  #
  # `lib.warn` on the two outputs answers the same question later and cannot
  # loop: it prints when a caller forces the value it wraps. A configuration
  # that sets these options and never builds a kluctl project is not using
  # kluctl, and now says nothing.
  deprecation = ''
    The kluctl integration is deprecated (easykubenix issue #2); it still
    works but will be removed.

    `ekn kubeapply` applies and prunes natively, using `ekn.resourcePriority`
    for ordering and `ekn.discriminator` (or a GitOps target's own) for prune
    scope.
  '';

  # Objects routed (via kubernetes.deploymentUnits / ekn.deploymentUnit) to one of
  # cfg.excludeDeploymentUnits already have their own deployment path -- an
  # ArgoCD Application/Flux Kustomization applying them from a git branch,
  # plus a one-time bootstrap apply for whatever's needed to get that
  # controller running in the first place. kluctl must not also manage them,
  # or the two reconcilers fight over the same objects (prune wars, drift
  # resets). Everything NOT in excludeDeploymentUnits -- including objects
  # routed to GitOps targets that aren't live yet -- keeps deploying via
  # kluctl as before, so a GitOps migration can move one target at a time.
  excludedUnitKeys =
    lib.genAttrs
      (lib.concatMap (
        name:
        map (
          object: "${object.metadata.namespace or "none"}/${object.kind}/${object.metadata.name}"
        ) config.kubernetes.deploymentUnits.${name}.objects
      ) (lib.filter (name: config.kubernetes.deploymentUnits ? ${name}) cfg.excludeDeploymentUnits))
      (_: true);
  isExcludedFromKluctl =
    object:
    (excludedUnitKeys ? "${object.metadata.namespace or "none"}/${object.kind}/${object.metadata.name}")
    # kluctl has no SOPS awareness at all -- it would apply the raw,
    # still-encrypted `sops:` blob as the object's literal content. Any
    # object carrying one is meant for a ksops/`ekn kubeapply`-style
    # decrypt-at-apply path instead, regardless of which GitOps target
    # it's routed to, so exclude it here unconditionally rather than
    # requiring it to live in an already-excluded target.
    || (object.sops or null) != null
    # Same argument, for the other thing kluctl cannot resolve. A Secret
    # holding an `ekn.envSeed' reference gets its value substituted by `ekn
    # kubeapply' at apply time; kluctl would apply `$ekn:env:VARNAME' as the
    # literal value and overwrite a live credential with it. `ekn' is the
    # only applier that can resolve one, so exclude these unconditionally
    # rather than per target.
    #
    # This is silent, unlike the GitOps case, and deliberately so: routing to
    # a GitOps target is an explicit per-object opt-in, so a seeded object
    # there is an author mistake worth an assertion, whereas kluctl takes
    # every object by default and excluding is the only sensible answer.
    || lib.isSeededObject object;
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
    # Renamed with the option it names. See gitops.nix.
    (lib.mkRenamedOptionModule
      [ "kluctl" "excludeGitopsTargets" ]
      [ "kluctl" "excludeDeploymentUnits" ]
    )
  ];

  options = {
    kluctl = {
      package = lib.mkPackageOption pkgs "kluctl" { };
      excludeDeploymentUnits = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [ ];
        description = ''
          Names of `deployment.units` whose objects kluctl should not deploy.
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
    # `projectDir` and `script` below carry the notice. See `deprecation`.

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
    kluctl.projectDir = lib.warn deprecation (
      pkgs.writeMultipleFiles {
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
      }
    );
    # No `lib.warn` here. The script names `cfg.projectDir` below, so forcing
    # it forces that, and one notice comes out rather than two.
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
