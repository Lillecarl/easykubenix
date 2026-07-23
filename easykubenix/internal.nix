{
  config,
  pkgs,
  lib,
  eknPackage,
  ...
}:
{
  options.internal = lib.mkOption {
    type = lib.types.anything;
  };
  config.internal = rec {
    # https://github.com/helm/helm/blob/4a91f3ad5cc0c1521f6d4dcb5681e2da4baaa157/pkg/release/v1/util/kind_sorter.go#L31
    helmOrder = [
      "PriorityClass"
      "Namespace"
      "NetworkPolicy"
      "ResourceQuota"
      "LimitRange"
      "PodSecurityPolicy"
      "PodDisruptionBudget"
      "ServiceAccount"
      "Secret"
      "SecretList"
      "ConfigMap"
      "StorageClass"
      "PersistentVolume"
      "PersistentVolumeClaim"
      "CustomResourceDefinition"
      "ClusterRole"
      "ClusterRoleList"
      "ClusterRoleBinding"
      "ClusterRoleBindingList"
      "Role"
      "RoleList"
      "RoleBinding"
      "RoleBindingList"
      "Service"
      "DaemonSet"
      "Pod"
      "ReplicationController"
      "ReplicaSet"
      "Deployment"
      "HorizontalPodAutoscaler"
      "StatefulSet"
      "Job"
      "CronJob"
      "IngressClass"
      "Ingress"
      "APIService"
      "MutatingWebhookConfiguration"
      "ValidatingWebhookConfiguration"
    ];

    # Map kind -> priority (index in helmOrder)
    applyPriorities = lib.listToAttrs (
      lib.imap0 (i: kind: {
        name = kind;
        value = i;
      }) helmOrder
    );

    # Get priority for a kind, default to end if not in list
    getApplyPriority = kind: applyPriorities.${kind} or (lib.length helmOrder);

    generatedOrdered = lib.sort (
      a: b: (getApplyPriority a.kind) < (getApplyPriority b.kind)
    ) config.kubernetes.generated;

    manifestAttrs = {
      apiVersion = "v1";
      kind = "List";
      items = generatedOrdered;
    };
    manifestJSON = builtins.toJSON manifestAttrs;
    manifestJSONFile = pkgs.writeText "manifest.json" manifestJSON;

    # A single derivation for the whole namespace/kind/name.yaml tree --
    # one `runCommand` instead of one derivation per object (the previous
    # `linkFarm`-of-writeTexts approach), which was slow to both evaluate
    # and cache at hundreds of objects. `builtins.toJSON` accumulates string
    # context from every embedded derivation across the whole list into one
    # string; `passAsFile` hands that string to the builder as a file
    # without creating a derivation of its own, so Nix still builds and
    # substitutes any referenced derivations as real inputs of *this*
    # derivation before `ekn split-manifest` runs. The splitting/formatting
    # itself (namespace/kind/name.yaml layout, pretty YAML) is done by ekn's
    # `split-manifest` subcommand, reusing the same flatten_manifests logic
    # as ekn/src/ekn/git.py -- so this works identically for ekn users and
    # plain `nix build` users, no fast/slow-path split needed.
    manifestYAMLDir =
      pkgs.runCommand "manifest-yaml-dir"
        {
          nativeBuildInputs = [ eknPackage ];
          json = builtins.toJSON generatedOrdered;
          passAsFile = [ "json" ];
        }
        ''
          mkdir -p "$out"
          ekn split-manifest "$jsonPath" "$out"
        '';

    # Beware that YAML rendering requires IFD
    # `_jsonToYAML` is the same derivation-fallback CLI subcommand
    # importyaml.nix's `_yamlToJson` counterpart uses, reusing nanopynix's
    # `to_yaml` so this stays byte-for-byte consistent with the in-process
    # `toYAML` primop path -- no more `yq` (whose old heredoc-based
    # invocation broke on manifest content containing a bare "EOF" line,
    # since an unquoted heredoc delimiter also gets scanned for inside
    # interpolated content, silently truncating the input and dumping the
    # remainder as literal shell commands).
    manifestYAMLFile =
      pkgs.runCommand "manifest.yaml"
        {
          nativeBuildInputs = [ eknPackage ];
          json = manifestJSON;
          passAsFile = [ "json" ];
        }
        ''
          ekn _jsonToYAML < "$jsonPath" > $out
        '';
    manifestYAML = builtins.readFile manifestYAMLFile;
    manifestYAMLList = builtins.readFile manifestYAMLFile;
  };
}
