{
  config,
  pkgs,
  lib,
  ekn,
  ...
}:
with lib;
let
  cfg = config.importyaml;
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
          type = lib.types.listOf (types.functionTo ekn.lib.kubeValueType);
          default = [ ];
        };
        yamlVersion = mkOption {
          description = ''
            YAML version to parse `src` with -- matches nanopynix's
            fromYAML11Stream/fromYAMLStream primops (in-process path) and
            ekn's hidden `_yamlToJson --yaml-version` CLI fallback
            (derivation path, used when those primops aren't registered).
            "yaml11" resolves bare leading-zero numbers as octal (e.g. a
            volume's `defaultMode: 0644` means 420, the Unix file-mode
            convention); "yaml12" reads the same literal as decimal 644.
          '';
          type = types.enum [
            "yaml11"
            "yaml12"
          ];
          default = "yaml12";
        };
        objects = mkOption {
          description = "Generated kubernetes objects";
          type = types.listOf types.attrs;
          default = [ ];
        };
      };
      config = {
        # No list-to-attribute-set pass runs on the imported YAML. An imported
        # list stays a plain list, and `ekn.lib.kubeValueType` merges it with an
        # `ekn.lib.mkNamedList` override by name when the object reaches
        # `kubernetes.objects`.
        objects =
          let
            src =
              if isDerivation yamlConfig.src then
                yamlConfig.src
              else if lib.hasPrefix "/" yamlConfig.src then
                # Local file path (store path from path-to-string conversion)
                yamlConfig.src
              else
                builtins.fetchTree {
                  type = "file";
                  url = yamlConfig.src;
                };

            list = ekn.lib.parseYAMLStream {
              inherit src;
              yamlVersion = yamlConfig.yamlVersion;
            };
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
            lib.mkDefault "${crd.spec.group}/${version}";
        }))
        lib.listToAttrs
      ];
    };
}
