# helm defines kubenix module with options for using helm charts with kubenix
# Based on hall/kubenix
{
  config,
  lib,
  pkgs,
  ...
}:
with lib;
let
  cfg = config.helm;
  settingsFormat = pkgs.formats.json { };
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
                type = settingsFormat.type;
                default = { };
              };
              kubeVersion = mkOption {
                description = "Kubernetes version to build chart for";
                type = types.str;
                default = globalConfig.kubernetes.package.version;
              };
              overrides = mkOption {
                description = "Overrides to apply to all chart objects, don't do namespace here";
                type = lib.types.listOf (types.functionTo settingsFormat.type);
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
                type = types.listOf settingsFormat.type;
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
                  list = lib.filter (x: x != null) (
                    importJSON (
                      pkgs.chart2json.override { kubernetes-helm = cfg.package; } {
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
                      }
                    )
                  );
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
