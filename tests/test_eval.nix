let
  sources = import ../nix/sources.nix;
  pkgs = import sources.nixpkgs { };
  easy = import ../. {
    inherit pkgs;
    modules = [
      ({ config, ... }: {
        # `ekn.discriminator` has no default, and a GitOps target derives
        # its own prune scope from it -- so a fixture with targets has to
        # say one. These keep the old default's value, which is what the
        # derived names below are asserted against.
        ekn.discriminator = "easykubenix";
        gitOps.deployBranch = "deploy";
        gitOps.targets.apps.path = "clusters/home/apps";
        kubernetes.objects.default.Deployment.api = {
          ekn.gitOpsTarget = "apps";
          apiVersion = "apps/v1";
        };
      })
    ];
  };
  easyCoercion = import ../. {
    inherit pkgs;
    modules = [
      {
        kubernetes.objects.default.ConfigMap.coerced = {
          metadata.labels.enabled = true;
          metadata.labels.replicas = 3;
          metadata.annotations.disabled = false;
          data.key = "value";
        };
      }
    ];
  };
  easyCoercionDisabled = import ../. {
    inherit pkgs;
    modules = [
      {
        kubernetes.coerceLabelsAndAnnotations = false;
        kubernetes.objects.default.ConfigMap.uncoerced = {
          metadata.labels.enabled = "true";
          data.key = "value";
        };
      }
    ];
  };
  # Top-level `metadata.labels`/`metadata.annotations` are the typed
  # `labelValueType` option -- with coercion disabled that's plain
  # `types.str`, so a bool there is rejected by the module system itself.
  easyCoercionDisabledThrows = import ../. {
    inherit pkgs;
    modules = [
      {
        kubernetes.coerceLabelsAndAnnotations = false;
        kubernetes.objects.default.ConfigMap.uncoerced = {
          metadata.labels.enabled = true;
          data.key = "value";
        };
      }
    ];
  };
  # A transformer runs after the option merge and returns a plain value that no
  # option type sees. Thus a transformer can put a marker into an object, and
  # the pipeline must change that marker back into a list. The order of the init
  # containers is important, so the numbered marker must keep it. This is the
  # case that `needsMarkerPass` in kubernetes.nix guards.
  easyInitContainers = import ../. {
    inherit pkgs;
    modules = [
      (
        { lib, ... }:
        {
          kubernetes.transformers = [
            (
              object:
              object
              // {
                spec = object.spec // {
                  initContainers = lib.mkNumberedList {
                    "0" = {
                      name = "first";
                      image = "a";
                    };
                    "1" = {
                      name = "second";
                      image = "b";
                    };
                    "2" = {
                      name = "third";
                      image = "c";
                    };
                  };
                };
              }
            )
          ];
          kubernetes.objects.default.Pod.test = {
            spec.initContainers = [ ];
          };
        }
      )
    ];
  };
  # `mkNamedList` inside `kubernetes.objects` with no other definition at the
  # same path. `ekn.lib.kubeValueType` must resolve it on its own, so the
  # rendered manifest holds a real list and no `_type` marker. Nothing converts
  # markers after the merge unless a generator or a transformer runs.
  easyLoneNamedList = import ../. {
    inherit pkgs;
    modules = [
      (
        { lib, ... }:
        {
          kubernetes.objects.default.Pod.solo.spec.containers = lib.mkNamedList {
            main.image = "nginx";
          };
        }
      )
    ];
  };

  # Two modules define the same object. One gives a plain rendered list, the
  # same shape a Helm chart produces. The other patches one entry by name and
  # adds a second entry. This is the merge that removes the need for a
  # pre-conversion pass in helm.nix and importyaml.nix.
  easyNamedListAcrossModules = import ../. {
    inherit pkgs;
    modules = [
      {
        kubernetes.objects.default.Pod.web.spec.containers = [
          {
            name = "app";
            image = "v1";
          }
          {
            name = "log";
            image = "fluentd";
          }
        ];
      }
      (
        { lib, ... }:
        {
          kubernetes.objects.default.Pod.web.spec.containers = lib.mkNamedList {
            app.image = lib.mkForce "v2";
            metrics.image = "exporter";
          };
        }
      )
    ];
  };

  # A module declares its own option with the recursive kube type, then hoists
  # the result into `kubernetes.objects`. This is the shape helm.nix and
  # importyaml.nix use. A marker must survive the hoist and still resolve.
  easyHoistedFromSubmodule = import ../. {
    inherit pkgs;
    modules = [
      (
        {
          config,
          lib,
          ekn,
          ...
        }:
        {
          options.myApps = lib.mkOption {
            type = lib.types.attrsOf (lib.types.submodule { freeformType = ekn.lib.kubeValueType; });
            default = { };
          };
          config.myApps.api = {
            apiVersion = "v1";
            kind = "Pod";
            metadata.name = "api";
            spec.containers = lib.mkNamedList { main.image = "api:1"; };
          };
          # The same one-key-attrset plus mkMerge lift that helm.nix uses.
          config.kubernetes.objects = lib.mkMerge (
            lib.mapAttrsToList (_: object: {
              ${object.metadata.namespace or "none"}.${object.kind}.${object.metadata.name} = object;
            }) config.myApps
          );
        }
      )
    ];
  };

  # `kubernetes.crds` goes around the type on purpose, so nothing there can
  # resolve a marker. Forcing this must throw rather than write a manifest with
  # a literal `_type` field.
  easyCrdMarkerThrows =
    (import ../. {
      inherit pkgs;
      modules = [
        (
          { lib, ... }:
          {
            kubernetes.crds = [
              {
                apiVersion = "apiextensions.k8s.io/v1";
                kind = "CustomResourceDefinition";
                metadata.name = "widgets.example.com";
                spec.versions = lib.mkNamedList { v1.served = true; };
              }
            ];
          }
        )
      ];
    }).config.kubernetes.generated;

  # A GitOps target carrying its own module list: a whole separate
  # easykubenix instance, rendered into that target's path and kept out of
  # this instance's `generated`. The `bootstrap` target has no routed objects
  # at all, only submodule ones, which is the normal shape for one -- and the
  # case the target-name union in kubernetes.nix would otherwise miss.
  easyGitOpsSubmodule = import ../. {
    inherit pkgs;
    modules = [
      {
        ekn.discriminator = "easykubenix";
        gitOps.deployBranch = "deploy";
        gitOps.targets.apps.path = "clusters/home/apps";
        kubernetes.objects.default.ConfigMap.routed = {
          ekn.gitOpsTarget = "apps";
          data.key = "from-parent";
        };
        gitOps.targets.bootstrap = {
          path = "bootstrap";
          modules = [
            (
              { parent, ... }:
              {
                # Reading the parent's *inputs* is the supported direction --
                # a real bootstrap config needs the branch to point a root
                # Application at. Reading its rendered outputs would recurse.
                kubernetes.objects.argocd.ConfigMap.root.data = {
                  branch = parent.gitOps.deployBranch;
                };
              }
            )
          ];
        };
      }
    ];
  };

  # A target that names its successor as field manager and stamps tracking
  # metadata onto everything it renders. Both halves of the handover a
  # bootstrap target needs: who applied the fields, and who owns the objects.
  easyGitOpsTargetMetadata = import ../. {
    inherit pkgs;
    modules = [
      (
        { ekn, ... }:
        {
          ekn.discriminator = "easykubenix";
          gitOps.deployBranch = "deploy";
          gitOps.targets.apps.path = "clusters/home/apps";
          # Routed here from the parent, and stamped exactly like a submodule
          # object -- a target's metadata covers both sources.
          kubernetes.objects.default.ConfigMap.routed = {
            ekn.gitOpsTarget = "bootstrap";
            data.key = "from-parent";
          };
          # The control: a target declaring no metadata stamps none, so the
          # assertions above are about the declaration rather than about
          # being in a target at all.
          kubernetes.objects.default.ConfigMap.unstamped = {
            ekn.gitOpsTarget = "apps";
            data.key = "ordinary";
          };
          gitOps.targets.bootstrap = {
            path = "bootstrap";
            fieldManager = "argocd-controller";
            labels = {
              # Static, and colliding with a label the object already carries:
              # the target's value has to win, since a Helm chart setting
              # `app.kubernetes.io/instance` to its release name is exactly
              # what would otherwise defeat the ownership stamp.
              "app.kubernetes.io/instance" = "argocd";
              # A function of the object, the shape argocdTrackingId has.
              "ekn.dev/kind" = object: object.kind;
            };
            annotations."argocd.argoproj.io/tracking-id" = ekn.lib.argocdTrackingId {
              app = "argocd";
              namespace = "argocd";
            };
            modules = [
              {
                # Namespaced, cluster-scoped (`none`), and a CRD -- the three
                # cases argocdTrackingId distinguishes. The CRD gets no
                # annotation at all, because ArgoCD never stamps one and a
                # stamp it does not render is a permanent diff.
                kubernetes.objects.argocd.ConfigMap.root = {
                  metadata.labels."app.kubernetes.io/instance" = "from-chart";
                  data.key = "from-bootstrap";
                };
                kubernetes.objects.none.Namespace.argocd = { };
                kubernetes.objects.none.CustomResourceDefinition."widgets.example.com" = {
                  spec.group = "example.com";
                };
              }
            ];
          };
        }
      )
    ];
  };
  # A deprecated `kluctl.*` option whose value names a rendered output.
  # Pushing the manifest to a cache before deploying it is the obvious thing
  # to write there, and nixkube does exactly this.
  #
  # It used to be impossible. The kluctl deprecation notice asked the module
  # system which `kluctl.*` options a configuration had written, and reading a
  # definition's priority forces its value -- so `manifestJSONFile` went
  # through `kubernetes.generated`, `checked`, `warnings`, this priority and
  # back to itself. `error: infinite recursion`, pointing at internal.nix and
  # naming neither kluctl nor the option at fault.
  easyKluctlScriptReadsManifest = import ../. {
    inherit pkgs;
    modules = [
      (
        { config, ... }:
        {
          kubernetes.objects.default.ConfigMap.test.data.key = "hello";
          kluctl.preDeployScript = "echo ${config.internal.manifestJSONFile}";
        }
      )
    ];
  };
  # A configuration that names no discriminator at all.
  #
  # `ekn.discriminator` has no default on purpose, so this is what a project
  # that never thought about the prune scope looks like. It has to render --
  # a manifest is not a deploy -- and it has to fail the moment somebody
  # builds something that applies.
  # `lib.mkIfExists` and `lib.mkIfExistsAtPath` against the real
  # `kubernetes.objects` tree. One Deployment exists. The second module names
  # a namespace, a Kind and an object that do not, and must create none of
  # them, while still patching the one that does.
  easyConditionalObjects = import ../. {
    inherit pkgs;
    modules = [
      { kubernetes.objects.default.Deployment.api.spec.replicas = 1; }
      (
        { lib, ... }:
        {
          kubernetes.objects = lib.mkMerge [
            {
              # No `missing` namespace exists, so this creates nothing.
              missing = lib.mkIfExists {
                ConfigMap.never.data.key = "value";
              };
              default = lib.mkIfExists {
                # A conditional namespace that exists still adds children.
                ConfigMap.added.data.key = "value";
                # A conditional Kind that does not exist creates nothing,
                # including the object named under it.
                MissingKind = lib.mkIfExists {
                  absent.spec.replicas = 10;
                };
                Deployment = lib.mkIfExists {
                  # A conditional Kind that exists still adds children.
                  extra.spec.replicas = 2;
                  # A conditional object that does not exist creates nothing.
                  gone = lib.mkIfExists { spec.replicas = 10; };
                  # A conditional object that exists is patched.
                  api = lib.mkIfExists { spec.replicas = lib.mkForce 3; };
                };
              };
            }
            (lib.mkIfExistsAtPath "default.Deployment.api" {
              metadata.annotations.patched = "true";
            })
            (lib.mkIfExistsAtPath [
              "default"
              "Deployment"
              "vanished"
            ] { spec.replicas = 10; })
          ];
        }
      )
    ];
  };

  easyNoDiscriminator = import ../. {
    inherit pkgs;
    modules = [
      { kubernetes.objects.default.ConfigMap.test.data.key = "hello"; }
    ];
  };
in
{
  # Only that it evaluates. The notice itself is `lib.warn` on
  # `kluctl.projectDir`, which nothing here forces.
  kluctlScriptReadsManifest = easyKluctlScriptReadsManifest.config.kubernetes.generated;

  conditionalObjects = easyConditionalObjects.config.kubernetes.generated;

  noDiscriminatorRenders = easyNoDiscriminator.config.kubernetes.generated;
  noDiscriminatorDeployThrows = easyNoDiscriminator.config.kluctl.script;

  eknRouting = {
    inherit (easy.config.kubernetes) generatedByPath gitOpsTargets;
  };
  gitOpsTargetMetadata = easyGitOpsTargetMetadata.config.kubernetes.gitOpsTargets;
  gitOpsSubmodule = {
    inherit (easyGitOpsSubmodule.config.kubernetes) generated gitOpsTargets;
    nestedDiscriminator =
      easyGitOpsSubmodule.config.gitOps.targets.bootstrap.instance.config.ekn.discriminator;
  };
  labelsAnnotationsCoercion = easyCoercion.config.kubernetes.generated;
  labelsAnnotationsCoercionDisabled = easyCoercionDisabled.config.kubernetes.generated;
  labelsAnnotationsCoercionDisabledThrows = easyCoercionDisabledThrows.config.kubernetes.generated;
  initContainersOrder = easyInitContainers.config.kubernetes.generated;
  loneNamedList = easyLoneNamedList.config.kubernetes.generated;
  namedListAcrossModules = easyNamedListAcrossModules.config.kubernetes.generated;
  hoistedFromSubmodule = easyHoistedFromSubmodule.config.kubernetes.generated;
  # Forcing this one must throw -- exposed as a thunk so the test can assert on
  # the error without eagerly evaluating it above.
  crdMarkerThrows = easyCrdMarkerThrows;
}
