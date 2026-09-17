self: lib: rec {
  # The `mkIfExists` marker and the type that reads it. See
  # conditionalAttrsOf.nix. A module author reaches them as
  # `lib.mkIfExists`, `lib.mkIfExistsAtPath` and `lib.conditionalAttrsOf`,
  # the same way it reaches `lib.mkNamedList`.
  inherit (import ./conditionalAttrsOf.nix { inherit lib; })
    conditionalAttrsOf
    ifExistsType
    isIfExists
    mkIfExists
    mkIfExistsAtPath
    ;

  # Seeded bootstrap credentials. `envSeed` makes a reference to an
  # environment variable and `envSeeded` marks the object holding one; `ekn`
  # substitutes the value at apply time. A module author reaches both as
  # `ekn.envSeed`/`ekn.envSeeded`. The rest is for this repository's own
  # module code. See envSeed.nix.
  inherit (import ./envSeed.nix { inherit lib; })
    envSeed
    envSeedAnnotationPrefix
    envSeeded
    envSeedPrefix
    envSeedVariable
    envSeedVariables
    hasEnvSeed
    isEnvSeed
    isSeededObject
    seededVariables
    ;

  # The three markers use `_type`. This is the tag that the module system uses
  # for its own directives. `mkIf`, `mkMerge` and `mkOverride` all use it.
  # Nixpkgs ignores a `_type` value that it does not know. Such a value goes
  # through `dischargeProperties` and `pushDownProperties` without a change.
  # Thus the tag is safe. It also makes an override different in structure from
  # a Kubernetes object that is an attribute set.
  namedListType = "namedList";
  numberedListType = "numberedList";
  untypedType = "untyped";

  isNamedList = value: lib.isAttrs value && (value._type or null) == namedListType;
  isNumberedList = value: lib.isAttrs value && (value._type or null) == numberedListType;
  isUntyped = value: lib.isAttrs value && (value._type or null) == untypedType;
  # Asks `isAttrs` once and reads `_type` once, rather than letting the two
  # predicates each do both. Measured at 238,025 duplicate `isAttrs` calls in
  # a full render -- about 2% of all evaluation calls.
  isMarkedList =
    value:
    lib.isAttrs value
    && (
      let
        marker = value._type or null;
      in
      marker == namedListType || marker == numberedListType
    );
  # Remove the marker. This gives the bare attribute set of entries.
  stripListMarker = value: lib.removeAttrs value [ "_type" ];

  # Search a value for any of the three markers: `mkNamedList`,
  # `mkNumberedList` and `mkIfExists`.
  # `ekn.lib.kubeValueType` and `conditionalAttrsOf` resolve a marker when they
  # merge an option. Thus a marker can only stay in a value that goes around
  # both, such as an entry of `kubernetes.crds`. Such a marker would reach the
  # cluster as a literal `_type` field, which is not valid Kubernetes.
  # The search stops at the first marker, because `lib.any` is lazy.
  #
  # One `isAttrs` and one `_type` read per node, not three and two. Calling
  # `isMarkedList` and `isIfExists` here asked `isAttrs` twice before the
  # `else if` asked a third time, on every node of every value this walks.
  # The three marker names are compared inline for that reason; the
  # predicates stay for callers with a single value to test.
  hasMarker =
    value:
    if lib.isAttrs value then
      let
        marker = value._type or null;
      in
      marker == namedListType
      || marker == numberedListType
      || marker == ifExistsType
      || lib.any hasMarker (lib.attrValues value)
    else if lib.isList value then
      lib.any hasMarker value
    else
      false;

  # Mark an attribute set as a named list.
  # A named list is a short form to override a Kubernetes list field by name.
  # Each element of such a field has a `name`.
  # The attribute name becomes the `name` of the element.
  # Each value in the input attribute set must also be an attribute set.
  mkNamedList =
    attrs:
    if !lib.isAttrs attrs then
      throw "mkNamedList error: Input must be an attribute set."
    # All children must also be attribute sets. This is a rule of the named-list
    # contract. Only objects can have a name.
    else if !(lib.all lib.isAttrs (lib.attrValues attrs)) then
      throw "mkNamedList error: All values in the attribute set must themselves be attribute sets."
    else
      attrs // { _type = namedListType; };

  # Mark a value as carried whole, never walked.
  #
  # For a value that is large, opaque and that **nobody merges field by
  # field**: a CustomResourceDefinition's OpenAPI schema, a Grafana
  # dashboard, an imported blob. `kubeValueType` walks every leaf of an
  # ordinary value through nixpkgs' merge machinery, and for these that work
  # buys nothing -- 39 dashboards cost 0.740s typed and 0.158s carried.
  #
  # **Set once.** Nothing looks inside, so nothing can merge two of them, and
  # the branch says so rather than picking one. That is the whole difference
  # from `lib.types.anything`, which measures the same here and silently
  # deep-merges a second definition -- invisible until the day someone sets
  # the value twice.
  mkUntyped = content: {
    _type = untypedType;
    inherit content;
  };

  # Mark an attribute set as a numbered list.
  # A numbered list is a short form to override a list field by index.
  # It keeps the order of the elements.
  # All keys must be integer strings, for example "0", "5" or "100".
  # Thus a sort of the keys by number gives the list again, with no data loss.
  #
  # `mkNumberedList` does not require attribute-set values, but `mkNamedList`
  # does. An index is also correct for a scalar value. This lets you override
  # `args` and `command`, for example `mkNumberedList { "1" = "--flag"; }`.
  mkNumberedList =
    attrs:
    if !lib.isAttrs attrs then
      throw "mkNumberedList error: Input must be an attribute set."
    else
      let
        keys = lib.attrNames attrs;
        # Check if every key can be successfully parsed as an integer.
        allKeysAreInts = lib.all (key: (builtins.tryEval (lib.toIntBase10 key)).success) keys;
      in
      if !allKeysAreInts then
        throw "mkNumberedList error: All keys in the attribute set must be integer strings."
      else
        attrs // { _type = numberedListType; };

  # Recursively traverses a data structure, applying a transformer function to each node.
  # The traversal is pre-order (top-down), meaning a node is transformed *before* its children.
  # The transformer function receives two arguments:
  #   1. `path`: A list of strings representing the attribute path to the current node.
  #   2. `value`: The value of the current node.
  # This allows for context-aware transformations based on a node's location.
  walkWithPath =
    transformer:
    let
      go =
        path: value:
        # The transformer is applied BEFORE recursing (pre-order)
        let
          v' = transformer path value;
        in
        if lib.isAttrs v' then
          lib.mapAttrs (name: val: go (path ++ [ name ]) val) v'
        else if lib.isList v' then
          lib.imap0 (index: val: go (path ++ [ (toString index) ]) val) v'
        else
          v';
    in
    go [ ];

  # Change the entries of a named list into a standard JSON list.
  #
  # `order` gives the keys to emit, and in which order. The key is the only
  # source of the `name` attribute, so a key wins over an inner `name`.
  #
  # There are two callers, and they differ only in the order they ask for.
  # `ekn.lib.kubeValueType` takes the order from the plain list definitions it
  # merges against, so an override keeps the position of the entry it patches.
  # `kubeAttrsToLists` has no such definition to read, so it uses attribute-name
  # order. Both share this function, thus the two paths cannot drift apart.
  fromNamedAttrs =
    order: attrs:
    map (
      name:
      let
        value = attrs.${name};
      in
      if !lib.isAttrs value then
        throw "namedList error: the value for key '${name}' is not an attribute set."
      else
        value // { inherit name; }
    ) order;

  # Change the entries of a numbered list into a standard JSON list. The keys
  # are index numbers as strings, so a sort of the keys by number gives back
  # the order. Shared by `ekn.lib.kubeValueType` and `kubeAttrsToLists`.
  fromNumberedAttrs =
    attrs:
    lib.pipe attrs [
      lib.attrsToList
      (lib.sort (a: b: (lib.toInt a.name) < (lib.toInt b.name)))
      (map (x: x.value))
    ];

  # Master transformer: change marked named and numbered attribute sets back
  # into standard JSON lists.
  #
  # `ekn.lib.kubeValueType` resolves both markers when it merges an option, so
  # a value that comes out of the module system holds no marker. This function
  # is for a value that the module system never typed. A generator and a
  # transformer both run after the merge and return such a value, and
  # `kubernetes.nix` uses this function on their output for that reason.
  # One `isAttrs` and one `_type` read, for the same reason as `hasMarker`:
  # `walkWithPath` calls this on every node, and `isNamedList` followed by
  # `isNumberedList` asked both twice.
  kubeAttrsToLists =
    path: value:
    if !(lib.isAttrs value) then
      value
    else
      let
        marker = value._type or null;
      in
      if marker == namedListType then
        let
          entries = stripListMarker value;
        in
        fromNamedAttrs (lib.attrNames entries) entries
      else if marker == numberedListType then
        fromNumberedAttrs (stripListMarker value)
      else
        value;

  # md5 hash an attrset, useful to trigger rollouts by hashing ConfigMaps.
  hashAttrs = attrs: builtins.hashString "md5" (builtins.toJSON attrs);
}
