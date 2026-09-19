{
  config,
  lib,
  ekn,
  ...
}:
with lib;
let
  cfg = config.importyaml;

  importyaml = types.submodule {
    options = {
      src = mkOption {
        description = "Should be either a derivation or URL for builtins.fetchTree";
        type = types.either types.package types.str;
      };
      transformers = mkOption {
        description = ''
          Functions from this source's whole object list to a new one, applied
          in order. Passed straight through to `ekn.lib.importYaml`, where the
          semantics are documented -- they run before the CRD split and before
          the objects are grouped by namespace, they see CRDs, and a marker is
          safe in one.

          This replaced a per-object `overrides` option. `map f` expresses
          that, so the two hooks bought nothing but an ordering rule to
          remember.
        '';
        type = lib.types.listOf (types.functionTo (lib.types.listOf lib.types.attrs));
        default = [ ];
        example = literalExpression ''
          [ (objects: map (object: object // { metadata = object.metadata // { namespace = "app"; }; }) objects) ]
        '';
      };
      crdSplit = mkOption {
        description = ''
          Route CustomResourceDefinitions to `kubernetes.crds` instead of
          `kubernetes.resources`. See `ekn.lib.importYaml` for the measured
          cost of turning it off, and for the pipeline stages a split CRD
          skips.
        '';
        type = types.bool;
        default = true;
      };
    };
  };
in
{
  _class = "kubernetes";

  options.importyaml = mkOption {
    description = ''
      Kubernetes manifests imported from YAML.

      A thin wrapper over `ekn.lib.importYaml`, which is the primitive and
      takes the same arguments. Use the function directly when building
      objects from a plain function rather than from a module -- it returns a
      config fragment you place with `lib.mkMerge`, with no option to declare
      and read back.
    '';
    type = types.attrsOf importyaml;
    default = { };
  };

  # One fragment per source. Every downstream concern -- transformers, the CRD
  # split, namespace grouping, apiMappings from CRDs -- lives in the primitive,
  # so this module and helm.nix cannot drift apart.
  #
  # The option paths below are written out rather than `mkMerge`-ing the
  # fragments whole. `config = mkMerge (mapAttrsToList ... cfg)` makes the set
  # of options this module defines depend on `cfg`, so the module system has to
  # force `cfg` to find out what is defined -- and that reaches
  # `config.kubernetes.*`, which needs every module's definitions, including
  # this one. Infinite recursion, and the message names neither module. A
  # consumer of `ekn.lib.importYaml` merges the fragment whole and is fine,
  # because their fragment does not decide which options their module declares.
  config =
    let
      fragments = lib.mapAttrsToList (
        _name: importspec:
        ekn.lib.importYaml {
          inherit (importspec)
            src
            transformers
            crdSplit
            ;
        }
      ) cfg;
    in
    {
      kubernetes.resources = lib.mkMerge (map (fragment: fragment.kubernetes.resources) fragments);
      kubernetes.crds = lib.concatMap (fragment: fragment.kubernetes.crds) fragments;
      kubernetes.apiMappings = lib.mkMerge (map (fragment: fragment.kubernetes.apiMappings) fragments);
    };
}
