{
  config,
  pkgs,
  lib,
  ekn,
  ...
}:
let
  cfg = config.kubernetes;
  # Type for a single top-level `metadata.labels`/`metadata.annotations`
  # value: real strings pass straight through; when coercion is enabled,
  # `lib.types.coercedTo` handles bool/int conversion (and gives a proper
  # module-system error, pointing at the exact option path, for anything
  # else) via the module system's own merge machinery instead of a manual
  # `throw`. This only covers the one *explicit* `metadata` submodule each
  # object has -- nested labels/annotations (pod templates, job templates,
  # PVC templates, etc.) live inside the freeform `spec` blob with no
  # submodule to hook a type into, and are deliberately NOT covered: giving
  # those the same treatment would mean walking/typing arbitrary depth of
  # every object on every eval, which is exactly the per-leaf
  # `ekn.lib.kubeValueType` cost `kubernetes.crds` exists to avoid, for a case
  # (nested labels/annotations with a stray bool/int) that hasn't come up in
  # practice.
  labelValueType =
    if cfg.coerceLabelsAndAnnotations then
      lib.types.coercedTo lib.types.bool lib.boolToString (
        lib.types.coercedTo lib.types.int builtins.toString lib.types.str
      )
    else
      lib.types.str;
  # Rendered Helm charts commonly emit `annotations: null`/`labels: null`
  # (a YAML/Helm convention for "none set", rather than omitting the key
  # or emitting `{}`) -- coerce that to an empty attrset rather than
  # rejecting it outright.
  labelsAnnotationsType = lib.types.coercedTo (lib.types.enum [ null ]) (_: { }) (
    lib.types.attrsOf labelValueType
  );
  # `metadata.labels`/`metadata.annotations` (the typed options above)
  # default to `{ }` so an object that never sets either doesn't throw when
  # serialized -- strip them back out here when still empty, so an object
  # that never set labels/annotations doesn't gain a gratuitous, always
  # present `"labels":{}`/`"annotations":{}` in the rendered manifest.
  # Kubernetes treats "absent" and "empty map" identically; only the
  # rendered-YAML noise differs.
  stripEmptyTopLevelLabelsAnnotations =
    object:
    if object ? metadata && lib.isAttrs object.metadata then
      object
      // {
        metadata = lib.filterAttrs (
          k: v:
          !(
            lib.elem k [
              "labels"
              "annotations"
            ]
            && v == { }
          )
        ) object.metadata;
      }
    else
      object;
  # `ekn.lib.kubeValueType` resolves an `mkNamedList` or `mkNumberedList` marker
  # when it merges an option. Thus no marker from `kubernetes.objects` reaches
  # the pipeline below, and the pipeline does not need a pass to convert one.
  #
  # A generator and a transformer both run after the merge. Each one is a plain
  # function that returns a plain value, and no option type sees that value. Thus
  # a generator or a transformer can put a new marker into an object. Run the
  # conversion pass only when one of the two is set. The pass is a deep walk over
  # every object, so it is not free.
  needsMarkerPass = cfg.generators != [ ] || cfg.transformers != [ ];
  generatedWithEkn = lib.pipe cfg.objects (
    [
      # Convert kubernetes.objects.namespace.kind.name into a list of objects
      (lib.collect (x: x ? apiVersion && x ? kind && x ? metadata))
      # Run a generator pass to generate objects from objects.
      (
        objects:
        objects
        ++ lib.pipe objects [
          (lib.concatMap (
            object:
            lib.pipe (map (generator: generator object) cfg.generators) [
              # A generator declining to fire returns `{ }` -- filter it out
              # here, before merging in a default `ekn`, otherwise the merge
              # below turns it into a non-empty `{ ekn = ...; }` stub with no
              # kind/apiVersion/metadata that survives downstream.
              (lib.filter (generated: generated != { }))
              (map (
                generated:
                generated
                // lib.optionalAttrs (!(generated ? ekn)) {
                  ekn = object.ekn or { };
                }
              ))
            ]
          ))
        ]
      )
      # Run a transformation pass over all objects
      (map (object: lib.pipe object cfg.transformers))
      # Run filter pass over all objects
      (lib.filter (object: lib.all (function: function object) cfg.filters))
    ]
    # Change any marked attribute set that a generator or a transformer made
    # back into a list. See `needsMarkerPass` above.
    ++ lib.optional needsMarkerPass (map (lib.walkWithPath lib.kubeAttrsToLists))
    ++ [
      (map stripEmptyTopLevelLabelsAnnotations)
    ]
  );
  # cfg.crds objects deliberately don't flow through generatedWithEkn's
  # pipeline above (generators/transformers/filters) -- that walk is exactly
  # what kubernetes.crds exists to avoid paying for. They're concatenated in
  # afterwards, already complete and untouched.
  #
  # `kubernetes.crds` is `listOf attrs`, so it also goes around
  # `ekn.lib.kubeValueType` and `lib.conditionalAttrsOf`. Nothing resolves an
  # `mkNamedList`, `mkNumberedList` or `mkIfExists` marker in a CRD. Such a
  # marker would reach the cluster as a literal `_type` field. Find it here and
  # stop, instead of writing a manifest that is not valid Kubernetes. The
  # search is lazy and stops at the first marker.
  #
  # Seeded credentials are deliberately not checked here. A CRD never holds
  # one -- an OpenAPI schema has nowhere to put a credential -- and searching
  # for one would mean a recursive walk of every CRD on every evaluation,
  # which is the cost `kubernetes.crds` exists to avoid. `ekn.envSeeded`
  # marks a seeded object with an annotation, and every consumer reads that
  # instead of walking.
  checkedCrds = map (
    crd:
    (lib.throwIf (lib.hasMarker crd) ''
      The CustomResourceDefinition "${
        crd.metadata.name or "<unnamed>"
      }" in `kubernetes.crds' uses lib.mkNamedList, lib.mkNumberedList or
      lib.mkIfExists.

      `kubernetes.crds' goes around ekn.lib.kubeValueType on purpose, for speed.
      Thus nothing resolves the marker, and it becomes a literal `_type' field
      in the manifest.

      Put the object in `kubernetes.objects' instead, where the type resolves
      the marker. You can also write the value directly.
    '' crd)
  ) cfg.crds;
  allGenerated = generatedWithEkn ++ checkedCrds;

  # `assertions`/`warnings` (assertions.nix) are collected from every module,
  # but a plain `lib.evalModules` has nothing playing the part NixOS'
  # top-level.nix plays -- so unless something forces them they are gathered
  # and thrown away. That made every `assertions` entry a silent no-op and,
  # more pressingly, made `mkRenamedOptionModule`'s deprecation notice
  # invisible, which is the whole point of keeping a renamed option around.
  #
  # So every fully-rendered output goes through here: `generated`,
  # `generatedWithEkn` (the same objects with their `ekn` routing still
  # attached) and `deploymentUnits`. `generatedByPath` and everything in
  # internal.nix are built from `generated`, so they inherit it.
  #
  # `resources` deliberately does not. It is the pre-render option tree rather
  # than an output, and it is the natural thing for an assertion message to
  # read -- see below.
  #
  # Anything defining an assertion or warning must not read any of the three
  # checked outputs to build its message -- that closes a loop through this
  # function and evaluation hits infinite recursion rather than a readable
  # error. Assert on the inputs an object was built from (`resources`, or the
  # module's own options), not on the rendered result.
  checked = lib.asserts.checkAssertWarn config.assertions config.warnings;

  # Seeded Secrets routed to a GitOps target, by identity.
  #
  # Read from `cfg.objects` rather than from `generated`: an assertion that
  # reads a checked output closes a loop through `checked` above, and
  # evaluation hits infinite recursion instead of printing the message.
  #
  # `isSeededObject` reads the annotations `ekn.envSeeded` wrote, so this
  # never descends into an object.
  seededGitOpsObjects = lib.concatLists (
    lib.mapAttrsToList (
      namespace: kinds:
      lib.concatLists (
        lib.mapAttrsToList (
          kind: objects:
          lib.concatLists (
            lib.mapAttrsToList (
              name: object:
              lib.optional (
                (object.ekn.deploymentUnit or null) != null && lib.isSeededObject object
              ) "${namespace}/${kind}/${name} -> ${object.ekn.deploymentUnit}"
            ) objects
          )
        ) kinds
      )
    ) cfg.objects
  );
in
{
  imports = [
    (lib.mkAliasOptionModule [ "kubernetes" "resources" ] [ "kubernetes" "objects" ])
    # `kubernetes.gitOpsTargets` is renamed further down rather than here.
    # `mkRenamedOptionModule` writes a *definition* onto the new option, and
    # this one is a read-only computed output, so aliasing it that way fails
    # with "is read-only, but it's set multiple times". A deprecated output
    # carrying a `lib.warn` is the shape that works for something nobody
    # sets and everybody reads.
    (lib.mkRemovedOptionModule [
      "kubernetes"
      "namespacedMappings"
    ] "RIP namespacedMappings")
  ];
  options.kubernetes = {
    package = lib.mkPackageOption pkgs "kubernetes" { };

    templates = lib.mkOption {
      type = lib.types.attrsOf (lib.types.functionTo ekn.lib.kubeValueType);
      default = { };
      description = "Typed resource template functions, usually created with the Adios-backed `template` helper.";
    };

    objects = lib.mkOption {
      # `lib.conditionalAttrsOf` at all three levels, so `lib.mkIfExists` and
      # `lib.mkIfExistsAtPath` reach a namespace, a Kind and an object.
      # Otherwise a module cannot patch a resource another module owns without
      # creating it when that module stops shipping it. See
      # lib/conditionalAttrsOf.nix. A definition with no marker takes a fast
      # path and behaves exactly as `attrsOf` did.
      type = lib.conditionalAttrsOf (
        lib.types.submodule (
          { name, ... }:
          let
            namespace = name;
          in
          {
            freeformType = lib.conditionalAttrsOf (
              lib.types.submodule (
                { name, ... }:
                let
                  kind = name;
                in
                {
                  freeformType = lib.conditionalAttrsOf (
                    lib.types.submodule (
                      { name, ... }:
                      {
                        freeformType = ekn.lib.kubeValueType;
                        options = {
                          ekn = lib.mkOption {
                            type = lib.types.submodule {
                              # `gitOpsTarget` was the old name. The alias
                              # lives inside the submodule because a
                              # top-level `mkRenamedOptionModule` cannot
                              # reach an option declared in one.
                              imports = [
                                (lib.mkRenamedOptionModule [ "gitOpsTarget" ] [ "deploymentUnit" ])
                              ];
                              options = {
                                deploymentUnit = lib.mkOption {
                                  type = lib.types.nullOr lib.types.str;
                                  default = null;
                                  description = ''
                                    Name of the `deployment.units.<name>` this object routes
                                    to. Deliberately single-valued, not a list: an object
                                    synced by two GitOps targets at once means two
                                    controllers independently reconciling (and potentially
                                    pruning) the same resource.

                                    The field itself is EKN-only and is stripped before
                                    Kubernetes manifests are rendered. Its *value* is not:
                                    the object's copy in `kubernetes.deploymentUnits` carries
                                    it in the `ekn.dev/deployment-unit` label, so the unit an
                                    object belongs to survives to the cluster whatever
                                    applies it. The copy in `kubernetes.generated` is
                                    unstamped, as it is for every other per-unit label.
                                  '';
                                };
                                novalidate = lib.mkOption {
                                  type = lib.types.bool;
                                  default = false;
                                  description = ''
                                    Skip this object in `ekn validate`'s ephemeral
                                    etcd+kube-apiserver harness. For objects that can never
                                    be meaningfully verified there regardless of manifest
                                    correctness -- e.g. an aggregated APIService, whose
                                    backing Service/Pod never actually runs in a throwaway,
                                    single-shot apiserver with no controllers or kubelets,
                                    so the apiserver 500s/503s on it (or on discovery calls
                                    that touch it) no matter what. Applied for real via
                                    `ekn kubeapply`/GitOps as normal; this only affects the
                                    validation harness.
                                  '';
                                };
                              };
                            };
                            default = { };
                            description = "EKN-only metadata for this Kubernetes object.";
                          };

                          apiVersion = lib.mkOption {
                            type = lib.types.str;
                            default = cfg.apiMappings.${kind} or (throw "No apiMapping for ${kind}");
                          };
                          kind = lib.mkOption {
                            type = lib.types.str;
                            default = kind;
                          };
                          metadata = lib.mkOption {
                            type = lib.types.submodule {
                              freeformType = ekn.lib.kubeValueType;
                              options = {
                                name = lib.mkOption {
                                  type = lib.types.str;
                                  default = name;
                                };
                                labels = lib.mkOption {
                                  type = labelsAnnotationsType;
                                  default = { };
                                };
                                annotations = lib.mkOption {
                                  type = labelsAnnotationsType;
                                  default = { };
                                };
                              };
                            };
                            default = { };
                          };
                        };
                        config = lib.mkMerge [
                          (lib.mkIf (namespace != "none") {
                            metadata.namespace = lib.mkDefault namespace;
                          })
                        ];
                      }
                    )
                  );
                }
              )
            );
          }
        )
      );

      default = { };
      description = ''
        Kubernetes objects, grouped by namespace, then kind.
        apiVersion is automatically injected (if apiMappings for the object exists)
        kind is automatically injected
        metadata.name is automatically injected
        metadata.namespace is automatically injected if namespace isn't "none"
      '';
      example = {
        kubernetes.objects.none.Namespace.easykubenix = { };
        kubernetes.objects.easykubenix.ConfigMap.myconfig.data.key = "value";
      };
    };

    crds = lib.mkOption {
      # `types.attrs` never recurses into a value's content (just checks
      # `isAttrs` on each list element) -- unlike `kubernetes.objects`'
      # per-object submodule, whose freeformType is ekn.lib.kubeValueType (a
      # real recursive JSON-schema-shaped type: nullOr(oneOf[bool int float
      # str path (attrsOf ...) (listOf ...)])). Profiling `ekn render` on a
      # CRD-heavy environment showed nixpkgs lib.types' `either`/`oneOf`
      # v2-merge-coherence machinery (types.nix's `merge`/`functor`/
      # `checkV2MergeCoherence`) accounting for ~86% of inclusive eval time,
      # entirely from that per-leaf validation walking every attribute of
      # every object's spec/data -- CRDs' OpenAPI schemas are the worst
      # offenders, but it applies to every object, not just CRDs. Changing
      # `kubernetes.objects.<namespace>.CustomResourceDefinition`'s type
      # alone did NOT help, because the value still lived inside the same
      # `kubernetes.objects` submodule tree; only routing objects through an
      # entirely separate option, outside that tree, avoids the expensive
      # type ever being reached. Each entry must already be a complete,
      # valid object (apiVersion/kind/metadata.name set) -- there is no
      # auto-injection here, unlike `kubernetes.objects`.
      type = lib.types.listOf lib.types.attrs;
      default = [ ];
      description = ''
        Pre-rendered, already-complete Kubernetes objects (typically
        CustomResourceDefinitions from Nix-rendered Helm charts) that bypass
        `kubernetes.objects`' per-object submodule and its expensive
        ekn.lib.kubeValueType value-checking entirely. Still passes through
        `ekn.deploymentUnit` GitOps routing and the final YAML render, just
        skips generators/transformers/filters and the auto-defaulting
        `kubernetes.objects` provides.
      '';
    };

    transformers = lib.mkOption {
      type = lib.types.listOf (lib.types.functionTo lib.types.attrs);
      default = [ ];
      description = "List of functions that transform object attrsets";
      example = ''
        kubernetes.transformers = [
          (
            object:
            # Apply annotations to all LoadBalancers
            if object.kind == "Service" && object.spec.type or null == "LoadBalancer" then
              lib.recursiveUpdate object {
                # IPv4 is scarce, share!
                metadata.annotations."metallb.io/allow-shared-ip" = "true";
                # Lowest TTL cloudflare allows
                metadata.annotations."external-dns.alpha.kubernetes.io/ttl" = "60";
              }
            # Make all services require dualstack
            else if object.kind == "Service" then
              lib.recursiveUpdate object {
                spec.ipFamilyPolicy = "RequireDualStack";
              }
            # Set lowest cloudflare TTL for ingress and gapi routes
            else if
              lib.elem object.kind [
                "Ingress"
                "HTTPRoute"
              ]
            then
              lib.recursiveUpdate object {
                metadata.annotations."external-dns.alpha.kubernetes.io/ttl" = "60";
              }
            else
              object
          )
        ];
      '';
    };

    generators = lib.mkOption {
      type = lib.types.listOf (lib.types.functionTo lib.types.attrs);
      default = [ ];
      description = "List of functions that generate object attrsets";
      example = ''
        kubernetes.generators = [
          (
            object:
            lib.optionalAttrs
              (
                (lib.elem (object.kind or "") [
                  "Deployment"
                  "StatefulSet"
                  "DaemonSet"
                ])
                && object.metadata.annotations.genvpa or "true" == "true"
                && !lib.hasAttrByPath [
                  object.metadata.namespace
                  "VerticalPodAutoscaler"
                  object.metadata.name
                ] config.kubernetes.objects
              )
              {
                apiVersion = "autoscaling.k8s.io/v1";
                kind = "VerticalPodAutoscaler";
                metadata = { inherit (object.metadata) name namespace; };
                spec = {
                  targetRef = {
                    inherit (object) apiVersion kind;
                    inherit (object.metadata) name;
                  };
                  updatePolicy.updateMode = "InPlaceOrRecreate";
                };
              }
          )
        ];
      '';
    };

    filters = lib.mkOption {
      type = lib.types.listOf (lib.types.functionTo lib.types.bool);
      default = [ ];
      description = "List of functions that filter objects";
    };

    coerceLabelsAndAnnotations = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Kubernetes `labels`/`annotations` are strictly `map[string]string` --
        every value must be a string, anywhere a `labels` or `annotations`
        attrset appears (not just top-level `metadata`: pod templates, job
        templates, PVC templates etc. each carry their own).

        When enabled (the default), Nix `bool` values are converted via
        `lib.boolToString` and `int` values via `builtins.toString`
        automatically. Values are always verified to be strings regardless
        of this setting -- disabling it only turns bools/ints from
        "auto-fixed" into "hard error at eval time", it never disables the
        check itself.
      '';
    };

    apiMappings = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = { };
      example = {
        Cluster = "cluster.x-k8s.io/v1beta1";
        HCloudMachineTemplate = "infrastructure.cluster.x-k8s.io/v1beta1";
        HCloudRemediationTemplate = "infrastructure.cluster.x-k8s.io/v1beta1";
        HelmChartProxy = "addons.cluster.x-k8s.io/v1alpha1";
        HelmReleaseProxy = "addons.cluster.x-k8s.io/v1alpha1";
        HetznerCluster = "infrastructure.cluster.x-k8s.io/v1beta1";
        KubeadmConfigTemplate = "bootstrap.cluster.x-k8s.io/v1beta1";
        KubeadmControlPlane = "controlplane.cluster.x-k8s.io/v1beta1";
        MachineDeployment = "cluster.x-k8s.io/v1beta1";
        MachineHealthCheck = "cluster.x-k8s.io/v1beta1";
      };
      description = "Map of kind to apiVersion. Merged with mappings from `apiMappingFile`.";
    };

    apiMappingFile = lib.mkOption {
      type = lib.types.path;
      default = ./apiResources/v1.33.json;
      description = ''
        A JSON file to extend apiMappings.
        Generated by calling `kubectl api-resources --output=json > mappings.json`
      '';
    };

    # generated/generatedByPath/generatedWithEkn/deploymentUnits are readOnly, fully-computed
    # outputs -- there is nothing left to override/merge on them, so
    # ekn.lib.kubeValueType's recursive per-leaf JSON-schema-style validation
    # buys nothing here and would re-force the exact same expensive
    # either/oneOf machinery `kubernetes.crds` was added to avoid (Nix's
    # laziness means forcing these option *values* re-enters that machinery
    # regardless of where the underlying data came from). `types.anything`
    # merges/passes the value through structurally with no per-leaf check.
    generated = lib.mkOption {
      type = lib.types.anything;
      description = "The final, generated Kubernetes list objects";
      readOnly = true;
    };

    generatedExportable = lib.mkOption {
      type = lib.types.anything;
      description = ''
        `generated`, minus every object holding an `ekn.envSeed` reference.

        The two lists differ by who applies them, not by what they contain:

        - `generated` is for `ekn`, which resolves a seed reference at apply
          time by substituting the environment variable it names.
        - `generatedExportable` is for anything that hands objects to a
          different applier -- a GitOps target that ArgoCD then syncs,
          `kluctl`, or a manifest a person feeds to `kubectl apply`.

        A seeded object must not reach the second kind. Nothing else can
        resolve the reference, so it would apply `$ekn:env:VARNAME` as the
        literal value and overwrite a live credential with it.

        `ekn validate` deliberately stays on `generated`: a reference is a
        plain string, so the manifest is schema-valid and `kubeconform` has
        nothing to object to.
      '';
      readOnly = true;
    };

    generatedByPath = lib.mkOption {
      type = lib.types.anything;
      description = "The final, generated Kubernetes objects by attrPath";
      readOnly = true;
    };

    generatedWithEkn = lib.mkOption {
      type = lib.types.anything;
      description = ''
        Like `generated`, but with each object's `ekn` routing metadata still
        attached (`generated` strips it). Consumed by anything that needs to
        see EKN metadata alongside the rendered object, e.g. `deploymentUnits`.
      '';
      readOnly = true;
    };

    deploymentUnits = lib.mkOption {
      type = lib.types.anything;
      description = ''
        Objects grouped by the `deployment.units.<name>` they route to (via
        `ekn.deploymentUnit`), each group paired with that target's resolved
        `{path}`. Objects with no `ekn.deploymentUnit` set are omitted. The
        branch(es) these all land on come from the instance-wide
        `deployment.deployBranch`/`deployment.sourceBranch`, not from the target.
        This is derived before the `ekn` field is stripped from rendered
        Kubernetes objects.
      '';
      readOnly = true;
    };

    gitOpsTargets = lib.mkOption {
      type = lib.types.anything;
      description = ''
        Deprecated name for `kubernetes.deploymentUnits`. Reading this still
        works and prints a notice; it will be removed.

        Declared as its own output rather than aliased, because
        `mkRenamedOptionModule` writes a definition onto the option it
        forwards to, and that one is read-only.
      '';
      readOnly = true;
      internal = true;
    };

    novalidateKeys = lib.mkOption {
      type = lib.types.anything;
      description = ''
        `{kind, namespace, name}` triples for every object with
        `ekn.novalidate = true` -- deliberately just the identifying keys,
        not the full objects (which `generated`/`manifestJSONFile` already
        provide), so `ekn validate` can force_json this cheaply without
        re-forcing the entire generated set a second time. Used by
        `Validate.run()` to skip applying objects that can never be
        meaningfully verified in its throwaway etcd+apiserver harness (e.g.
        an aggregated APIService with no real backing Service running).
      '';
      readOnly = true;
    };

    sopsAgeIdentities = lib.mkOption {
      type = lib.types.listOf (
        lib.types.submodule {
          options = {
            namespace = lib.mkOption {
              type = lib.types.nonEmptyStr;
              description = "Namespace the identity Secret lives in.";
            };
            secretName = lib.mkOption {
              type = lib.types.nonEmptyStr;
              description = "Name of the Secret holding the age identity.";
            };
            key = lib.mkOption {
              type = lib.types.nonEmptyStr;
              default = "key.txt";
              description = "Key within the Secret's data the age identity is stored under.";
            };
            sopsConfigFile = lib.mkOption {
              type = lib.types.nullOr lib.types.path;
              default = null;
              description = ''
                Path to the `.sops.yaml` this identity's age public key should
                be registered in as a recipient (idempotent -- skipped if
                already present in every matching creation rule). Null (the
                default) leaves `.sops.yaml` untouched; the public key is only
                logged for manual handling.
              '';
            };
            sopsFiles = lib.mkOption {
              type = lib.types.listOf lib.types.path;
              default = [ ];
              description = ''
                Already-SOPS-encrypted files to run `sops updatekeys` on after
                the recipient is added to `sopsConfigFile`, so their data key
                gets rewrapped for the new identity too. Only consulted when
                `sopsConfigFile` is set.
              '';
            };
          };
        }
      );
      default = [ ];
      description = ''
        SOPS age decrypt identities some GitOps engine needs mounted as a
        Secret before it can sync anything referencing them (e.g. ArgoCD's
        ksops-enabled repo-server, see argocd.nix's `ksops.enable`). Purely
        additive metadata -- any module wanting this generic bootstrap
        behavior appends an entry here instead of hand-rolling its own
        bootstrap script. `ekn kubeapply` ensures each declared identity
        exists (generating a fresh age keypair the first time one is
        missing) before applying, and -- when `sopsConfigFile` is set --
        registers the public key as a recipient there and re-runs
        `sops updatekeys` on `sopsFiles`, aborting the whole apply rather
        than proceeding half-configured if either step fails. This is the
        one place ekn actually generates key material -- everywhere else,
        ekn only ever decrypts what SOPS already produced (see
        `ekn.sops.maybe_decrypt`).
      '';
    };

    rawFiles = lib.mkOption {
      type = lib.types.listOf (
        lib.types.submodule {
          # `gitOpsTarget` was the old name here too, and the alias has to be
          # inside the submodule for the same reason.
          imports = [
            (lib.mkRenamedOptionModule [ "gitOpsTarget" ] [ "deploymentUnit" ])
          ];
          options = {
            path = lib.mkOption {
              type = lib.types.path;
              description = ''
                A complete, already-rendered Kubernetes manifest file (JSON
                or YAML, apiVersion/kind/metadata all present). Tracked as a
                Nix input (so it's pinned/reproducible like everything
                else), but never parsed into `resources`/`generated` --
                `ekn kubeapply`/`ekn commit` read it directly instead.
              '';
            };
            deploymentUnit = lib.mkOption {
              type = lib.types.nullOr lib.types.str;
              default = null;
              description = ''
                Same routing as ekn.deploymentUnit on a normal object -- which
                deployment.units entry this file belongs to. Null omits it from
                every deploymentUnits entry (still kubeapply-able via the full
                kubernetes.rawFiles list, just never committed to a GitOps
                branch).

                A raw file gets none of its unit's `labels`/`annotations`, and
                so no `ekn.dev/deployment-unit` label either. Nix never parses
                it -- that is the whole point of a raw file -- so there is
                nothing here to stamp. Write the label into the file if the
                objects in it should carry one.
              '';
            };
          };
        }
      );
      default = [ ];
      description = ''
        Escape hatch from the module system for objects that must reach the
        cluster/git byte-identical to how they exist on disk -- the normal
        `kubernetes.resources` -> `generated` pipeline round-trips every
        object through Nix's own attrset representation, which
        `builtins.toJSON` always serializes with alphabetized keys, and
        which gains new sibling fields (`ekn`, `metadata` merged in by a
        module, etc.) that weren't present when the object was first
        authored. Both of those permanently invalidate a SOPS
        whole-document MAC computed before either happened -- fine for
        `ekn kubeapply`'s own decrypt (`ekn.sops.maybe_decrypt` passes
        `--ignore-mac`, since each field's own AES-GCM tag is still
        verified independently), but fatal for a MAC-verifying consumer
        that offers no such bypass (e.g. ArgoCD's ksops-enabled
        repo-server). A raw file sidesteps the round-trip entirely: it
        must already be the complete manifest from before it was
        SOPS-encrypted, so nothing about its structure changes between
        encryption and decryption.
      '';
    };
  };

  config.assertions = [
    {
      assertion = seededGitOpsObjects == [ ];
      message = ''
        These Secrets hold an `ekn.envSeed' reference and are routed to a
        GitOps target:

        ${lib.concatMapStringsSep "\n" (entry: "  ${entry}") seededGitOpsObjects}

        `ekn' is the only thing that resolves an `ekn.envSeed' reference.
        Routing an object to a GitOps target hands it to something else, so
        the object would reach the cluster with `$ekn:env:VARNAME' as its
        literal value, overwriting the credential this mechanism exists to
        deliver.

        Set `ekn.deploymentUnit = null' on the object. `ekn kubeapply' applies
        it either way, including `--target <name>'; the routing only decides
        what `ekn commit' writes to a branch.

        This fires inside a nested bootstrap instance too, where nobody
        commits the target and `ekn kubeapply --target' applies it directly.
        The assertion is deliberately blunt there rather than carved out: it
        cannot tell from here whether a given target is ever committed, and
        a wrong guess writes a credential to git.
      '';
    }
  ];

  config.kubernetes = {
    # Get apiMappings from apiMappingFile
    apiMappings =
      let
        data = lib.importJSON cfg.apiMappingFile;
        objectToAttr = object: {
          name = object.kind;
          value = if object.group or "" == "" then object.version else "${object.group}/${object.version}";
        };
      in
      lib.listToAttrs (map objectToAttr data.resources);

    generated = checked (
      lib.pipe allGenerated [
        # `ekn` belongs to the EKN compiler, not to the Kubernetes manifest.
        (map (object: removeAttrs object [ "ekn" ]))
      ]
    );

    generatedExportable = lib.filter (object: !(lib.isSeededObject object)) config.kubernetes.generated;

    generatedWithEkn = checked allGenerated;

    # The deprecated name, carrying its notice on the value. `lib.warn`
    # prints when a caller forces it, so a configuration that never reads
    # the old name says nothing -- the same shape kluctl.nix uses for its
    # own deprecation, and for the same reason: asking the option system
    # who *defined* something closes a loop through `checked`.
    gitOpsTargets = lib.warn ''
      `kubernetes.gitOpsTargets' is now `kubernetes.deploymentUnits'.

      "GitOps" named one way of delivering these objects, and it is not the
      only one: `ekn kubeapply --target <name>' applies a unit by hand, with
      no git and no controller in the path.
    '' config.kubernetes.deploymentUnits;

    deploymentUnits =
      let
        objectsByTarget = lib.pipe allGenerated [
          (lib.filter (object: (object.ekn.deploymentUnit or null) != null))
          (lib.groupBy (object: object.ekn.deploymentUnit))
          (lib.mapAttrs (
            _name: objects:
            # `ekn` belongs to the EKN compiler, not to the Kubernetes
            # manifest -- same strip as `kubernetes.generated` above. Left
            # in place here, it would land as a literal top-level field in
            # every object `ekn commit` writes to the GitOps tree,
            # permanently OutOfSync since nothing ever produces it on the
            # live-cluster side.
            map (object: removeAttrs object [ "ekn" ]) objects
          ))
        ];
        rawFilesByTarget = lib.pipe config.kubernetes.rawFiles [
          (lib.filter (f: f.deploymentUnit != null))
          (lib.groupBy (f: f.deploymentUnit))
          (lib.mapAttrs (_name: files: map (f: f.path) files))
        ];

        # Targets carrying their own `modules` -- a whole separate
        # easykubenix instance, evaluated by gitops.nix. Its objects belong
        # to this target and to nothing else: they are deliberately absent
        # from this instance's `generated`, so `ekn kubeapply --target
        # <name>` is the only thing that applies them.
        #
        # Its routing is ignored on purpose. `ekn.deploymentUnit` selects
        # between *this* instance's targets, and a nested instance's targets
        # are a different set entirely, so every object it renders is taken.
        submodulesByTarget = lib.mapAttrs (_name: t: t.instance.config.kubernetes) (
          lib.filterAttrs (_name: t: t.modules != [ ]) config.deployment.units
        );

        # `submodulesByTarget` too: a bootstrap target commonly has no
        # routed objects at all, and would otherwise be missing from
        # `deploymentUnits` entirely.
        allTargetNames = lib.unique (
          lib.attrNames objectsByTarget ++ lib.attrNames rawFilesByTarget ++ lib.attrNames submodulesByTarget
        );

        # Stamp a target's `labels`/`annotations` onto one of its objects.
        #
        # A value may be a function of the object rather than a string --
        # ArgoCD's tracking-id annotation encodes each object's own
        # group/kind/namespace/name, so a constant cannot express it (see
        # lib/argocdTrackingId.nix).
        #
        # The target's entries win over the object's own. Helm charts
        # routinely set `app.kubernetes.io/instance` to their release name,
        # which is exactly the key a GitOps engine may be reading to decide
        # what belongs to it, so losing that fight would defeat the point.
        stampTargetMetadata =
          declared: object:
          let
            merge =
              field: defined:
              let
                # A function may return `null` to decline this object --
                # ArgoCD puts no tracking annotation on a CRD, and stamping
                # one anyway is a permanent diff (see lib/argocdTrackingId.nix).
                # Declining leaves whatever the object already had, rather
                # than removing it: "no opinion", not "unset".
                resolved = lib.filterAttrs (_: value: value != null) (
                  lib.mapAttrs (_: value: if lib.isFunction value then value object else value) defined
                );
                merged = (object.metadata.${field} or { }) // resolved;
              in
              # Guarded on the result, not on `defined`: a target whose every
              # value declined must not leave `labels: {}` behind on an object
              # that had none.
              lib.optionalAttrs (merged != { }) { ${field} = merged; };
          in
          # The fast path below no longer fires: every unit carries
          # `ekn.dev/deployment-unit` in `labels` by default (see gitops.nix),
          # so `declared.labels` is never empty. It stays because a unit can
          # decline that label, and because the work it skips is small --
          # one shallow merge per object in a unit, over `metadata` only, and
          # `deploymentUnits` already copies each object here. See
          # easykubenix issue #11 on measuring evaluation cost.
          if declared.labels == { } && declared.annotations == { } then
            object
          else
            object
            // {
              metadata =
                object.metadata // merge "labels" declared.labels // merge "annotations" declared.annotations;
            };
      in
      checked (
        lib.listToAttrs (
          map (
            name:
            let
              declared =
                config.deployment.units.${name} or (throw ''
                  ekn.deploymentUnit references unknown GitOps target "${name}".
                  Declared targets: ${lib.concatStringsSep ", " (lib.attrNames config.deployment.units)}
                '');
              submodule = submodulesByTarget.${name} or null;
            in
            {
              inherit name;
              value = {
                # Named fields, not the whole `declared` submodule. This is
                # serialized to JSON for `ekn` (see eval.py's
                # `_GitOpsTargetRef` and `_unpack_gitops_target`, which read
                # exactly these three), and the submodule also carries
                # `modules` and `instance` -- module functions and an entire
                # evaluated option tree. Passing it whole would try to
                # serialize those.
                #
                # `labels`/`annotations` are left out for a different reason:
                # they are stamped into `objects` below, so they are already
                # in both the committed manifests and the apply. Their values
                # may be functions too.
                target = {
                  inherit (declared) path discriminator fieldManager;
                };
                objects = map (stampTargetMetadata declared) (
                  (objectsByTarget.${name} or [ ]) ++ (if submodule == null then [ ] else submodule.generated)
                );
                # Paths only, deliberately not read/parsed here -- reading them
                # would mean round-tripping their content through Nix's
                # attrset representation, exactly what rawFiles exists to
                # avoid (see kubernetes.rawFiles' description). `ekn
                # kubeapply`/`ekn commit` read these files themselves, in
                # Python, where insertion-ordered dicts + `sort_keys=False`
                # actually preserve source order.
                rawFiles =
                  (rawFilesByTarget.${name} or [ ])
                  ++ (if submodule == null then [ ] else map (f: f.path) submodule.rawFiles);
              };
            }
          ) allTargetNames
        )
      );

    novalidateKeys = lib.pipe allGenerated [
      (lib.filter (object: object.ekn.novalidate or false))
      (map (object: {
        inherit (object) kind;
        namespace = object.metadata.namespace or "none";
        inherit (object.metadata) name;
      }))
    ];

    # like kubernetes.objects but with transformation and generation applied
    generatedByPath = lib.foldl' (
      acc: object:
      lib.recursiveUpdate acc {
        ${object.metadata.namespace or "none"}.${object.kind}.${object.metadata.name} = object;
      }
    ) { } cfg.generated;
  };
}
