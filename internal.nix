{
  config,
  pkgs,
  lib,
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

    # Makes a valid YAML string, supports multiple documents or single attrsets,
    # documents will be JSON formatted since Nix can't render YAML.
    toYAMLStr =
      input:
      if builtins.typeOf input == "list" then
        lib.concatStringsSep "\n---\n" (map (doc: builtins.toJSON doc) input)
      else if builtins.typeOf input == "set" then
        builtins.toJSON input
      else
        throw "toYAML only supports set and list types";

    # Makes a valid YAML string, supports multiple documents or single attrsets,
    # reformatted using "yq-go".
    toYAMLFile =
      filename: input:
      pkgs.runCommand filename
        {
          nativeBuildInputs = [
            pkgs.yq-go
          ];
          yamlContent = toYAMLStr input;
          passAsFile = [ "yamlContent" ];
        }
        #bash
        ''
          yq --prettyPrint < $yamlContentPath > $out
        '';

    manifestAttrs = {
      apiVersion = "v1";
      kind = "List";
      items = generatedOrdered;
    };
    manifestJSON = builtins.toJSON manifestAttrs;
    manifestJSONFile = pkgs.writeText "manifest.json" manifestJSON;
    # Beware that YAML rendering requires IFD
    manifestYAMLList = builtins.readFile manifestYAMLFile;
    manifestYAMLFileList =
      pkgs.runCommand "manifest.yaml" { } # bash
        ''
          ${lib.getExe pkgs.yq} --yaml-output '.' ${manifestJSONFile} > $out
        '';
    manifestYAML = builtins.readFile manifestYAMLFile;
    manifestYAMLFile = toYAMLFile "nix-csi.yaml" generatedOrdered;
  };
}
