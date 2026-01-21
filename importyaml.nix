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
    let
      yamlConfig = config;
    in
    {
      options = {
        src = mkOption {
          description = "Should be either a derivation or URL for builtins.fetchTree";
          type = types.either types.package types.str;
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
        objects = mkOption {
          description = "Generated kubernetes objects";
          type = types.listOf types.attrs;
          default = [ ];
        };
      };
      config = {
        # list to attrset convertion is just a preconfigured override
        overrides = lib.optional yamlConfig.convertLists (
          lib.mkBefore (object: (lib.walkWithPath (lib.kubeListsToAttrs object)) object)
        );

        objects =
          let
            # TODO: This is bugged if you input a fetchTree
            src =
              if isDerivation yamlConfig.src then
                yamlConfig.src
              else
                builtins.fetchTree {
                  type = "file";
                  url = yamlConfig.src;
                };

            list = lib.importJSON (
              pkgs.runCommand "yaml2json" { } # bash
                ''
                  ${pkgs.yq}/bin/yq -Scs '.' ${src} >$out
                ''
            );
          in
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
  config =
    let
      allObjects = lib.pipe cfg [
        (lib.mapAttrsToList (
          _: importspec: lib.map (object: lib.pipe object importspec.overrides) importspec.objects
        ))
        lib.flatten
      ];
    in
    {
      kubernetes.objects = lib.pipe allObjects [
        (lib.map (object: {
          ${object.metadata.namespace or "none"}.${object.kind}.${object.metadata.name} = object;
        }))
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
            "${crd.spec.group}/${version}";
        }))
        lib.listToAttrs
      ];
    };
}
