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
  /*
    Recursively applies a function `f` to every non-list, non-attrset value
    in a nested structure. The function `f` receives two arguments:
    `f key value`, where `key` is the attribute name. For list elements,
    `key` is null.
  */
  deepMapWithKey =
    f:
    let
      # Recursive helper function
      recurse =
        key: value:
        if lib.isAttrs value then
          lib.mapAttrs (name: val: recurse name val) value
        else if lib.isList value then
          # List elements don't have a key, so pass null
          lib.map (elem: recurse null elem) value
        else
          # At a leaf, apply the user's function with the key and value
          f key value;
    in
    # Start the recursion; the top-level value has no parent key
    value: recurse null value;

  importyaml = types.submodule (
    { config, ... }:
    {
      options = {
        src = mkOption {
          description = "Should be either a derivation or URL for builtins.fetchTree";
          type = types.either types.package types.str;
        };
        overrideNamespace = lib.mkOption {
          description = "Override any attribute with name namespace DEEPLY";
          type = types.nullOr types.str;
          default = null;
        };
        overrides = mkOption {
          description = "Overrides to apply to all resources";
          type = types.listOf settingsFormat.type;
          default = [ ];
        };
        manifests = lib.mkOption {
          type = types.listOf settingsFormat.type;
          internal = true;
        };
      };
      config = {
        manifests =
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
            # Update attribute with name namespace through the entire manifest
            lib.pipe list [
              # Special case for updating namespace resources
              (lib.map (
                v:
                if v.kind or null == "Namespace" then
                  lib.recursiveUpdate v {
                    metadata.name = config.overrideNamespace;
                  }
                else
                  v
              ))
              # Recursively update anything called "namespace" with the new value
              # There's no way to import a YAML with multiple namespace specs correctly.
              (deepMapWithKey (n: v: if n == "namespace" then config.overrideNamespace else v))
            ]
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
                object
              ]
              ++ yaml.overrides
            );
          }) yaml.manifests
        ) cfg
      )
    );
  };
}
