# helm defines a kubenix module with options for using helm charts with kubenix
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
  _class = "kubernetes";

  options.helm = {
    package = lib.mkPackageOption pkgs "kubernetes-helm" { };
    releases = mkOption {
      description = ''
        Attribute set of helm releases.

        A thin wrapper over `ekn.lib.importHelm`, which is the primitive and
        takes the same arguments. Use the function directly when building
        objects from a plain function rather than from a module -- it returns a
        config fragment you place with `lib.mkMerge`, with no option to declare
        and read back.
      '';
      type = types.attrsOf (
        types.submodule (
          { name, ... }:
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
                description = ''
                  Namespace to install the helm chart to.

                  This is `.Release.Namespace` and `--namespace`, and it also
                  creates the Namespace object. It does NOT put a namespace on
                  an object whose template omits one -- see
                  `ekn.lib.importHelm`.
                '';
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
              transformers = mkOption {
                description = ''
                  Functions from this release's whole object list to a new one,
                  applied in order. Passed straight through to
                  `ekn.lib.importHelm` and on to `ekn.lib.importYaml`, where
                  the semantics are documented -- they run before the CRD split
                  and before the objects are grouped by namespace, they see
                  CRDs, and a marker is safe in one.

                  This replaced a per-object `overrides` option. `map f`
                  expresses that, so the two hooks bought nothing but an
                  ordering rule to remember.
                '';
                type = lib.types.listOf (types.functionTo (lib.types.listOf lib.types.attrs));
                default = [ ];
                example = lib.literalExpression ''
                  [ (objects: map (object: object // { metadata = object.metadata // { namespace = "app"; }; }) objects) ]
                '';
              };
              crdSplit = mkOption {
                description = ''
                  Route CustomResourceDefinitions to `kubernetes.crds` instead
                  of `kubernetes.resources`. See `ekn.lib.importYaml` for the
                  measured cost of turning it off, and for the pipeline stages
                  a split CRD skips.
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
            };
          }
        )
      );
      default = { };
    };
  };

  # One fragment per release. Every downstream concern -- transformers, the CRD
  # split, namespace grouping, apiMappings from CRDs -- lives in the primitive,
  # so this module and importyaml.nix cannot drift apart. They used to carry
  # byte-identical `apiMappings` blocks.
  #
  # The option paths below are written out rather than `mkMerge`-ing the
  # fragments whole. See the same note in importyaml.nix: making the set of
  # defined options depend on `cfg` recurses, and this module reaches it faster
  # because `kubeVersion` defaults from `config.kubernetes.package.version`.
  config =
    let
      fragments = lib.mapAttrsToList (
        _name: release:
        ekn.lib.importHelm {
          # The configurable Helm binary. The primitive defaults to the package
          # set's, so passing it here is what keeps `helm.package` working.
          helmPackage = cfg.package;
          inherit (release)
            chart
            name
            namespace
            values
            kubeVersion
            includeCRDs
            noHooks
            apiVersions
            yamlVersion
            transformers
            crdSplit
            ;
        }
      ) cfg.releases;
    in
    {
      kubernetes.resources = lib.mkMerge (map (fragment: fragment.kubernetes.resources) fragments);
      kubernetes.crds = lib.concatMap (fragment: fragment.kubernetes.crds) fragments;
      kubernetes.apiMappings = lib.mkMerge (map (fragment: fragment.kubernetes.apiMappings) fragments);
    };
}
