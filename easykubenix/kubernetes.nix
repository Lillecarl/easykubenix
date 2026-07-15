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
        backend = "argo";
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
          backend = "flux";
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

    generated = lib.mkOption {
      type = settingsFormat.type;
      description = "The final, generated Kubernetes list objects";
      readOnly = true;
    };

    generatedByPath = lib.mkOption {
      type = settingsFormat.type;
      description = "The final, generated Kubernetes objects by attrPath";
      readOnly = true;
    };

    eknByPath = lib.mkOption {
      type = settingsFormat.type;
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

    generated = lib.pipe generatedWithEkn [
      # `ekn` belongs to the EKN compiler, not to the Kubernetes manifest.
      (map (object: removeAttrs object [ "ekn" ]))
    ];

    eknByPath = lib.pipe generatedWithEkn [
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
              map argoRoute object.ekn.argo ++ map fluxRoute object.ekn.flux;
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
