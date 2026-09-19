# Render a Helm chart and turn the result into easykubenix configuration.
#
# A producer in front of `importYaml`, and nothing more: `helm template`
# emits a YAML stream, which is exactly what `importYaml` consumes. Every
# argument below that is not about rendering is passed straight through, so
# the two paths cannot grow different downstreams.
#
#   config = lib.mkMerge [
#     (ekn.lib.importHelm {
#       chart = pkgs.fetchHelm { ... };
#       name = "argo-cd";
#       namespace = "argocd";
#       transformers = [ ... ];
#     })
#   ];
{
  lib,
  pkgs,
  importYaml,
}:
{
  # The chart itself: a derivation or path. Use `pkgs.fetchHelm` for one from
  # a repository.
  chart,
  # Helm release name. `.Release.Name` in the templates.
  name,
  # `.Release.Namespace` in the templates, and `--namespace`.
  #
  # This does NOT put a namespace on an object whose template omits one --
  # `helm install` does not rewrite the manifest either, it lets the API
  # server default it. easykubenix groups by `metadata.namespace` instead, so
  # such an object is treated as cluster-scoped. Pass a `transformers` entry
  # to default it; the namespace is in scope at the call site because you
  # supplied it here.
  namespace ? null,
  values ? { },
  kubeVersion ? null,
  includeCRDs ? false,
  noHooks ? false,
  apiVersions ? null,
  # Passed through untouched. See importYaml.nix.
  transformers ? [ ],
  crdSplit ? true,
  # The Helm binary that renders the chart. Null takes the one in the package
  # set; `helm.releases` passes `helm.package` so that option keeps working.
  helmPackage ? null,
}:
let
  chart2yaml =
    if helmPackage == null then
      pkgs.chart2yaml
    else
      pkgs.chart2yaml.override { kubernetes-helm = helmPackage; };

  rendered = chart2yaml {
    inherit
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

  # The release's own namespace, created alongside its objects. `helm install
  # --create-namespace` does this; a rendered template never contains it.
  #
  # Appended as a trailing transformer rather than merged into the result, for
  # two reasons. It keeps this function's return a plain config fragment,
  # identical in shape to `importYaml`'s, so a caller can read
  # `.kubernetes.resources` off either. And it puts the Namespace through the
  # same grouping as everything else, which is what files it under `none` --
  # a Namespace has no `metadata.namespace` of its own.
  #
  # Trailing, so a caller's own transformers do not see it. It is this
  # function's bookkeeping, not part of what the chart rendered.
  appendNamespace =
    objects:
    objects
    ++ [
      {
        apiVersion = "v1";
        kind = "Namespace";
        metadata.name = namespace;
      }
    ];

  imported = importYaml {
    src = rendered;
    inherit crdSplit;
    transformers = transformers ++ lib.optional (namespace != null) appendNamespace;
  };
in
imported
