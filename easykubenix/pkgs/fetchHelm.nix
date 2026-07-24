{
  stdenvNoCC,
  lib,
  kubernetes-helm,
  cacert,
}:
let
  cleanName = lib.replaceStrings [ "/" ] [ "-" ];
  isOci = repo: repo != null && lib.hasPrefix "oci://" repo;
in
{
  # name of the chart
  chart,
  # chart url to fetch from custom location
  chartUrl ? null,
  # version of the chart
  version ? null,
  # chart hash
  sha256,
  # whether to extract chart
  untar ? true,
  # use custom charts repo
  repo ? null,
  # pass --verify to helm chart
  verify ? false,
  # pass --devel to helm chart
  devel ? false,
}:
stdenvNoCC.mkDerivation {
  name = "${cleanName chart}-${if version == null then "dev" else version}";

  buildCommand = ''
    export HOME="$PWD"
    echo "adding helm repo"
    ${if repo == null || isOci repo then "" else "helm repo add repository ${repo}"}
    echo "fetching helm chart"
    helm fetch -d ./chart \
      ${if untar then "--untar" else ""} \
      ${if version == null then "" else "--version ${version}"} \
      ${if devel then "--devel" else ""} \
      ${if verify then "--verify" else ""} \
      ${
        if chartUrl != null then
          chartUrl
        else if repo == null then
          chart
        else if isOci repo then
          # OCI registries have no index.yaml to `helm repo add` against --
          # `helm fetch`/`pull` accepts an oci:// ref directly instead.
          "${repo}/${chart}"
        else
          "repository/${chart}"
      }
    # helm fetch --untar with a direct chartUrl can leave a same-named empty
    # directory alongside the real extracted chart, so pick the one directory
    # that actually contains a Chart.yaml rather than globbing all of them.
    chartDir=$(find chart -mindepth 1 -maxdepth 1 -type d -exec test -f {}/Chart.yaml \; -print -quit)
    if [ -z "$chartDir" ]; then
      echo "no extracted chart directory containing Chart.yaml found under ./chart" >&2
      exit 1
    fi
    cp -r "$chartDir"/. $out
  '';
  outputHashMode = "recursive";
  outputHashAlgo = "sha256";
  outputHash = sha256;
  nativeBuildInputs = [
    kubernetes-helm
    cacert
  ];
}
