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
  _class = "kubernetes";

  imports = [
    (lib.mkRemovedOptionModule [ "ekn" "discriminator" ] ''
      `ekn.discriminator' is now `ekn.environment', and the label it stamps
      is now `ekn.dev/environment'. Rename the definition; the value can stay
      the same.

      The prune scope is now two labels, not one. `ekn' stamps
      `ekn.dev/environment' at apply time, and every deployment unit renders
      `ekn.dev/deployment-unit'. A whole-instance prune selects objects
      carrying this environment and *no* unit label; `--target <name>' selects
      this environment and that unit. So a per-unit discriminator is gone
      too -- `deployment.units.<name>.discriminator' no longer exists, and a
      nested instance inherits `ekn.environment' from its parent.

      **A project that already deployed has live objects wearing the old
      label.** `--prune' finds objects by label alone, so nothing recognises
      them any more: they are orphaned, not deleted, and nothing reports it.
      Relabel them once before the next prune:

        kubectl label <kinds> -A -l ekn.dev/discriminator=<value> \
          ekn.dev/environment=<value>
    '')
  ];

  options.ekn = {
    environment = lib.mkOption {
      type = lib.types.str;
      description = ''
        Value of the `ekn.dev/environment` label stamped on every object
        `ekn` applies, and half of the selector it lists objects back by
        when pruning.

        The other half is `ekn.dev/deployment-unit`, which every deployment
        unit renders onto its own objects. Together they give two prune
        scopes:

        - `ekn kubeapply --prune` deletes objects carrying this environment
          and *no* unit label, which the current apply did not produce.
        - `ekn kubeapply --target <name> --prune` deletes objects carrying
          this environment and that unit's label, which the current apply
          did not produce.

        The environment label is stamped by `ekn` at apply time rather than
        rendered. That is what confines pruning to objects `ekn` itself
        applied: an object a GitOps engine synced from the committed YAML
        never carries it, so `ekn` and that engine cannot end up deleting
        each other's work.

        Required, with no default, and that is deliberate.

        A default is a value every project shares until somebody thinks to
        change it. Two easykubenix projects that both took it and both
        deploy to one cluster do not conflict quietly: each apply prunes
        the other's objects, because from where it stands they are objects
        carrying its own label that its own apply did not produce. The
        scope is per kind, not the whole cluster, but a kind both projects
        use is the normal case.

        Nothing can detect that from inside one project, so the name has to
        be a decision somebody made. Only a deploy needs it: rendering a
        manifest never reads this, so a configuration that only builds
        `manifestJSONFile` does not have to answer.

        **Changing the value of a project that already deployed is not
        free.** Live objects carry the label they were applied with, and
        `--prune` finds objects by that label alone. Pick a different word
        and the next prune no longer recognises anything the previous one
        would have: those objects are not deleted, they are orphaned, and
        nothing reports it.
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
        dump. Its closure covers every store path the manifests reference
        *with string context intact*, so most projects need not enumerate
        anything by hand.

        A path a module deliberately strips context from is NOT in it, and
        that is the case worth checking before relying on this. nixkube
        discards context on every resource annotated `nixkube/discard`,
        because rendering on one architecture would otherwise have to build
        the other's `buildEnv`, which sets `allowSubstitutes = false` and so
        can never be fetched. Measured on nixkube's own instance:

          store paths named as text in manifest.json   4
          paths in its closure                         1   (itself)

        The four are the node and pynixd environments -- exactly the paths a
        node needs to boot. So a cluster whose nodes depend on this push must
        set `cachePackage` to something that names them with context, such as
        a `buildEnv` over them, rather than assuming the manifest carries
        them. `ekn deploy` reports a successful push either way, which is
        what makes this worth stating here.

        A module reaches them through the `csiPkgs` module argument:

          { csiPkgs, pkgs, lib, ... }:
          {
            ekn.cachePackage = pkgs.linkFarm "nixkube-node-paths" (
              lib.concatLists (
                lib.mapAttrsToList (system: p: [
                  { name = "''${system}-node"; path = p.nixkube-node-env; }
                  { name = "''${system}-pynixd"; path = p.nixkube-pynixd-env; }
                ]) csiPkgs
              )
            );
          }

        `linkFarm` and not `buildEnv`. Nothing here wants the environments
        merged, only one derivation that depends on all of them, and merging
        them fails:

          error: two given paths contain a conflicting subpath:
            .../cacheEnv/bin/kill and .../nodeEnv/bin/kill

        linkFarm gives each input its own name, so it cannot collide.

        Both environments, not only the node one. `nixkube-node-env` is what
        the node's init fetches; `nixkube-pynixd-env` is what pynixd mounts
        for itself. Pushing only the first leaves pynixd unable to start on a
        node that does not already have its own.

        Measured on nixkube's own instance: the closure holds 604 paths and
        names all four environments, against 1 for the manifest alone.

        On a multi-architecture cluster, know the cost. `buildEnv` sets
        `allowSubstitutes = false`, so a foreign-architecture environment is
        built rather than fetched even when a cache holds that exact output:
        one such closure wanted to build the aarch64 `nodeEnv` on an x86_64
        machine while its seven aarch64 dependencies fetched normally.
        Without binfmt that fails outright. `csiPkgs` holds only the systems
        `nixkube.systems` enables, so a single-architecture cluster never
        meets this.

        `always-allow-substitutes = true` in the deployer's nix.conf removes
        it without changing any derivation. Measured on the same foreign
        environment, into an empty store, with binfmt off:

          (default)  error: Cannot build '...-nodeEnv.drv'
                     Reason: platform mismatch
          (true)     copying path '...-nodeEnv' from 'https://nix-csi.cachix.org'

        It is a machine-wide setting: it lets Nix fetch any output whose
        derivation asked to be built locally, which is a deliberate trade
        rather than a free one.

        Override for that, or whenever a project needs a different (narrower
        or wider) closure pushed.
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
