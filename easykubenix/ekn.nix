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

  resourcePriorityDefaults =
    lib.listToAttrs (lib.imap1 (index: kind: lib.nameValuePair kind (index * 10)) helmInstallOrder)
    # These three keep their place in the list above -- it stays a verbatim
    # transcription -- and are renumbered here, so the deviation from Helm is
    # three visible lines rather than an edit to the data.
    #
    # Their cost is paid by the requests that come after them, not by creating
    # them: an admission webhook configuration makes the API server call its
    # backing Service on every matching write, and an aggregated APIService
    # hands that group's discovery to one, so a backend that is not serving
    # fails discovery outright rather than merely stalling a write. During a
    # bootstrap that backend was applied seconds earlier and is not ready, so
    # all three have to land after everything they can intercept -- including
    # custom resources, which are unlisted and so sort at 1000 (see
    # DEFAULT_BARRIER_PRIORITY in ekn/src/ekn/apply.py; tests/test_eval.py
    # asserts the two sides agree).
    // {
      APIService = 1010;
      MutatingWebhookConfiguration = 1020;
      ValidatingWebhookConfiguration = 1030;
    };
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

        Defaults to Helm's `InstallOrder`, numbered in steps of ten so kinds
        can be slotted between two neighbours without renumbering the rest.

        A kind absent from this set applies at 1000, which is every custom
        resource. That gives four bands to number against:

        - `10`-`380`  Helm's order as-is
        - `381`-`999` late, but still before custom resources
        - `1000`      custom resources and anything else unlisted
        - `1001`+     after custom resources

        `APIService` and the two admission webhook configurations sit in that
        last band, which is the one place this deviates from Helm. Helm sorts
        unknown kinds after its whole list, leaving all three ahead of every
        custom resource they intercept; during a bootstrap their backing
        workload was applied seconds earlier and is not serving yet, so each
        intercepted request costs a full timeout or fails discovery outright.

        Definitions merge per key, so setting one kind keeps the defaults for
        every other. Replacing the set wholesale takes `lib.mkForce`.
      '';
      default = resourcePriorityDefaults;
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

  # An option's `default` is used only when it has no definitions at all, so a
  # config setting one kind of `resourcePriority` would otherwise drop the
  # other 38 -- `attrsOf` merges *definitions* per key, and a default is not a
  # definition. Defining it here at `mkDefault` priority makes each key its own
  # definition, so a user's key wins and every other key survives.
  #
  # The `default` above is then never the value that gets used; it stays
  # because it is what the generated option docs render, and because both come
  # from the same binding they cannot drift.
  config.ekn.resourcePriority = lib.mapAttrs (_: lib.mkDefault) resourcePriorityDefaults;
}
