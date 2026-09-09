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
        transformers = mkOption {
          description = ''
            Functions from this source's whole object list to a new one,
            applied in order. Run after `overrides` and before the objects are
            grouped into `kubernetes.objects`.

            `overrides` is the per-object hook and cannot express anything
            that needs the set: detecting two objects that render to one
            identity, or deriving a value from a sibling. This is that hook.

            Being before the grouping is the point, and it is what
            `kubernetes.transformers` cannot offer. Grouping reads
            `metadata.namespace`, and an object without one goes to the `none`
            bucket, where kubernetes.nix deliberately injects no namespace. A
            manifest may legitimately omit it, because `kubectl apply
            --namespace` does not rewrite the file either -- it lets the API
            server default it. Only a hook here runs early enough to put one
            back.

            easykubenix ships no transformer. This is a seam; the policy is
            the caller's, because "which kinds are namespaced" needs API scope
            data that a project has and this module does not.

            Not to be confused with `kubernetes.transformers`, which is per
            object, instance-wide, and runs long after grouping.

            A marker is safe here, and that is the opposite of the rule for
            `kubernetes.transformers`. This output still has to pass through
            `kubernetes.objects`, whose freeform type is `kubeValueType`, so
            `namedListOf` resolves an `mkNamedList` when it merges. Introducing
            one in `kubernetes.transformers` is what needs `needsMarkerPass`
            (kubernetes.nix), because that seam runs past the type and a marker
            it leaves behind reaches the manifest as a literal `_type` field.
          '';
          # `listOf attrs` rather than the recursive value type: these objects
          # are typed again when they land in `kubernetes.objects`, so
          # validating each leaf here would pay that cost twice. See
          # easykubenix issue #11.
          type = lib.types.listOf (types.functionTo (lib.types.listOf lib.types.attrs));
          default = [ ];
          example = lib.literalExpression ''
            [ (objects: map (object: object // { metadata = object.metadata // { namespace = "app"; }; }) objects) ]
          '';
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
          _: importspec:
          # Per-object first, then the whole set. A set transformer that has
          # to reason about identities must see what `overrides` produced,
          # not what the file contained.
          lib.pipe (lib.map (
            object: lib.pipe object importspec.overrides
          ) importspec.objects) importspec.transformers
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
