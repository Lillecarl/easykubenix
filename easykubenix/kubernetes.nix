{
  config,
  pkgs,
  lib,
  ...
}:
let
  cfg = config.kubernetes;
  settingsFormat = pkgs.formats.json { };
  generatedWithEkn = lib.pipe cfg.objects [
    # Convert kubernetes.objects.namespace.kind.name into a list of objects
    (lib.collect (x: x ? apiVersion && x ? kind && x ? metadata))
    # Run a generator pass to generate objects from objects.
    (
      objects:
      objects
      ++ lib.pipe objects [
        (
          lib.concatMap (
            object:
            lib.pipe (map (generator: generator object) cfg.generators) [
              # A generator declining to fire returns `{ }` -- filter it out
              # here, before merging in a default `ekn`, otherwise the merge
              # below turns it into a non-empty `{ ekn = ...; }` stub with no
              # kind/apiVersion/metadata that survives downstream.
              (lib.filter (generated: generated != { }))
              (
                map (
                  generated:
                  generated
                  // lib.optionalAttrs (!(generated ? ekn)) {
                    ekn = object.ekn or { };
                  }
                )
              )
            ]
          )
        )
      ]
    )
    # Run a transformation pass over all objects
    (map (object: lib.pipe object cfg.transformers))
    # Run filter pass over all objects
    (lib.filter (object: lib.all (function: function object) cfg.filters))
    # Convert attrset with _namedlist attribute true to lists. This is useful
    # when we want to override things in the Kubernetes containers list for
    # example.
    (map (lib.walkWithPath lib.kubeAttrsToLists))
  ];
  # cfg.crds objects deliberately don't flow through generatedWithEkn's
  # pipeline above (generators/transformers/filters/kubeAttrsToLists) -- that
  # walk is exactly what kubernetes.crds exists to avoid paying for. They're
  # concatenated in afterwards, already complete and untouched.
  allGenerated = generatedWithEkn ++ cfg.crds;
in
{
  imports = [
    (lib.mkAliasOptionModule [ "kubernetes" "resources" ] [ "kubernetes" "objects" ])
    (lib.mkRemovedOptionModule [
      "kubernetes"
      "namespacedMappings"
    ] "RIP namespacedMappings")
  ];
  options.kubernetes = {
    package = lib.mkPackageOption pkgs "kubernetes" { };

    templates = lib.mkOption {
      type = lib.types.attrsOf (lib.types.functionTo settingsFormat.type);
      default = { };
      description = "Typed resource template functions, usually created with the Adios-backed `template` helper.";
    };

    objects = lib.mkOption {
      type = lib.types.attrsOf (
        lib.types.submodule (
          { name, ... }:
          let
            namespace = name;
          in
          {
            freeformType = lib.types.attrsOf (
              lib.types.submodule (
                { name, ... }:
                let
                  kind = name;
                in
                {
                  freeformType = lib.types.attrsOf (
                    lib.types.submodule (
                      { name, ... }:
                      {
                        freeformType = settingsFormat.type;
                        options = {
                          ekn = lib.mkOption {
                            type = lib.types.submodule {
                              options = {
                                gitOpsTarget = lib.mkOption {
                                  type = lib.types.nullOr lib.types.str;
                                  default = null;
                                  description = ''
                                    Name of the `gitops.targets.<name>` this object routes
                                    to. Deliberately single-valued, not a list: an object
                                    synced by two GitOps targets at once means two
                                    controllers independently reconciling (and potentially
                                    pruning) the same resource. This is EKN-only routing
                                    metadata and is stripped before Kubernetes manifests
                                    are rendered.
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
                              freeformType = settingsFormat.type;
                              options.name = lib.mkOption {
                                type = lib.types.str;
                                default = name;
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
      # per-object submodule, whose freeformType is settingsFormat.type (a
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
        settingsFormat.type value-checking entirely. Still passes through
        `ekn.gitOpsTarget` GitOps routing and the final YAML render, just
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

    # generated/generatedByPath/generatedWithEkn/gitopsTargets are readOnly, fully-computed
    # outputs -- there is nothing left to override/merge on them, so
    # settingsFormat.type's recursive per-leaf JSON-schema-style validation
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
        see EKN metadata alongside the rendered object, e.g. `gitopsTargets`.
      '';
      readOnly = true;
    };

    gitopsTargets = lib.mkOption {
      type = lib.types.anything;
      description = ''
        Objects grouped by the `gitops.targets.<name>` they route to (via
        `ekn.gitOpsTarget`), each group paired with that target's resolved
        `{branch, path}`. Objects with no `ekn.gitOpsTarget` set are omitted.
        This is derived before the `ekn` field is stripped from rendered
        Kubernetes objects.
      '';
      readOnly = true;
    };
  };

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

    generated = lib.pipe allGenerated [
      # `ekn` belongs to the EKN compiler, not to the Kubernetes manifest.
      (map (object: removeAttrs object [ "ekn" ]))
    ];

    generatedWithEkn = allGenerated;

    gitopsTargets = lib.pipe allGenerated [
      (lib.filter (object: (object.ekn.gitOpsTarget or null) != null))
      (lib.groupBy (object: object.ekn.gitOpsTarget))
      (lib.mapAttrs (
        name: objects:
        {
          target =
            config.gitops.targets.${name} or (throw ''
              ekn.gitOpsTarget references unknown GitOps target "${name}".
              Declared targets: ${lib.concatStringsSep ", " (lib.attrNames config.gitops.targets)}
            '');
          inherit objects;
        }
      ))
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
