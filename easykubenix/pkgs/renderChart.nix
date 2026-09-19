# Render a chart and return its objects, split into CRDs and everything
# else. Objects come back exactly as Helm emitted them: routing
# (`ekn.deploymentUnit`) and annotation stamping are the caller's business,
# via the transformers seam on `helm.releases`/`importYaml`.
{
  runCommand,
  lib,
  kubernetes-helm,
  fetchHelm,
  ekn-yaml2json,
}:
{
  # { repoURL, name, version, sha256 }
  chart,
  # Helm release name.
  name,
  namespace,
  values ? { },
  kubeVersion ? null,
  noHooks ? false,
  # API versions to claim the target cluster has, for a chart gating templates
  # on `.Capabilities.APIVersions.Has`. There is no cluster here, so without
  # this the list is empty and every such check answers "absent" -- usually by
  # emitting nothing at all, which renders and applies and reports healthy. See
  # `helm.charts.<name>.apiVersions` in helm.nix, which carries the detail,
  # including that Helm matches these strings literally.
  apiVersions ? null,
}:
let
  # Single switch for every chart's CRD handling: `true` routes CRDs through
  # `kubernetes.crds` (bypasses kubernetes.objects' per-object submodule
  # typing entirely); `false` folds them into `resources` like any other
  # object, so they get full settingsFormat.type checking/mergeability.
  # Measured cost of `false` on the dynamist environment (371 objects, 78
  # CRDs): ~12% slower render (~11.4s -> ~12.8s wall), structurally
  # identical output either way. Flip this one flag to switch every module
  # at once -- no other file needs to change.
  crdsBypassTyping = true;
  chartDrv = fetchHelm {
    chart = chart.name;
    repo = chart.repoURL;
    version = chart.version;
    sha256 = chart.sha256;
  };
  valuesJsonFile = builtins.toFile "${name}-values.json" (builtins.toJSON values);
  # `helm template` runs as a plain (non-fixed-output) derivation, so IFD only
  # pays for it once per set of inputs (chart/values/name/namespace) -- Nix
  # caches the build in the store like any other derivation. This replaced a
  # `builtins.renderHelm` primop call: that primop round-trips every single
  # eval through nanopynix's Python worker AND a separate gRPC hop to a Go
  # subprocess (which itself execs `helm template` and JSON-marshals the
  # result back across that boundary), none of which Nix caches between
  # evaluations. Benchmarking showed even a tiny chart (~4 objects) cost ~2s
  # per eval through that path with no dependence on content size -- a fixed
  # RPC/marshalling floor, not module-system overhead. `helm template`
  # executed as a derivation is a one-time cost; parsing its cached output
  # then happens entirely in-process (see `parsed` below), no subprocess/RPC
  # hop at all.
  resourcesYaml = runCommand "${name}-rendered.yaml" { nativeBuildInputs = [ kubernetes-helm ]; } ''
    helm template "${name}" \
      --namespace "${namespace}" \
      --include-crds \
      ${lib.optionalString (values != { }) "-f ${valuesJsonFile}"} \
      ${lib.optionalString (kubeVersion != null) "--kube-version ${kubeVersion}"} \
      ${lib.optionalString noHooks "--no-hooks"} \
      ${
        lib.optionalString (
          apiVersions != null && apiVersions != [ ]
        ) "--api-versions ${lib.concatStringsSep "," apiVersions}"
      } \
      ${chartDrv} > $out
  '';
  # `ekn-yaml2json` reads the stream and does the grouping, and this file
  # takes its answer whole. See lib/parseYamlStream.nix for why go-yaml is
  # the only reader here.
  #
  # The grouping moved out of Nix with it. `lib.foldl' lib.recursiveUpdate`
  # built the same namespace/kind/name tree, and it *deep-merged* two objects
  # that shared an identity into one nothing rendered, which then applied
  # without complaint. The Go side refuses and names the document.
  #
  # CustomResourceDefinitions carry enormous OpenAPI schemas. Forcing them
  # through kubernetes.resources' per-object submodule (settingsFormat.type's
  # recursive value-checking) costs measurably more eval time (see
  # `crdsBypassTyping` above), so `--crd-split` files them separately for a
  # caller to route through `kubernetes.crds`, which skips that machinery.
  grouped = builtins.fromJSON (
    builtins.readFile (
      runCommand "${name}-rendered.json" { nativeBuildInputs = [ ekn-yaml2json ]; } ''
        ekn-yaml2json --crd-split=${lib.boolToString crdsBypassTyping} \
          < ${resourcesYaml} > $out
      ''
    )
  );
in
{
  inherit (grouped) crds resources;
}
