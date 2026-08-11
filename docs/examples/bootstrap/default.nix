{
  pkgs,
  lib,
  easykubenix,
}:
let
  ekn = easykubenix {
    inherit pkgs;
    modules = [ ./cluster.nix ];
  };

  # The nested instance behind `gitOps.targets.bootstrap`. It is a complete
  # easykubenix instance, which is what lets the gate below exist at all: it
  # has its own `validation.script`, so the bootstrap objects can be applied
  # to a real API server without anything here reaching inside them.
  bootstrap = ekn.config.gitOps.targets.bootstrap.instance;

  targets = ekn.config.kubernetes.gitOpsTargets;
  routingJSON = builtins.toJSON {
    bootstrapPath = targets.bootstrap.target.path;
    appsPath = targets.apps.target.path;
    bootstrapKinds = map (o: "${o.kind}/${o.metadata.name}") targets.bootstrap.objects;
    appsKinds = map (o: "${o.kind}/${o.metadata.name}") targets.apps.objects;
    generatedKinds = map (o: "${o.kind}/${o.metadata.name}") ekn.config.kubernetes.generated;
    rootApplication =
      (lib.findFirst (o: o.kind == "Application") null targets.bootstrap.objects).spec.source;
  };
in
{
  inherit (ekn) manifestJSON manifestYAMLFile;

  # Boots etcd + kube-apiserver, applies the bootstrap objects through the
  # same `apply_and_prune` that `ekn kubeapply --target bootstrap` uses --
  # ArgoCD's CRDs first, then the `Application` that needs them -- and
  # kubeconforms the result against the live schema. Not part of `checks`,
  # same as `validation`'s: run it with
  # `nix run --file ./nix packages.bootstrapValidationScript`.
  validationScript = bootstrap.config.validation.script;

  check =
    pkgs.runCommand "check-bootstrap-routing"
      {
        nativeBuildInputs = [ pkgs.jq ];
        json = routingJSON;
        passAsFile = [ "json" ];
      }
      ''
        set -euo pipefail
        cd "$TMPDIR"
        jq '.' "$jsonPath"

        expect() {
          local label="$1" filter="$2" want="$3" got
          got=$(jq -r "$filter" "$jsonPath")
          if [[ "$got" != "$want" ]]; then
            echo "FAIL: $label: expected $want, got $got"
            exit 1
          fi
        }

        # The bootstrap instance's objects land in the bootstrap target...
        expect "bootstrap path" '.bootstrapPath' 'bootstrap'
        expect "bootstrap has argocd objects" \
          '[.bootstrapKinds[] | select(startswith("CustomResourceDefinition/"))] | length > 0' 'true'
        expect "bootstrap has the root Application" \
          '.bootstrapKinds | index("Application/root") != null' 'true'
        expect "bootstrap creates its own namespace" \
          '.bootstrapKinds | index("Namespace/argocd") != null' 'true'

        # ...and nowhere else. This is the whole point: a plain `ekn kubeapply`
        # or `ekn validate` reads `generated`, and must not see any of it.
        expect "generated is only the parent's object" \
          '.generatedKinds == ["ConfigMap/hello"]' 'true'
        expect "apps target is only the parent's object" \
          '.appsKinds == ["ConfigMap/hello"]' 'true'
        expect "apps path" '.appsPath' 'clusters/example/apps'

        # The root Application was built from the parent's config rather than
        # from values repeated by hand -- the reason `parent` exists.
        expect "root app branch" '.rootApplication.targetRevision' 'deploy'
        expect "root app path" '.rootApplication.path' 'clusters/example/apps'
        expect "root app repo" '.rootApplication.repoURL' 'https://github.com/example/cluster.git'

        echo "PASS: bootstrap routing" > "$out"
      '';
}
