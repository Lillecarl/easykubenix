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
              transformers = mkOption {
                description = ''
                  Functions from this release's whole object list to a new
                  one, applied in order. Run after `overrides` and before the
                  objects are grouped into `kubernetes.objects`.

                  `overrides` is the per-object hook and cannot express
                  anything that needs the set: detecting two objects that
                  render to one identity, or deriving a value from a sibling.
                  This is that hook.

                  Being before the grouping is the point, and it is what
                  `kubernetes.transformers` cannot offer. Grouping reads
                  `metadata.namespace`, and an object without one goes to the
                  `none` bucket, where kubernetes.nix deliberately injects no
                  namespace. A chart may legitimately omit it -- `helm
                  install` does not rewrite the manifest either, it lets the
                  API server default the namespace to the release's. Only a
                  hook here still knows `namespace`, so only a hook here can
                  put it back.

                  easykubenix ships no transformer. This is a seam; the
                  policy is the caller's, because "which kinds are namespaced"
                  needs API scope data that a project has and this module does
                  not.

                  Not to be confused with `kubernetes.transformers`, which is
                  per object, instance-wide, and runs long after grouping.

                  A marker is safe here, and that is the opposite of the rule
                  for `kubernetes.transformers`. This output still has to pass
                  through `kubernetes.objects`, whose freeform type is
                  `kubeValueType`, so `namedListOf` resolves an `mkNamedList`
                  when it merges. Introducing one in `kubernetes.transformers`
                  is what needs `needsMarkerPass` (kubernetes.nix), because
                  that seam runs past the type and a marker it leaves behind
                  reaches the manifest as a literal `_type` field.
                '';
                # `listOf attrs` rather than the recursive value type: these
                # objects are typed again when they land in
                # `kubernetes.objects`, so validating each leaf here would pay
                # that cost twice. See easykubenix issue #11.
                type = lib.types.listOf (types.functionTo (lib.types.listOf lib.types.attrs));
                default = [ ];
                example = lib.literalExpression ''
                  [ (objects: map (object: object // { metadata = object.metadata // { namespace = "app"; }; }) objects) ]
                '';
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
              # No list-to-attribute-set pass runs on the chart output. A
              # rendered chart list stays a plain list, and
              # `ekn.lib.kubeValueType` merges it with an
              # `ekn.lib.mkNamedList` override by name when the object reaches
              # `kubernetes.objects`.
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
          _: release:
          # Per-object first, then the whole set. A set transformer that has
          # to reason about identities must see what `overrides` produced,
          # not what the chart rendered.
          lib.pipe (lib.map (object: lib.pipe object release.overrides) release.objects) release.transformers
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
