let
  sources = import ../nix/sources.nix;
  pkgs = import sources.nixpkgs { };
  easy = import ../. {
    inherit pkgs;
    modules = [
      ({ config, ... }: {
        # `ekn.environment` has no default, so a fixture that deploys has to
        # say one.
        ekn.environment = "easykubenix";
        deployment.deployBranch = "deploy";
        deployment.units.apps.path = "clusters/home/apps";
        kubernetes.objects.default.Deployment.api = {
          ekn.deploymentUnit = "apps";
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

  # The same guard, for the third marker. `mkIfExists` is resolved by
  # `conditionalAttrsOf`, which a CRD also goes around.
  easyCrdIfExistsMarkerThrows =
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
                spec.conversion = lib.mkIfExists { strategy = "None"; };
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
        ekn.environment = "easykubenix";
        deployment.deployBranch = "deploy";
        deployment.units.apps.path = "clusters/home/apps";
        kubernetes.objects.default.ConfigMap.routed = {
          ekn.deploymentUnit = "apps";
          data.key = "from-parent";
        };
        deployment.units.bootstrap = {
          path = "bootstrap";
          modules = [
            (
              { parent, ... }:
              {
                # Reading the parent's *inputs* is the supported direction --
                # a real bootstrap config needs the branch to point a root
                # Application at. Reading its rendered outputs would recurse.
                kubernetes.objects.argocd.ConfigMap.root.data = {
                  branch = parent.deployment.deployBranch;
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
        { ekn, lib, ... }:
        {
          ekn.environment = "easykubenix";
          deployment.deployBranch = "deploy";
          deployment.units.apps.path = "clusters/home/apps";
          # A unit renaming the label rather than taking its own name. That
          # is the whole extent of the freedom here: the value must stay a
          # plain string, since it is what pruning selects on.
          deployment.units.renamed = {
            path = "renamed";
            labels."ekn.dev/deployment-unit" = lib.mkForce "renamed-scope";
          };
          kubernetes.objects.default.ConfigMap.renamed-unit = {
            ekn.deploymentUnit = "renamed";
            data.key = "renamed";
          };
          # Routed here from the parent, and stamped exactly like a submodule
          # object -- a target's metadata covers both sources.
          kubernetes.objects.default.ConfigMap.routed = {
            ekn.deploymentUnit = "bootstrap";
            data.key = "from-parent";
          };
          # The control: a target declaring no metadata stamps none, so the
          # assertions above are about the declaration rather than about
          # being in a target at all.
          kubernetes.objects.default.ConfigMap.unstamped = {
            ekn.deploymentUnit = "apps";
            data.key = "ordinary";
          };
          deployment.units.bootstrap = {
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
  # A unit whose name cannot be a label value. Underscores are legal inside
  # one but not at its ends, which is the sort of name that renders fine and
  # then fails per object at the API server.
  easyBadUnitName = import ../. {
    inherit pkgs;
    modules = [
      {
        ekn.environment = "easykubenix";
        deployment.deployBranch = "deploy";
        deployment.units."_apps".path = "clusters/home/apps";
      }
    ];
  };

  # A unit declining the label that scopes its pruning. Legal once, and now
  # rejected: its objects would look unowned to a whole-instance `--prune`.
  easyDeclinedUnitLabel = import ../. {
    inherit pkgs;
    modules = [
      (
        { lib, ... }:
        {
          ekn.environment = "easykubenix";
          deployment.deployBranch = "deploy";
          deployment.units.declined = {
            path = "declined";
            labels."ekn.dev/deployment-unit" = lib.mkForce (_: null);
          };
        }
      )
    ];
  };

  # A nested instance routing to a unit name only the parent declares. That is
  # the documented shape -- `deployment.units.<name>.modules` says a nested
  # instance's routing is ignored, because unit names belong to one instance
  # and a nested instance's are a different set. Forcing it must not throw.
  easyNestedRoutingIgnored = import ../. {
    inherit pkgs;
    modules = [
      {
        ekn.environment = "easykubenix";
        deployment.deployBranch = "deploy";
        deployment.units.bootstrap = {
          path = "bootstrap";
          labels."app.kubernetes.io/instance" = "argocd";
          modules = [
            {
              kubernetes.objects.default.ConfigMap.root = {
                ekn.deploymentUnit = "bootstrap";
                data.k = "v";
              };
            }
          ];
        };
      }
    ];
  };

  # Unit dependencies, with the two shapes that make a closure a closure:
  # `bootstrap` reaches `certs` only through `secrets`, and `secrets` and
  # `argocd` both reach `certs`, which must still appear once.
  easyUnitDependencies = import ../. {
    inherit pkgs;
    modules = [
      {
        ekn.environment = "easykubenix";
        deployment.deployBranch = "deploy";
        deployment.units = {
          certs.path = "certs";
          secrets = {
            path = "secrets";
            dependencies = [ "certs" ];
          };
          argocd = {
            path = "argocd";
            dependencies = [ "certs" ];
          };
          bootstrap = {
            path = "bootstrap";
            dependencies = [
              "secrets"
              "argocd"
            ];
          };
        };
        kubernetes.objects.default.ConfigMap = {
          ca.ekn.deploymentUnit = "certs";
          creds.ekn.deploymentUnit = "secrets";
          server.ekn.deploymentUnit = "argocd";
          root.ekn.deploymentUnit = "bootstrap";
        };
      }
    ];
  };

  # A cycle. Forcing this must throw and print the chain.
  easyUnitDependencyCycle = import ../. {
    inherit pkgs;
    modules = [
      {
        ekn.environment = "easykubenix";
        deployment.deployBranch = "deploy";
        deployment.units.a = {
          path = "a";
          dependencies = [ "b" ];
        };
        deployment.units.b = {
          path = "b";
          dependencies = [ "a" ];
        };
        kubernetes.objects.default.ConfigMap.one.ekn.deploymentUnit = "a";
      }
    ];
  };

  # A dependency no `deployment.units` entry declares.
  easyUnknownUnitDependency = import ../. {
    inherit pkgs;
    modules = [
      {
        ekn.environment = "easykubenix";
        deployment.deployBranch = "deploy";
        deployment.units.a = {
          path = "a";
          dependencies = [ "nope" ];
        };
        kubernetes.objects.default.ConfigMap.one.ekn.deploymentUnit = "a";
      }
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
  # A configuration that names no environment at all.
  #
  # `ekn.environment` has no default on purpose, so this is what a project
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

  # A seeded Secret, shaped like a real ArgoCD repository credential: four
  # `stringData` keys, one of them a reference, plus the label ArgoCD needs
  # to discover it.
  easySeeded = import ../. {
    inherit pkgs;
    modules = [
      (
        { ekn, ... }:
        {
          kubernetes.objects.argocd.Secret.repo-creds = ekn.envSeeded {
            metadata.labels."argocd.argoproj.io/secret-type" = "repository";
            stringData = {
              type = "git";
              url = "https://example.com/group/repo.git";
              username = "ci-token";
              password = ekn.envSeed "ARGOCD_REPO_PASSWORD";
            };
          };
          kubernetes.objects.argocd.ConfigMap.plain.data.key = "value";
        }
      )
    ];
  };

  # Routing a seeded Secret to a GitOps target must be refused: ArgoCD would
  # apply the reference as a literal value.
  easySeededGitOpsThrows =
    (import ../. {
      inherit pkgs;
      modules = [
        (
          { ekn, ... }:
          {
            ekn.environment = "easykubenix";
            deployment.deployBranch = "deploy";
            deployment.units.apps.path = "clusters/home/apps";
            kubernetes.objects.argocd.Secret.repo-creds = ekn.envSeeded {
              ekn.deploymentUnit = "apps";
              stringData.password = ekn.envSeed "ARGOCD_REPO_PASSWORD";
            };
          }
        )
      ];
    }).config.kubernetes.generated;

  # `ekn.envSeeded` needs something to mark. Wrapping an object with no
  # reference in it is a mistake, not a no-op.
  easyEnvSeededWithoutReferenceThrows =
    (import ../. {
      inherit pkgs;
      modules = [
        (
          { ekn, ... }:
          {
            kubernetes.objects.argocd.Secret.repo-creds = ekn.envSeeded {
              stringData.password = "hunter2";
            };
          }
        )
      ];
    }).config.kubernetes.generated;

  # A special argument reaching a module inside a GitOps target's nested
  # instance. This is the only way to pass something the module system must
  # evaluate to learn a module's shape -- a constructor a module is written
  # as a call to. `_module.args` cannot carry one: working out the shape
  # would require `config`, and evaluation stops with infinite recursion.
  #
  # The module below is written in exactly that shape, so it only evaluates
  # at all if the argument arrived.
  easyForwardedSpecialArgs = import ../. {
    inherit pkgs;
    specialArgs.mkThing = name: { kubernetes.objects.default.ConfigMap.${name}.data.made = "yes"; };
    modules = [
      {
        ekn.environment = "easykubenix";
        deployment.deployBranch = "deploy";
        deployment.units.bootstrap = {
          path = "bootstrap";
          modules = [ ({ mkThing, ... }: mkThing "from-a-special-arg") ];
        };
      }
    ];
  };

  # `parent` is gitops.nix's own, and a consumer must not be able to shadow
  # it by passing one at the top level.
  easyParentStillWins = import ../. {
    inherit pkgs;
    specialArgs.parent = "shadowed";
    modules = [
      {
        ekn.environment = "easykubenix";
        deployment.deployBranch = "deploy";
        deployment.units.bootstrap = {
          path = "bootstrap";
          modules = [
            (
              { parent, ... }:
              {
                kubernetes.objects.default.ConfigMap.probe.data.parentEnvironment = parent.ekn.environment;
              }
            )
          ];
        };
      }
    ];
  };

  # Every old name still works, so a consumer converts at its own pace rather
  # than in one commit. `mkRenamedOptionModule` warns with the new path.
  easyOldGitOpsNames = import ../. {
    inherit pkgs;
    modules = [
      {
        ekn.environment = "easykubenix";
        gitOps.deployBranch = "deploy";
        gitOps.targets.apps.path = "clusters/home/apps";
        kubernetes.objects.default.ConfigMap.routed = {
          ekn.gitOpsTarget = "apps";
          data.key = "value";
        };
      }
    ];
  };

  # `validation.serviceSubnet` reaches the API server two ways, and for a while
  # it only reached one. It decided the certificate SANs through
  # `kubeadmConfig.networking`, while `--service-cluster-ip-range` was a
  # literal -- so the option could not make the harness accept an IPv6 Service,
  # whatever it was set to.
  easyCustomServiceSubnet = import ../. {
    inherit pkgs;
    modules = [
      {
        ekn.environment = "servicesubnet";
        validation.serviceSubnet = "10.99.0.0/16,fd00:99::/112";
        kubernetes.objects.default.ConfigMap.probe.data.key = "value";
      }
    ];
  };

  # The import-time set transformer seam. `importyaml` rather than `helm`,
  # because a YAML file costs nothing to render and the two modules wire the
  # hook identically.
  #
  # The manifest deliberately has one object WITHOUT `metadata.namespace`.
  # That is the shape a chart is entitled to produce -- `kubectl apply
  # --namespace` and `helm install --namespace` both leave the manifest alone
  # and let the API server default it -- and the shape that lands in the
  # `none` bucket, where kubernetes.nix injects no namespace and the object
  # ships with none at all.
  transformerSource =
    pkgs:
    pkgs.writeText "sample.yaml" ''
      apiVersion: v1
      kind: ConfigMap
      metadata:
        name: with-namespace
        namespace: explicit
      data:
        key: value
      ---
      apiVersion: v1
      kind: ServiceAccount
      metadata:
        name: without-namespace
    '';

  easyImportTransformers = import ../. {
    inherit pkgs;
    modules = [
      (
        { pkgs, ... }:
        {
          ekn.environment = "transformers";
          importyaml.sample = {
            src = transformerSource pkgs;
            transformers = [
              # What the removed per-object `overrides` option did, written as
              # `map f`. That equivalence is the whole reason it is gone: one
              # hook instead of two, and no ordering rule to remember.
              (map (
                object:
                object
                // {
                  metadata = object.metadata // {
                    labels.stage = "mapped";
                  };
                }
              ))
              # Sees the whole set at once, which `map f` cannot. Stamps the
              # count so the test can tell it was not called per object.
              (
                objects:
                map (
                  object:
                  object
                  // {
                    metadata = object.metadata // {
                      # Merged, not replaced, so the previous transformer's
                      # label survives and the test can prove the order.
                      labels = (object.metadata.labels or { }) // {
                        count = toString (builtins.length objects);
                      };
                    };
                  }
                ) objects
              )
              # Defaults the namespace for one kind, which is the job the whole
              # seam exists for. A real one would read API scope data; this is
              # a test, so it names the kind.
              (
                objects:
                map (
                  object:
                  if object.kind == "ServiceAccount" && !(object.metadata ? namespace) then
                    object
                    // {
                      metadata = object.metadata // {
                        namespace = "defaulted";
                      };
                    }
                  else
                    object
                ) objects
              )
              # Composition, and order: this one runs last and can see what the
              # one above did.
              (
                objects:
                objects
                ++ [
                  {
                    apiVersion = "v1";
                    kind = "ConfigMap";
                    metadata = {
                      name = "added-by-transformer";
                      namespace =
                        (builtins.head (builtins.filter (o: o.kind == "ServiceAccount") objects)).metadata.namespace;
                    };
                    data.key = "value";
                  }
                ]
              )
            ];
          };
        }
      )
    ];
  };

  # A marker introduced by an import-time transformer.
  #
  # `kubernetes.transformers` runs past the type, so a marker it introduces
  # needs `needsMarkerPass` to convert it back or it reaches the manifest as a
  # literal `_type`. The seam here is on the other side of that boundary: its
  # output still has to go through `kubernetes.objects`, whose freeform type is
  # `kubeValueType`, and `namedListOf` resolves the marker when it merges. So
  # `mkNamedList` is safe here and needs no pass.
  easyImportTransformerMarker = import ../. {
    inherit pkgs;
    modules = [
      (
        { pkgs, lib, ... }:
        {
          ekn.environment = "transformers";
          importyaml.marker = {
            src = pkgs.writeText "pod.yaml" ''
              apiVersion: v1
              kind: Pod
              metadata:
                name: web
                namespace: default
              spec:
                containers:
                  - name: app
                    image: v1
            '';
            transformers = [
              (
                objects:
                map (
                  object:
                  object
                  // {
                    spec = object.spec // {
                      containers = lib.mkNamedList { app.image = lib.mkForce "v2"; };
                    };
                  }
                ) objects
              )
            ];
          };
        }
      )
    ];
  };

  # The control: the same manifest with no transformer. The namespace-less
  # object lands in `none` and renders without a namespace.
  easyImportNoTransformers = import ../. {
    inherit pkgs;
    modules = [
      (
        { pkgs, ... }:
        {
          ekn.environment = "transformers";
          importyaml.sample.src = transformerSource pkgs;
        }
      )
    ];
  };

  easyNoEnvironment = import ../. {
    inherit pkgs;
    modules = [
      { kubernetes.objects.default.ConfigMap.test.data.key = "hello"; }
    ];
  };

  # `kubernetes.clusterInfo.ipFamilies`: the IP stack the API server admits a
  # Service against. The default is a single-stack IPv4 cluster, and
  # `validation.serviceSubnet` follows the declaration.
  easyClusterInfo = import ../. {
    inherit pkgs;
    modules = [ ];
  };

  # The dual-stack cluster, declared once: the Service renders, and the
  # validation harness's service CIDR follows the same declaration.
  easyDualStackCluster = import ../. {
    inherit pkgs;
    modules = [
      (
        { ... }:
        {
          ekn.environment = "easykubenix";
          kubernetes.clusterInfo.ipFamilies = [
            "IPv4"
            "IPv6"
          ];
          kubernetes.clusterInfo.serviceCidr.ipv6 = "fd00:96::/112";
          kubernetes.objects.default.Service.dual.spec.ipFamilyPolicy = "RequireDualStack";
        }
      )
    ];
  };
in
{
  # Only that it evaluates. The notice itself is `lib.warn` on
  # `kluctl.projectDir`, which nothing here forces.
  kluctlScriptReadsManifest = easyKluctlScriptReadsManifest.config.kubernetes.generated;

  conditionalObjects = easyConditionalObjects.config.kubernetes.generated;

  seededGenerated = easySeeded.config.kubernetes.generated;
  seededExportable = easySeeded.config.kubernetes.generatedExportable;

  # The manifest outputs `default.nix` exposes are built for a different
  # applier, so they drop the seeded object. `internal.*` keeps it, because
  # everything reading that goes through the CLI, which substitutes first.
  forwardedSpecialArgs =
    easyForwardedSpecialArgs.config.deployment.units.bootstrap.instance.config.kubernetes.generated;
  parentStillWins =
    easyParentStillWins.config.deployment.units.bootstrap.instance.config.kubernetes.generated;

  oldGitOpsNamesStillWork = easyOldGitOpsNames.config.kubernetes.deploymentUnits;
  # `generatedWithEkn` publishes each object's `ekn` sidecar. A deprecated
  # alias is still a declared option, so without a strip it rides along on
  # every object -- and, while the alias warned on read, printed a deprecation
  # notice for a configuration that never wrote the old name.
  eknSidecarKeys = map (
    object: builtins.attrNames object.ekn
  ) easy.config.kubernetes.generatedWithEkn;

  seededPublicManifest = easySeeded.config.internal.exportable.manifestAttrs;
  seededInternalManifest = easySeeded.config.internal.manifestAttrs;
  # Forcing these must throw -- thunks, so the test can assert on the error.
  seededGitOpsThrows = easySeededGitOpsThrows;
  envSeededWithoutReferenceThrows = easyEnvSeededWithoutReferenceThrows;

  importTransformerMarker = easyImportTransformerMarker.config.kubernetes.generated;
  importTransformers = easyImportTransformers.config.kubernetes.generated;
  importTransformersBuckets = builtins.attrNames easyImportTransformers.config.kubernetes.objects;
  importWithoutTransformers = easyImportNoTransformers.config.kubernetes.generated;
  importWithoutTransformersBuckets = builtins.attrNames easyImportNoTransformers.config.kubernetes.objects;

  serviceSubnetReachesTheApiserverFlag =
    let
      script = builtins.readFile "${easyCustomServiceSubnet.config.validation.script}/bin/kubeval";
      subnet = easyCustomServiceSubnet.config.validation.serviceSubnet;
    in
    {
      # The option's value, not a literal, and not just the IPv4 half.
      inFlag = pkgs.lib.hasInfix "--service-cluster-ip-range=${subnet}" script;
      # The certificate SANs read the same option, so the two agree.
      inKubeadmConfig = easyCustomServiceSubnet.config.validation.kubeadmConfig.networking.serviceSubnet;
      # The literal that used to be here, in case someone writes one again.
      noHardcodedRange = !(pkgs.lib.hasInfix "--service-cluster-ip-range=10.96.0.0/12" script);
    };

  noEnvironmentRenders = easyNoEnvironment.config.kubernetes.generated;
  noEnvironmentDeployThrows = easyNoEnvironment.config.kluctl.script;

  eknRouting = {
    inherit (easy.config.kubernetes) generatedByPath deploymentUnits;
  };
  deploymentUnitMetadata = easyGitOpsTargetMetadata.config.kubernetes.deploymentUnits;
  # The same objects as they appear outside any unit. A routed object carries
  # its unit's stamp here too -- see `stampRouted` in kubernetes.nix.
  deploymentUnitMetadataGenerated = easyGitOpsTargetMetadata.config.kubernetes.generated;
  gitOpsSubmodule = {
    inherit (easyGitOpsSubmodule.config.kubernetes) generated deploymentUnits;
    nestedEnvironment =
      easyGitOpsSubmodule.config.deployment.units.bootstrap.instance.config.ekn.environment;
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
  badUnitNameThrows = easyBadUnitName.config.kubernetes.generated;
  declinedUnitLabelThrows = easyDeclinedUnitLabel.config.kubernetes.generated;

  nestedRoutingIgnored = {
    inherit (easyNestedRoutingIgnored.config.kubernetes) deploymentUnits;
    nestedGenerated =
      easyNestedRoutingIgnored.config.deployment.units.bootstrap.instance.config.kubernetes.generated;
  };

  unitDependencies = pkgs.lib.mapAttrs (
    _name: entry: entry.dependencies
  ) easyUnitDependencies.config.kubernetes.deploymentUnits;
  # Forcing either of these must throw.
  unitDependencyCycleThrows = easyUnitDependencyCycle.config.kubernetes.deploymentUnits;
  unknownUnitDependencyThrows = easyUnknownUnitDependency.config.kubernetes.deploymentUnits;
  crdMarkerThrows = easyCrdMarkerThrows;
  crdIfExistsMarkerThrows = easyCrdIfExistsMarkerThrows;

  clusterInfoIPFamilies = easyClusterInfo.config.kubernetes.clusterInfo.ipFamilies;
  clusterInfoServiceSubnet = easyClusterInfo.config.validation.serviceSubnet;

  # A Service the declared stack cannot serve must be refused at evaluation,
  # naming the Service -- not left for the API server to deny at admission.
  dualStackServiceOnSingleStackThrows =
    (import ../. {
      inherit pkgs;
      modules = [
        (
          { ... }:
          {
            ekn.environment = "easykubenix";
            kubernetes.objects.default.Service.dual.spec.ipFamilyPolicy = "RequireDualStack";
          }
        )
      ];
    }).config.kubernetes.generated;

  iPv6ServiceOnIPv4ClusterThrows =
    (import ../. {
      inherit pkgs;
      modules = [
        (
          { ... }:
          {
            ekn.environment = "easykubenix";
            kubernetes.objects.default.Service.v6.spec.ipFamilies = [
              "IPv6"
            ];
          }
        )
      ];
    }).config.kubernetes.generated;

  # The dual-stack cluster, declared once: the Service renders, and the
  # validation harness's service CIDR follows the same declaration.
  dualStackClusterRenders = easyDualStackCluster.config.kubernetes.generated;
  dualStackClusterServiceSubnet = easyDualStackCluster.config.validation.serviceSubnet;

  # A pinned cluster IP inside the declared range renders; one outside is
  # refused at evaluation, which is where the API server denies it at
  # admission ("provided IP is not in the valid range").
  pinnedIPv4InsideCidrRenders =
    (import ../. {
      inherit pkgs;
      modules = [
        (
          { ... }:
          {
            ekn.environment = "easykubenix";
            kubernetes.objects.default.Service.dns.spec.clusterIP = "10.96.0.10";
          }
        )
      ];
    }).config.kubernetes.generated;

  pinnedIPv4OutsideCidrThrows =
    (import ../. {
      inherit pkgs;
      modules = [
        (
          { ... }:
          {
            ekn.environment = "easykubenix";
            # Inside podSubnet's 10.97.0.0/16, outside serviceCidr's
            # 10.96.0.0/16 -- the near-miss a wrong range produces.
            kubernetes.objects.default.Service.dns.spec.clusterIP = "10.97.0.10";
          }
        )
      ];
    }).config.kubernetes.generated;

  # A headless Service pins nothing and is not checked.
  headlessServiceRenders =
    (import ../. {
      inherit pkgs;
      modules = [
        (
          { ... }:
          {
            ekn.environment = "easykubenix";
            kubernetes.objects.default.Service.headless.spec = {
              clusterIP = "None";
              clusterIPs = [ "None" ];
            };
          }
        )
      ];
    }).config.kubernetes.generated;

  # Declaring a family without its CIDR leaves the harness no range to serve
  # it from; the two options must agree.
  dualFamiliesWithoutIPv6CidrThrows =
    (import ../. {
      inherit pkgs;
      modules = [
        (
          { ... }:
          {
            ekn.environment = "easykubenix";
            kubernetes.clusterInfo.ipFamilies = [
              "IPv4"
              "IPv6"
            ];
          }
        )
      ];
    }).config.kubernetes.generated;

  # A duplicate family says nothing about the stack and would make every
  # consumer of the list guess.
  duplicateIPFamilyThrows =
    (import ../. {
      inherit pkgs;
      modules = [
        (
          { ... }:
          {
            ekn.environment = "easykubenix";
            kubernetes.clusterInfo.ipFamilies = [
              "IPv4"
              "IPv4"
            ];
          }
        )
      ];
    }).config.kubernetes.generated;
}
