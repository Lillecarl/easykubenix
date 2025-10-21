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
          {
            options = {
              name = mkOption {
                description = "Helm release name";
                type = types.str;
                default = name;
              };
              chart = mkOption {
                description = "Helm chart to use";
                type = types.package;
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
                description = "Overrides to apply to all chart resources, don't do namespace here";
                type = types.listOf settingsFormat.type;
                example = [
                  {
                    metadata.annotations."who.is/coolest" = "lillecarl";
                  }
                ];
                default = [ ];
              };
              # Do we really need this here? Doesn't Helm take care of this?
              overrideNamespace = lib.mkOption {
                description = "Override namespace for all namespaced resources";
                type = types.nullOr types.str;
                default = null;
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
                  Inform Helm about which CRDs are available in the cluster (`--api-versions` option).
                  This is useful for charts which contain `.Capabilities.APIVersions.Has` checks.
                  If you use `kubernetes.customTypes` to make kubenix aware of CRDs, it will include those as well by default.
                '';
                type = types.listOf types.str;
                default = lib.attrValues globalConfig.kubernetes.apiMappings;
              };

              objects = mkOption {
                description = "Generated kubernetes objects";
                type = types.listOf types.attrs;
                default = [ ];
              };
            };

            config.objects =
              let
                list = importJSON (
                  pkgs.chart2json.override { kubernetes-helm = cfg.package; } {
                    inherit (config)
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
                );
              in
              if config.overrideNamespace != null then
                lib.map (
                  resource:
                  let
                    namespaced =
                      if lib.hasAttr resource.kind globalConfig.kubernetes.namespacedMappings then
                        globalConfig.kubernetes.namespacedMappings.${resource.kind}
                      else
                        throw "kind ${resource.kind} doesn't have a namespacedMapping";
                  in
                  if namespaced then
                    lib.recursiveUpdate resource { metadata.namespace = config.overrideNamespace; }
                  else
                    resource
                ) list
              else
                list;
          }
        )
      );
      default = { };
    };
  };

  config = {
    kubernetes.resources = mkMerge (
      flatten (
        mapAttrsToList (
          _: release:
          map (object: {
            ${object.metadata.namespace or "none"}.${object.kind}."${object.metadata.name}" = mkMerge (
              [
                (
                  if release.convertLists then # fmt
                    (lib.walkWithPath lib.kubeListsToAttrs) object
                  else
                    object
                )
              ]
              ++ release.overrides
            );
          }) release.objects
        ) cfg.releases
      )
    );
  };
}
