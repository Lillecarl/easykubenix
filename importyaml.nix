{
  config,
  pkgs,
  lib,
  ...
}:
with lib;
let
  cfg = config.importyaml;
  settingsFormat = pkgs.formats.json { };
  globalConfig = config;

  importyaml = types.submodule (
    { config, ... }:
    {
      options = {
        src = mkOption {
          description = "Should be either a derivation or URL for builtins.fetchTree";
          type = types.either types.package types.str;
        };
        overrides = mkOption {
          description = "Overrides to apply to all resources, don't do namespace here";
          type = types.listOf settingsFormat.type;
          default = [ ];
        };
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
        objects = mkOption {
          description = "Generated kubernetes objects";
          type = types.listOf types.attrs;
          default = [ ];
        };
      };
      config = {
        objects =
          let
            src =
              if isDerivation config.src then
                config.src
              else
                builtins.fetchTree {
                  type = "file";
                  url = config.src;
                };

            # Thanks kubenix
            jsonFile =
              pkgs.runCommandNoCCLocal "yaml2json" { } # bash
                ''
                  # Remove null values
                  ${lib.getExe pkgs.yq} -Scs 'walk(
                    if type == "object" then
                      with_entries(select(.value != null))
                    elif type == "array" then
                      map(select(. != null))
                    else
                      .
                    end)' ${toString src} >$out
                '';
            jsonStr = builtins.readFile jsonFile;
            list = builtins.fromJSON jsonStr;
          in
          if config.overrideNamespace != null then
            lib.map (
              resource:
              let
                namespaced =
                  globalConfig.kubernetes.namespacedMappings.${resource.kind} or throw
                    "kind ${resource.kind} doesn't have a namespacedMapping";
              in
              if namespaced then
                lib.recursiveUpdate resource { metadata.namespace = config.overrideNamespace; }
              else
                resource
            ) list
          else
            list;
      };
    }
  );
in
{
  options.importyaml = mkOption {
    type = types.attrsOf importyaml;
    default = { };
  };
  config = {
    # Thanks kubenix for this voodoo magic function
    kubernetes.resources = mkMerge (
      flatten (
        mapAttrsToList (
          _: yaml:
          map (object: {
            ${object.metadata.namespace or "none"}.${object.kind}."${object.metadata.name}" = mkMerge (
              [
                (
                  if yaml.convertLists then # fmt
                    (lib.walkWithPath lib.kubeListsToAttrs) object
                  else
                    object
                )
              ]
              ++ yaml.overrides
            );
          }) yaml.objects
        ) cfg
      )
    );
  };
}
