{
  config,
  ekn,
  lib,
  ...
}:
{
  # `gitOps.*` was the old name for all of this, and every definition of it
  # keeps working and warns with the new path.
  #
  # The rename is not cosmetic. "GitOps" names one delivery mechanism, and
  # this option is not about that mechanism. `ekn kubeapply --target
  # bootstrap` applies a unit by hand, with no git and no ArgoCD anywhere in
  # the path -- and a bootstrap unit exists *because* ArgoCD cannot sync it
  # yet. A unit is a set of objects with a destination; whether that
  # destination is reached by a commit, by `ekn kubeapply` or by kluctl is a
  # property of the run.
  #
  # The old name had already misled in this repository. The assertion for a
  # seeded credential routed to a unit explained itself as "committed to a
  # branch and applied by ArgoCD", and the first consumer to hit it was
  # inside a nested bootstrap instance where nobody commits anything.
  imports = [
    (lib.mkRenamedOptionModule [ "gitOps" "enable" ] [ "deployment" "enable" ])
    (lib.mkRenamedOptionModule [ "gitOps" "deployBranch" ] [ "deployment" "deployBranch" ])
    (lib.mkRenamedOptionModule [ "gitOps" "sourceBranch" ] [ "deployment" "sourceBranch" ])
    (lib.mkRenamedOptionModule [ "gitOps" "path" ] [ "deployment" "path" ])
    (lib.mkRenamedOptionModule [ "gitOps" "targets" ] [ "deployment" "units" ])
  ];

  options.deployment = {
    enable = lib.mkEnableOption "rendering objects into named deployment units";

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

    units = lib.mkOption {
      type = lib.types.attrsOf (
        lib.types.submodule (
          # `config` is deliberately not in the pattern: the options below
          # read the *outer* one (this instance's `ekn.environment`,
          # `deployment`, ...) by closure, and naming it here would shadow that
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
              fieldManager = lib.mkOption {
                type = lib.types.str;
                default = "ekn";
                description = ''
                  Server-side-apply field manager `ekn kubeapply --target
                  ${name}` writes as. Apply-time only -- it never appears in
                  the rendered manifests, because it is a property of who
                  applied an object rather than of the object.

                  Set it to the name of the controller that takes the
                  objects over afterwards, which for a bootstrap target is
                  the point of the whole exercise. Two managers declaring the
                  same field is an SSA conflict, forcing `Force=true` on the
                  successor's side, and worse: SSA only drops a field when
                  its owning manager stops declaring it, so every field `ekn`
                  set and the successor does not stays owned by `ekn`
                  forever, because a bootstrap apply never runs again.
                  Applying as the successor makes the handover complete
                  instead -- fields the bootstrap set and steady state does
                  not are simply removed.

                  The cost is that a *second* apply of this target silently
                  overwrites the successor's fields instead of reporting a
                  conflict, since they are now the same manager. That is the
                  trade, and it is acceptable only because a bootstrap target
                  runs once by definition.
                '';
                example = "argocd-controller";
              };
              labels = lib.mkOption {
                type = lib.types.attrsOf (
                  lib.types.either lib.types.str (lib.types.functionTo (lib.types.nullOr lib.types.str))
                );
                default = { };
                description = ''
                  Labels stamped onto every object in this target -- both the
                  ones routed here with `ekn.deploymentUnit` and the ones
                  `modules` renders. Baked into the rendered manifests, so the
                  committed YAML and what `ekn kubeapply --target ${name}`
                  applies agree.

                  One entry is here by default: `ekn.dev/deployment-unit`,
                  holding this unit's name. It is what makes `kubectl get all
                  -A -l ekn.dev/deployment-unit=${name}` answer, and it is
                  what scopes pruning. `mkForce` a different string if the
                  name does not suit. Do not remove it and do not make it a
                  function: an assertion rejects both, because a unit that
                  cannot be selected by this label cannot be pruned, and its
                  objects look unowned to the next whole-instance prune.

                  A value may be a function of the object rather than a
                  string, for metadata that has to encode the object's own
                  identity -- `ekn.lib.argocdTrackingId` is one. Such a
                  function may return `null` to leave that one object alone,
                  which is not a nicety: ArgoCD deliberately puts no tracking
                  annotation on a CRD, so stamping one anyway would make every
                  CRD permanently OutOfSync.

                  These win over labels the object already carries. That is
                  deliberate and it matters: Helm charts routinely set
                  `app.kubernetes.io/instance` to their release name, which
                  is exactly the key a GitOps engine may be using to decide
                  ownership.

                  An object routed here also appears in
                  `kubernetes.generated`, and carries the same stamp there.
                  Both copies must agree: a whole-instance apply and a
                  `--target` apply write as the same field manager, so a
                  stamp on only one of them flips with whichever apply ran
                  last. See `stampRouted` in kubernetes.nix.
                '';
                example = lib.literalExpression ''{ "app.kubernetes.io/instance" = "root"; }'';
              };
              annotations = lib.mkOption {
                type = lib.types.attrsOf (
                  lib.types.either lib.types.str (lib.types.functionTo (lib.types.nullOr lib.types.str))
                );
                default = { };
                description = ''
                  Annotations stamped onto every object in this target, with
                  the same semantics as `labels` above.

                  The per-object form is the useful one here: ArgoCD's
                  `argocd.argoproj.io/tracking-id` -- its default ownership
                  mechanism since 3.0 -- encodes each object's own
                  group/kind/namespace/name, so a constant cannot express it.
                  See `ekn.lib.argocdTrackingId`.
                '';
                example = lib.literalExpression ''
                  {
                    "argocd.argoproj.io/tracking-id" = ekn.lib.argocdTrackingId {
                      app = "root";
                      namespace = "default";
                    };
                  }
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
                  `ekn.environment` defaults to this instance's.

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
                  `kubernetes.deploymentUnits` joins into this target's
                  objects. Internal because it holds functions and a whole
                  option tree: it must never reach `kubernetes.deploymentUnits`
                  itself, which is serialized to JSON for `ekn`.
                '';
              };
            };

            # Which unit an object belongs to, recorded on the object itself.
            #
            # `ekn.deploymentUnit` is EKN-only routing and is stripped before
            # render, so before this the cluster held no record of it. Nor did
            # it hold `ekn.dev/environment` for most objects: `ekn` stamps
            # that at apply time (see `_with_environment_label` in
            # ekn/src/ekn/apply.py), and on a GitOps cluster nearly every
            # object reaches the API server through ArgoCD instead, carrying
            # whatever the committed YAML carries. Measured on a live cluster:
            # an `Application` synced that way had no labels at all.
            #
            # So the mark has to be rendered here rather than stamped by the
            # CLI. It goes through this unit's own `labels`, which
            # `stampTargetMetadata` (kubernetes.nix) already applies to routed
            # objects and to a nested instance's objects alike.
            #
            # A label and not an annotation, because the question this answers
            # is a query: `kubectl get all -A -l ekn.dev/deployment-unit=apps`.
            # An annotation stores the same bytes and cannot be selected on,
            # so answering it would mean listing every object and filtering
            # client-side. The API server keeps no index of arbitrary labels
            # either way -- its watch cache indexes namespace, and matches
            # label selectors by iterating -- so the label costs its bytes and
            # nothing more. `ekn.dev/environment` is the same shape.
            #
            # `mkDefault`, so a unit can `mkForce` another string. It cannot
            # decline it: an assertion below requires a plain string here,
            # because this label is the prune scope in both directions.
            config.labels."ekn.dev/deployment-unit" = lib.mkDefault name;

            config.instance = ekn.lib.mkInstance {
              modules = [
                # The parent's environment, not a derived one. Both applies
                # stamp `ekn.dev/environment`, and the unit's own
                # `ekn.dev/deployment-unit` label is what separates the two
                # prune scopes -- so the environment has to match, or a
                # `--target ${name} --prune` would find none of its own
                # objects. `mkDefault`, so a nested module can still set its
                # own. `ekn.environment` is required and has no default, so
                # this is also what saves every nested instance from
                # declaring it.
                { ekn.environment = lib.mkDefault config.ekn.environment; }
              ]
              ++ target.config.modules;
              specialArgs = {
                # The parent's *evaluated* config. A bootstrap configuration
                # genuinely needs it -- an ArgoCD root Application has to
                # name the branch and path it syncs, and those live up here.
                #
                # Reading a rendered output through this closes a loop and
                # evaluation hits infinite recursion rather than a readable
                # error: `parent.kubernetes.deploymentUnits` is built *from*
                # this instance, and `generated`/`generatedWithEkn` run the
                # assertion checker that `deploymentUnits` also runs. Read
                # inputs -- `parent.deployment.deployBranch`,
                # `parent.ekn.environment`, a module's own options -- not
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
          `ekn.deploymentUnit` -- rather than embedding a reference to the
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

  config.assertions =
    let
      # Every unit records its name in `ekn.dev/deployment-unit` (see the
      # unit submodule), so a unit name is now a label value and has to obey
      # the API server's rules for one: at most 63 characters, starting and
      # ending alphanumeric, with dashes, underscores and dots between.
      #
      # Without this check an offending name renders fine and fails at apply
      # time, per object, with a validation error that names the label rather
      # than the unit that produced it.
      #
      # Reads the resolved label rather than the attribute name, so a unit
      # that overrides the value is checked on what it actually sets.
      labelOf = unit: unit.labels."ekn.dev/deployment-unit" or null;

      # A unit that puts no plain string here cannot be selected, and a unit
      # that cannot be selected cannot be pruned -- in either direction. See
      # the assertion message below.
      unlabelled = lib.attrNames (
        lib.filterAttrs (_name: unit: !(lib.isString (labelOf unit))) config.deployment.units
      );

      offenders = lib.filter (value: value != null) (
        lib.mapAttrsToList (
          name: unit:
          let
            value = labelOf unit;
          in
          if !(lib.isString value) then
            null
          else if builtins.stringLength value > 63 then
            "${name}: ${toString (builtins.stringLength value)} characters, over the 63 a label value allows"
          else if builtins.match "[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?" value == null then
            "${name}: ${value}"
          else
            null
        ) config.deployment.units
      );
    in
    [
      {
        assertion = unlabelled == [ ];
        message = ''
          These deployment units set no `ekn.dev/deployment-unit' label to a
          plain string:

          ${lib.concatMapStringsSep "\n" (entry: "  ${entry}") unlabelled}

          The label is what scopes pruning, so every unit has to carry one.
          `ekn kubeapply --target <name> --prune' selects on its value, and a
          whole-instance `ekn kubeapply --prune' selects on its *absence* --
          that is how a whole-instance prune leaves a bootstrap unit's
          objects alone. Those objects exist nowhere but in the unit, so
          nothing else marks them as somebody's.

          A unit with no label therefore does not merely lose its own
          `--target' prune. Its objects look unowned to the next
          whole-instance `--prune', which deletes them. For a bootstrap unit
          that is ArgoCD and the CNI.

          A function is not enough either: pruning selects on the label
          before it sees an object, so the value has to be the same string
          for every object in the unit.
        '';
      }
      {
        assertion = offenders == [ ];
        message = ''
          These deployment units cannot be recorded in the
          `ekn.dev/deployment-unit` label:

          ${lib.concatMapStringsSep "\n" (entry: "  ${entry}") offenders}

          A label value is at most 63 characters. It starts and ends with a
          letter or a digit, and holds only letters, digits, dashes,
          underscores and dots between them.

          Rename the unit, or set
          `deployment.units.<name>.labels."ekn.dev/deployment-unit"' to a
          legal value.
        '';
      }
    ];
}
