# helm defines kubenix module with options for using helm charts with kubenix
# Based on hall/kubenix
{
  config,
  lib,
  pkgs,
  ekn,
  ...
}:
with lib;
let
  cfg = config.helm;
  globalConfig = config;
in
{
  options.helm = {
    package = lib.mkPackageOption pkgs "kubernetes-helm" { };
    releases = mkOption {
      description = "Attribute set of helm releases";
      type = types.attrsOf (
        types.submodule (
          { config, name, ... }:
          let
            releaseConfig = config;
          in
          {
            options = {
              name = mkOption {
                description = "Helm release name";
                type = types.str;
                default = name;
              };
              chart = mkOption {
                description = "Helm chart to use";
                type = types.either types.package types.path;
              };
              namespace = mkOption {
                description = "Namespace to install helm chart to";
                type = types.nullOr types.str;
                default = null;
              };
              values = mkOption {
                description = "Values to pass to chart";
                type = ekn.lib.kubeValueType;
                default = { };
              };
              kubeVersion = mkOption {
                description = "Kubernetes version to build chart for";
                type = types.str;
                default = globalConfig.kubernetes.package.version;
              };
              overrides = mkOption {
                description = "Overrides to apply to all chart objects, don't do namespace here";
                type = lib.types.listOf (types.functionTo ekn.lib.kubeValueType);
                default = [ ];
              };
              convertLists = mkOption {
                description = ''
                  Converts lists where all entires have a name attribute into
                  attrsets instead. These attrsets are converted back into
                  lists before rendering Kubernetes manifests.
                '';
                type = types.bool;
                default = true;
              };
              yamlVersion = mkOption {
                description = ''
                  YAML version to parse the rendered `helm template` output
                  with -- matches nanopynix's fromYAML11Stream/fromYAMLStream
                  primops (in-process path) and ekn's hidden `_yamlToJson
                  --yaml-version` CLI fallback (derivation path, used when
                  those primops aren't registered). Defaults to "yaml11"
                  here (unlike importyaml.nix's "yaml12" default) because
                  Helm's Go YAML library commonly emits bare leading-zero
                  numbers with octal semantics -- e.g. a volume's
                  `defaultMode: 0644` means 420, the Unix file-mode
                  convention, which "yaml12" would misread as decimal 644.
                '';
                type = types.enum [
                  "yaml11"
                  "yaml12"
                ];
                default = "yaml11";
              };
              includeCRDs = mkOption {
                description = ''
                  Whether to include CRDs.

                  Warning: Always including CRDs here is dangerous and can break CRs in your cluster as CRDs may be updated unintentionally.
                  An interactive `helm install` NEVER updates CRDs, only installs them when they are not existing.
                  See https://github.com/helm/community/blob/aa8e13054d91ee69857b13149a9652be09133a61/hips/hip-0011.md

                  Only set this to true if you know what you are doing and are manually checking the included CRDs for breaking changes whenever updating the Helm chart.
                '';
                type = types.bool;
                default = false;
              };
              noHooks = mkOption {
                description = ''
                  Wether to include Helm hooks.

                  Without this all hooks run immediately on apply since we are bypassing the Helm CLI.
                  However, some charts only have minor validation hooks (e.g., upgrade version skew validation) and are safe to ignore.
                '';
                type = types.bool;
                default = false;
              };

              apiVersions = mkOption {
                description = ''
                  Inform Helm about which API versions are available in the cluster (`--api-versions` option).
                  This is useful for charts which contain `.Capabilities.APIVersions.Has` checks.
                '';
                type = types.listOf types.str;
                default = [ ];
              };

              objects = mkOption {
                description = "Generated kubernetes objects";
                type = types.listOf ekn.lib.kubeValueType;
                default = [ ];
              };
            };

            config = {
              # list to attrset convertion is just a preconfigured override
              overrides = lib.optional releaseConfig.convertLists (
                lib.mkBefore (object: (lib.walkWithPath (lib.kubeListsToAttrs object)) object)
              );

              objects =
                let
                  resourcesYaml = pkgs.chart2yaml.override { kubernetes-helm = cfg.package; } {
                    inherit (releaseConfig)
                      chart
                      name
                      namespace
                      values
                      kubeVersion
                      includeCRDs
                      noHooks
                      apiVersions
                      ;
                  };
                  list = ekn.lib.parseYAMLStream {
                    src = resourcesYaml;
                    yamlVersion = releaseConfig.yamlVersion;
                  };
                in
                list
                ++ lib.optional (releaseConfig.namespace != null) {
                  apiVersion = "v1";
                  kind = "Namespace";
                  metadata.name = releaseConfig.namespace;
                };
            };
          }
        )
      );
      default = { };
    };
  };

  config =
    let
      allObjects = lib.pipe cfg.releases [
        (lib.mapAttrsToList (
          _: release: lib.map (object: lib.pipe object release.overrides) release.objects
        ))
        lib.flatten
      ];
    in
    {
      kubernetes.objects = lib.pipe allObjects [
        (lib.map (
          object:
          let
            kind = object.kind or (throw "no kind for ${object}");
            name = object.metadata.name or (throw "no name for ${object}");
            namespace = object.metadata.namespace or "none";
          in
          {
            ${namespace}.${kind}.${name} = object;
          }
        ))
        lib.mkMerge
      ];
      kubernetes.apiMappings = lib.pipe allObjects [
        (lib.filter (object: object.kind or null == "CustomResourceDefinition"))
        (map (crd: {
          name = crd.spec.names.kind;
          value =
            let
              version = lib.pipe crd.spec.versions [
                (lib.filter (x: x.storage or false == true))
                lib.head
                (x: x.name)
              ];
            in
            lib.mkDefault "${crd.spec.group}/${version}";
        }))
        lib.listToAttrs
      ];
    };
}
