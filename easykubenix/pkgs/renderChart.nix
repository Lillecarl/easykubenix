{
  runCommand,
  lib,
  kubernetes-helm,
  fetchHelm,
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
  apiVersions ? null,
  syncWave ? "0",
  # Name of the `gitOps.targets.<name>` to route these resources through
  # (see kubernetes.nix's ekn.gitOpsTarget); null leaves them unrouted.
  gitOpsTarget ? null,
  # The `ekn` CLI package, used only by the no-primop fallback below to
  # convert this chart's rendered YAML. Not needed when evaluating through
  # `ekn` itself, hence the null default.
  eknPackage ? null,
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
  # `fromYAML11Stream` is a nanopynix primop (registered per-Session via
  # `yaml_primops()`, not a stock Nix builtin), only present when this is
  # evaluated through `ekn`'s worker. It parses YAML with 1.1 semantics --
  # notably leading-zero numbers as octal (Helm's Go YAML parser produces
  # e.g. a volume's `defaultMode: 0644`, meaning octal 420, the Unix
  # file-mode convention; YAML 1.2 would silently read that as decimal 644)
  # -- entirely in-process, with no extra derivation or subprocess.
  #
  # Falling back to plain Nix (`nix build`/`nix eval` with no nanopynix
  # primops registered) shells out to ekn's own hidden `_yamlToJson`
  # subcommand in a derivation, then parses with `builtins.fromJSON`. That
  # is the same nanopynix parser the primop above uses, just out-of-process,
  # so both branches agree on YAML 1.1 semantics.
  #
  # This used to run `yq` instead, which was the only foreign YAML parser
  # left in the project and did not agree with the primop: measured, `yq`
  # reads `0644` as octal 420 (correct) but an unquoted `yes` as the string
  # "yes" where YAML 1.1 -- and therefore Kubernetes' own go-yaml v2 -- reads
  # boolean true. That made the parse result depend on which evaluator ran.
  # No such scalar appears in any chart this repo renders today (checked: 143
  # rendered outputs, 0 hits), so it was latent rather than live, but the
  # divergence class is now gone rather than merely unexercised.
  #
  # `eknPackage` is threaded in as a *call-time* argument rather than a
  # callPackage one: this file is instantiated inside the `pkgs.extend`
  # overlay, where the ekn CLI does not exist yet. (Verified there is no
  # actual cycle -- nothing in ekn's build graph pulls a chart through
  # renderChart -- but taking it here keeps the overlay uninvolved either
  # way.) Only this fallback branch needs it, so callers evaluating through
  # `ekn` never have to supply it.
  parsed =
    if builtins ? fromYAML11Stream then
      builtins.fromYAML11Stream (builtins.readFile resourcesYaml)
    else if eknPackage == null then
      throw ''
        renderChart: evaluating without nanopynix's fromYAML11Stream primop
        (i.e. not through `ekn`) needs `eknPackage` passed in to convert
        "${name}"'s rendered YAML, and none was given.
      ''
    else
      let
        resourcesJson = runCommand "${name}-rendered.json" { } ''
          ${eknPackage}/bin/ekn _yamlToJson --yaml-version yaml11 \
            < ${resourcesYaml} > $out
        '';
      in
      builtins.fromJSON (builtins.readFile resourcesJson);
  rendered = lib.filter (object: object != null) parsed;
  tagged = map (
    object:
    lib.recursiveUpdate object {
      metadata.annotations."argocd.argoproj.io/sync-wave" = syncWave;
      ekn.gitOpsTarget = gitOpsTarget;
    }
  ) rendered;
  # CustomResourceDefinitions carry enormous OpenAPI schemas. Forcing them
  # through kubernetes.resources' per-object submodule (settingsFormat.type's
  # recursive value-checking) costs measurably more eval time (see
  # `crdsBypassTyping` above); split them out here so callers can route them
  # through `kubernetes.crds` instead, which skips that machinery.
  isCRD = object: object.kind == "CustomResourceDefinition";
in
{
  crds = if crdsBypassTyping then lib.filter isCRD tagged else [ ];
  resources = lib.foldl' (
    acc: object:
    lib.recursiveUpdate acc {
      ${object.metadata.namespace or "none"}.${object.kind}.${object.metadata.name} = object;
    }
  ) { } (if crdsBypassTyping then lib.filter (object: !isCRD object) tagged else tagged);
}
