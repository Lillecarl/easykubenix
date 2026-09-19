# Turn a YAML document stream into easykubenix configuration.
#
# This is the primitive both import paths are built on. It returns a config
# fragment, so a caller places it with `lib.mkMerge` (or by returning it from a
# module) rather than going through an option:
#
#   config = lib.mkMerge [
#     (ekn.lib.importYaml { src = ./manifests.yaml; })
#     { ... }
#   ];
#
# A function rather than only a module option, because a component constructor
# has the real fixpoint in scope and wants a value, not an option to write into
# and read back. `importyaml.nix` and `helm.nix` are thin wrappers over this
# one, so the two paths cannot drift apart again -- they had byte-identical
# `kubernetes.apiMappings` blocks and near-identical downstreams before this
# existed, which is how one gained a hook the other lacked.
{
  lib,
  parseYAMLStream,
}:
{
  # Derivation, store path, or URL holding the YAML stream. A URL is fetched
  # with `builtins.fetchTree`.
  src,
  # Functions from the whole object list to a new one, applied in order.
  #
  # The only hook. There is deliberately no per-object one: `map f` expresses
  # it, and two hooks cost an ordering rule that has to be documented and
  # remembered forever.
  #
  # These run before anything below -- before the CRD split and before the
  # objects are grouped by namespace. That ordering is the point. Grouping
  # reads `metadata.namespace`, and an object without one is treated as
  # cluster-scoped, so only a hook here can default a namespace. A transformer
  # also sees CRDs, which a namespace transformer needs in order to leave them
  # alone.
  #
  # A marker is safe here. This output passes through `kubernetes.resources`,
  # whose freeform type is `kubeValueType`, so `namedListOf` resolves an
  # `mkNamedList` when it merges. That is the opposite of the rule for
  # `kubernetes.transformers`, which runs past the type -- see
  # `needsMarkerPass` in kubernetes.nix.
  transformers ? [ ],
  # Route CustomResourceDefinitions to `kubernetes.crds` instead of
  # `kubernetes.resources`.
  #
  # On by default because the cost is not marginal. Measured on a real tree:
  # 182 CRDs are 11.6 MB against 567 other objects at 0.8 MB -- 24% of the
  # objects and 94% of the payload -- and routing them through the typed
  # option took an environment from 0.90 s to 3.50 s. The curve confirms the
  # mechanism rather than only the outcome: the bypassed arm stays flat as
  # CRDs are added while the typed arm climbs.
  #
  # It is a semantic switch and not only a speed one, which is why it can be
  # turned off. `kubernetes.crds` skips the whole `generatedWithEkn` pipeline
  # -- generators, `kubernetes.transformers` and filters -- and throws on any
  # marker, because nothing there resolves one. Set this false for a project
  # whose own passes have to reach a CRD.
  #
  # `transformers` above still see CRDs either way. The split happens after
  # them.
  crdSplit ? true,
}:
let
  objects = lib.pipe (parseYAMLStream { inherit src; }) transformers;

  isCRD = object: (object.kind or null) == "CustomResourceDefinition";
  crds = lib.filter isCRD objects;
  # Everything when the split is off, so a CRD goes through the typed option
  # and the pipeline like any other object.
  resources = if crdSplit then lib.filter (object: !(isCRD object)) objects else objects;
in
{
  # `kubernetes.resources` groups by namespace then kind then name. An object
  # with no `metadata.namespace` goes to `none`, where kubernetes.nix injects
  # no namespace -- see `transformers` above for why that is the caller's job
  # to fix and not this function's.
  kubernetes.resources = lib.mkMerge (
    map (object: {
      ${object.metadata.namespace or "none"}.${object.kind}.${object.metadata.name} = object;
    }) resources
  );

  kubernetes.crds = crds;

  # Every CRD in the stream teaches `kubernetes.apiMappings` the apiVersion for
  # its own kind, so a custom resource elsewhere in the configuration does not
  # have to name one. Read from `objects` and not from `crds`, so it still
  # works with the split off.
  #
  # `mkDefault`, so a configuration can still override a mapping by hand.
  kubernetes.apiMappings = lib.listToAttrs (
    map (crd: {
      name = crd.spec.names.kind;
      value =
        let
          version = lib.pipe crd.spec.versions [
            (lib.filter (x: x.storage or false == true))
            lib.head
            (x: x.name)
          ];
        in
        lib.mkDefault "${crd.spec.group}/${version}";
    }) (lib.filter isCRD objects)
  );
}
