{
  config,
  pkgs,
  lib,
  eknPackage,
  ...
}:
{
  options.internal = lib.mkOption {
    # `types.anything`'s merge recurses into every nested attrset (including
    # derivations, which are themselves attrsets) to detect mkIf/mkOverride/
    # mkMerge markers on each key -- see `mergeDefinitions` in nixpkgs'
    # lib/modules.nix, `!(isAttrs d.value && d.value ? _type)`. Forcing WHNF
    # of a plain attrset value is normally free, but `manifestYAML`/
    # `manifestYAMLList` below are `builtins.readFile <derivation>`, whose
    # WHNF is NOT free -- `readFile` eagerly realises the file. That means
    # merely asking this attrset for its attribute *names* (e.g.
    # `has_attr`/`attr_names`, used by attribute-path selection before any
    # value is actually read) was forcing a full IFD build storm across
    # every helm release/importyaml spec, just to answer "does `internal`
    # have a key called `manifestJSONFile`". `internal` only ever has one
    # definition (this module) and was never meant to support multi-module
    # merging, so `types.raw` (single-definition passthrough, no per-key
    # decomposition) is the correct type, not `types.anything`.
    type = lib.types.raw;
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

    # `builtins.toYAML` is nanopynix's in-process primop -- only bound when
    # evaluation is running under ekn's worker (not plain `nix build`/`nix
    # eval`), hence the existence check rather than a hard dependency. When
    # present it renders at eval time with no derivation/IFD at all, so
    # prefer it outright over the derivation-fallback path below.
    #
    # Fallback: `_jsonToYAML` is the same derivation-fallback CLI subcommand
    # importyaml.nix's `_yamlToJson` counterpart uses, reusing nanopynix's
    # `to_yaml` so this stays byte-for-byte consistent with the in-process
    # `toYAML` primop path -- no more `yq` (whose old heredoc-based
    # invocation broke on manifest content containing a bare "EOF" line,
    # since an unquoted heredoc delimiter also gets scanned for inside
    # interpolated content, silently truncating the input and dumping the
    # remainder as literal shell commands). Beware that this path requires
    # IFD.
    manifestYAMLFile =
      if builtins ? toYAML then
        pkgs.writeText "manifest.yaml" (builtins.toYAML manifestAttrs)
      else
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
