{
  config,
  pkgs,
  lib,
  ...
}:
let
  cfg = config.kluctl;
  settingsFormat = pkgs.formats.json { };
in
{
  options = {
    kluctl = {
      package = lib.mkPackageOption pkgs "kluctl" { };
      discriminator = lib.mkOption {
        type = lib.types.str;
        description = ''
          kluctl deployment label discriminator. RFC1123 compliant string
          This is used to prune resources that are no longer generated so make
          sure to change this between projects
        '';
        default = "easykubenix";
      };
      preDeployScript = lib.mkOption {
        type = lib.types.lines;
        description = ''
          Bash script that runs just before deploying, useful to push manifests to
          a binary cache. JSON manifest file is passed as first argument
        '';
        default = "";
      };
      postDeployScript = lib.mkOption {
        type = lib.types.lines;
        description = ''
          Bash script that runs just after deploying
        '';
        default = "";
      };
      project = lib.mkOption {
        type = settingsFormat.type;
        description = "Anything to be rendered into .kluctl.yaml";
        default = {
          targets = [ { name = "local"; } ];
        };
      };
      resourcePriority = lib.mkOption {
        type = lib.types.attrsOf lib.types.int;
        description = "Priority of which order to apply resource types in";
        # See https://github.com/helm/helm/blob/490dffeb3458a1ad1a8e0140b33a1d1b43ce7a04/pkg/release/v1/util/kind_sorter.go#L31
        # and cry knowing that this is what the worlds Kubernetes deployments rely on.
        default = {
          Namespace = 10;
          CustomResourceDefinition = 10;
          # PriorityClass = 0;
          # Namespace = 5;
          # NetworkPolicy = 10;
          # ResourceQuota = 15;
          # LimitRange = 20;
          # PodSecurityPolicy = 25;
          # PodDisruptionBudget = 30;
          # ServiceAccount = 35;
          # Secret = 40;
          # SecretList = 45;
          # ConfigMap = 50;
          # StorageClass = 55;
          # PersistentVolume = 60;
          # PersistentVolumeClaim = 65;
          # CustomResourceDefinition = 70;
          # ClusterRole = 75;
          # ClusterRoleList = 80;
          # ClusterRoleBinding = 85;
          # ClusterRoleBindingList = 90;
          # Role = 95;
          # RoleList = 100;
          # RoleBinding = 105;
          # RoleBindingList = 110;
          # Service = 115;
          # DaemonSet = 120;
          # Pod = 125;
          # ReplicationController = 130;
          # ReplicaSet = 135;
          # Deployment = 140;
          # HorizontalPodAutoscaler = 145;
          # StatefulSet = 150;
          # Job = 155;
          # CronJob = 160;
          # IngressClass = 170;
          # Ingress = 175;
          # APIService = 180;
          # MutatingWebhookConfiguration = 185;
          # ValidatingWebhookConfiguration = 190;
        };
      };
      deployment = lib.mkOption {
        type = settingsFormat.type;
        description = "Anything to be rendered into deployment.yaml";
        default = { };
      };
      files = lib.mkOption {
        type = lib.types.attrsOf lib.types.str;
        description = "Attribute set where name is filename and value is string to be put into the file";
        default = { };
      };
      projectDir = lib.mkOption {
        type = lib.types.package;
        internal = true;
      };
      script = lib.mkOption {
        type = lib.types.package;
        internal = true;
      };
    };
  };
  config = {
    kluctl.deployment = {
      deployments =
        # Create barrier deployments for prioritized resource kinds
        (lib.pipe cfg.resourcePriority [
          lib.attrValues
          (lib.sort (a: b: a < b))
          lib.unique
          (lib.map (v: {
            path = "prio-${toString v}";
            barrier = true;
            skipDeleteIfTags = true;
          }))
        ])
        ++ [
          # Default resource kinds go into "default"
          {
            path = "default";
            skipDeleteIfTags = true;
          }
        ];
    };
    kluctl.projectDir = pkgs.writeMultipleFiles {
      name = "kluctlProject";
      files = {
        ".kluctl.yaml" = {
          content = builtins.toJSON config.kluctl.project;
        };
        "deployment.yaml" = {
          content = builtins.toJSON config.kluctl.deployment;
        };
        # Don't apply prioritized resources again.
        "default/easykubenix.yaml" = {
          content = builtins.toJSON {
            apiVersion = "v1";
            kind = "List";
            items = lib.filter (
              v: !lib.elem v.kind (lib.attrNames cfg.resourcePriority)
            ) config.kubernetes.generated;
          };
        };
      }
      # Prioritized resources
      // (lib.mapAttrs' (n: v: {
        name = "prio-${toString v}/${n}.yaml";
        value = builtins.toJSON {
          apiVersion = "v1";
          kind = "List";
          items = lib.filter (v: v.kind == n) config.kubernetes.generated;
        };
      }) cfg.resourcePriority)
      # Other user-supplied files
      // cfg.files;
    };
    kluctl.script =
      pkgs.writeScriptBin "kubenixDeploy" # bash
        ''
          #! ${pkgs.runtimeShell}
          set -euo pipefail
          set -x
          ${cfg.preDeployScript}
          ${lib.getExe cfg.package} \
            deploy \
              --no-update-check \
              --target local \
              --discriminator ${cfg.discriminator} \
              --project-dir ${cfg.projectDir} \
              $@ # --dry-run? --yes? --prune!
          ${cfg.postDeployScript}
        '';
  };
}
