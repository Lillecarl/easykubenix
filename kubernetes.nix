{
  config,
  pkgs,
  lib,
  ...
}:
let
  cfg = config.kubernetes;
  settingsFormat = pkgs.formats.json { };
in
{
  options.kubernetes = {
    package = lib.mkPackageOption pkgs "kubernetes" { };

    resources = lib.mkOption {
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
        Kubernetes resources, grouped by namespace, then kind.
        apiVersion is automatically injected (if apiMappings for the resource exists)
        kind is automatically injected
        metadata.name is automatically injected
        metadata.namespace is automatically injected if namespace isn't "none"
      '';
      example = {
        kubernetes.resources.none.Namespace.easykubenix = { };
        kubernetes.resources.easykubenix.ConfigMap.myconfig.data.key = "value";
      };
    };

    transformers = lib.mkOption {
      type = lib.types.listOf (lib.types.functionTo lib.types.attrs);
      default = [ ];
      description = "List of functions that transform resource attrsets";
      example = ''
        kubernetes.transformers = [
          (
            resource:
            # Apply annotations to all LoadBalancers
            if resource.kind == "Service" && resource.spec.type or null == "LoadBalancer" then
              lib.recursiveUpdate resource {
                # IPv4 is scarce, share!
                metadata.annotations."metallb.io/allow-shared-ip" = "true";
                # Lowest TTL cloudflare allows
                metadata.annotations."external-dns.alpha.kubernetes.io/ttl" = "60";
              }
            # Make all services require dualstack
            else if resource.kind == "Service" then
              lib.recursiveUpdate resource {
                spec.ipFamilyPolicy = "RequireDualStack";
              }
            # Set lowest cloudflare TTL for ingress and gapi routes
            else if
              lib.elem resource.kind [
                "Ingress"
                "HTTPRoute"
              ]
            then
              lib.recursiveUpdate resource {
                metadata.annotations."external-dns.alpha.kubernetes.io/ttl" = "60";
              }
            else
              resource
          )
        ];
      '';
    };

    generators = lib.mkOption {
      type = lib.types.listOf (lib.types.functionTo (lib.types.listOf lib.types.attrs));
      default = [ ];
      description = "List of functions that generate resource attrsets";
      example = ''
        kubernetes.generators = [
          (
            resource:
            lib.optionals
              (
                (lib.elem (resource.kind or "") [
                  "Deployment"
                  "StatefulSet"
                  "DaemonSet"
                ])
                && resource.metadata.annotations.genvpa or "true" == "true"
                && !lib.hasAttrByPath [
                  resource.metadata.namespace
                  "VerticalPodAutoscaler"
                  resource.metadata.name
                ] config.kubernetes.resources
              )
              [
                {
                  apiVersion = "autoscaling.k8s.io/v1";
                  kind = "VerticalPodAutoscaler";
                  metadata = { inherit (resource.metadata) name namespace; };
                  spec = {
                    targetRef = {
                      inherit (resource) apiVersion kind;
                      inherit (resource.metadata) name;
                    };
                    updatePolicy.updateMode = "InPlaceOrRecreate";
                  };
                }
              ]
          )
        ];
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

    namespacedMappings = lib.mkOption {
      type = lib.types.attrsOf lib.types.bool;
      default = { };
      example = {
        Cluster = "cluster.x-k8s.io/v1beta1";
      };
      description = "If a kind is namespaced or not. Merged with values from `apiMappingFile`.";
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
      internal = true;
      description = "The final, generated Kubernetes list object.";
    };
  };

  config.kubernetes = {
    # Get apiMappings from apiMappingFile
    apiMappings =
      let
        data = lib.importJSON cfg.apiMappingFile;
        resourceToAttr = resource: {
          name = resource.kind;
          value =
            if !(resource ? group) || resource.group == "" then
              resource.version
            else
              "${resource.group}/${resource.version}";
        };
      in
      lib.listToAttrs (map resourceToAttr data.resources);

    namespacedMappings =
      let
        data = lib.importJSON config.kubernetes.apiMappingFile;
        resourceToAttr = resource: {
          name = resource.kind;
          value = resource.namespaced;
        };
      in
      lib.listToAttrs (map resourceToAttr data.resources);

    generated = lib.pipe cfg.resources [
      # Remove all nulls (TODO: is this a bad idea?)
      (lib.filterAttrsRecursive (_: value: value != null))
      # Convert kubernetes.resources.namespace.kind.name into a list of list resources
      (lib.collect (x: x ? apiVersion && x ? kind && x ? metadata))
      # Run a generator pass to allow generating resources from other resources (VPA)
      (resources: resources ++ lib.concatMap (r: lib.concatMap (g: g r) cfg.generators) resources)
      # Run a transformation pass over all resources (allows applying generic rules across all resources)
      (map (resource: lib.pipe resource cfg.transformers))
      # Convert attrset with _namedlist attribute true to lists. This is useful
      # when we want to override things in the Kubernetes containers list for
      # example.
      (map (lib.walkWithPath lib.kubeAttrsToLists))
    ];
  };
}
