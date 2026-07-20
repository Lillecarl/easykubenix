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
            map (
              generator:
              let
                generated = generator object;
              in
              generated
              // lib.optionalAttrs (!(generated ? ekn)) {
                ekn = object.ekn or { };
              }
            ) cfg.generators
          )
        )
        (lib.filter (x: x != { }))
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
  generatedWithEknByPath = lib.foldl' (
    acc: object:
    lib.recursiveUpdate acc {
      ${object.metadata.namespace or "none"}.${object.kind}.${object.metadata.name} = object;
    }
  ) { } generatedWithEkn;
  argoRoute = application:
    if application.kind != "Application" || !(lib.hasPrefix "argoproj.io/" application.apiVersion) then
      throw "ekn.argo must reference an Argo CD Application"
    else
      {
        branch = application.spec.source.targetRevision;
        path = application.spec.source.path;
      };
  fluxRoute = kustomization:
    if kustomization.kind != "Kustomization" || !(lib.hasPrefix "kustomize.toolkit.fluxcd.io/" kustomization.apiVersion) then
      throw "ekn.flux must reference a Flux Kustomization"
    else
      let
        sourceRef = kustomization.spec.sourceRef;
        namespace = sourceRef.namespace or kustomization.metadata.namespace or "none";
        source = lib.attrByPath [ namespace sourceRef.kind sourceRef.name ]
          (throw "ekn.flux Kustomization sourceRef does not resolve to a Kubernetes resource")
          generatedWithEknByPath;
      in
      if source.kind != "GitRepository" || !(lib.hasPrefix "source.toolkit.fluxcd.io/" source.apiVersion) then
        throw "ekn.flux Kustomization sourceRef must reference a Flux GitRepository"
      else
        {
          branch = source.spec.ref.branch;
          path = kustomization.spec.path;
        };
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
                                argo = lib.mkOption {
                                  type = lib.types.listOf lib.types.attrs;
                                  default = [ ];
                                  description = ''
                                    Argo Application resources which should receive this
                                    object. This is EKN-only routing metadata and is
                                    stripped before Kubernetes manifests are rendered.
                                  '';
                                };

                                flux = lib.mkOption {
                                  type = lib.types.listOf lib.types.attrs;
                                  default = [ ];
                                  description = ''
                                    Flux Kustomization resources which should receive
                                    this object. This is EKN-only routing metadata and
                                    is stripped before Kubernetes manifests are rendered.
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
        `ekn.argo`/`ekn.flux` GitOps routing and the final YAML render, just
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

    # generated/generatedByPath/eknByPath are readOnly, fully-computed
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

    eknByPath = lib.mkOption {
      type = lib.types.anything;
      description = ''
        Resolved EKN GitOps routes by Kubernetes object path. This is derived
        before the `ekn` field is stripped from rendered Kubernetes objects.
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

    eknByPath = lib.pipe allGenerated [
      (lib.filter (
        object:
        let
          ekn = object.ekn or { };
        in
        (ekn.argo or [ ]) != [ ] || (ekn.flux or [ ]) != [ ]
      ))
      (
        objects:
        lib.foldl' (
          acc: object:
          lib.recursiveUpdate acc {
            ${object.metadata.namespace or "none"}.${object.kind}.${object.metadata.name} =
              # `or [ ]`: kubernetes.crds objects (renderChart-tagged) only
              # ever set ekn.argo, never ekn.flux -- unlike
              # kubernetes.objects' per-object submodule, there's no option
              # default filling in the missing field for them.
              map argoRoute (object.ekn.argo or [ ]) ++ map fluxRoute (object.ekn.flux or [ ]);
          }
        ) { } objects
      )
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
