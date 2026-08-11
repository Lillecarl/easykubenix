{
  config,
  lib,
  ...
}:
let
  # Helm's `InstallOrder` verbatim, from pkg/release/v1/util/kind_sorter.go --
  # the de-facto order the entire ecosystem's charts are written against, which
  # is why it is the default rather than something designed here.
  #
  # Kept as an ordered list and numbered below rather than written out as an
  # attrset of literal numbers: the hand-transcribed attrset this replaced had
  # silently shifted everything from `IngressClass` onward by one slot (a gap
  # at 165), which no reader would catch and no test would fail on.
  helmInstallOrder = [
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
in
{
  options.ekn = {
    discriminator = lib.mkOption {
      type = lib.types.str;
      default = "easykubenix";
      description = ''
        Value of the `ekn.dev/discriminator` label stamped on every object
        `ekn` applies, and the selector it lists objects back by when
        pruning. It is therefore the *prune scope*: `ekn kubeapply --prune`
        deletes every object carrying this label that the current apply did
        not produce.

        Change it between projects sharing a cluster, or two easykubenix
        instances will prune each other's objects out from under one another.
        Per-GitOps-target applies get their own derived value -- see
        `gitOps.targets.<name>.discriminator`.
      '';
      example = "acme-production";
    };

    resourcePriority = lib.mkOption {
      type = lib.types.attrsOf lib.types.int;
      description = ''
        Apply order by object kind, lowest number first. Objects sharing a
        number apply together as one barrier, which is fully applied (and,
        for CustomResourceDefinitions, waited on to become Established)
        before the next barrier starts.

        Defaults to Helm's `InstallOrder`, numbered in steps of five so a
        kind can be slotted between two neighbours without renumbering the
        rest.

        A kind absent from this set applies at 1000, which is every custom
        resource. That gives three bands to number against:

        - `0`-`185`   Helm's order as-is
        - `186`-`999` late, but still before custom resources
        - `1000`      custom resources and anything else unlisted
        - `1001`+     after custom resources

        The two admission webhook configurations are moved into that last
        band, which is the one place this deviates from Helm. Helm sorts
        unknown kinds after its whole list, leaving the webhooks ahead of
        every custom resource they intercept; during a bootstrap the
        webhook's backing workload was applied seconds earlier and is not
        serving yet, so each intercepted write costs the webhook's full
        `timeoutSeconds`. Numbering them past the unlisted band is what
        keeps an apply from stalling on a backend it is still bringing up.
      '';
      default =
        lib.listToAttrs (lib.imap0 (index: kind: lib.nameValuePair kind (index * 5)) helmInstallOrder)
        # Overriding two entries of the list above rather than reordering it,
        # so the Helm order stays a verbatim transcription that can be diffed
        # against upstream, and the deviation is one visible exception rather
        # than a silent edit to the data.
        // {
          MutatingWebhookConfiguration = 1005;
          ValidatingWebhookConfiguration = 1010;
        };
    };

    cacheTo = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = ''
        Destination Nix store URI (e.g. "ssh-ng://user@host:2222") that `ekn
        deploy` automatically pushes `ekn.cachePackage`'s closure to, before
        committing/pushing GitOps manifests to the remote. `null` disables
        the cache push entirely.
      '';
      example = "ssh-ng://nix@cache.example.com:2222";
    };

    cachePackage = lib.mkOption {
      type = lib.types.package;
      default = config.internal.manifestJSONFile;
      description = ''
        Derivation whose full closure gets pushed to `ekn.cacheTo`. Defaults
        to `internal.manifestJSONFile` -- the same manifest-JSON derivation
        `ekn validate` already builds, rather than a fresh whole-cluster
        dump. Nix string context means its closure automatically includes
        every store path referenced anywhere in the generated manifests
        (e.g. CSI-mounted store paths embedded in `volumeAttributes`),
        without needing to enumerate them by hand. Override only if a
        project needs a different (narrower or wider) closure pushed
        instead.
      '';
    };
  };
}
