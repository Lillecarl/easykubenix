self: lib: rec {
  # The `mkIfExists` marker and the type that reads it. See
  # conditionalAttrsOf.nix. A module author reaches them as
  # `lib.mkIfExists`, `lib.mkIfExistsAtPath` and `lib.conditionalAttrsOf`,
  # the same way it reaches `lib.mkNamedList`.
  inherit (import ./conditionalAttrsOf.nix { inherit lib; })
    conditionalAttrsOf
    isIfExists
    mkIfExists
    mkIfExistsAtPath
    ;

  # The three markers use `_type`. This is the tag that the module system uses
  # for its own directives. `mkIf`, `mkMerge` and `mkOverride` all use it.
  # Nixpkgs ignores a `_type` value that it does not know. Such a value goes
  # through `dischargeProperties` and `pushDownProperties` without a change.
  # Thus the tag is safe. It also makes an override different in structure from
  # a Kubernetes object that is an attribute set.
  namedListType = "namedList";
  numberedListType = "numberedList";

  isNamedList = value: lib.isAttrs value && (value._type or null) == namedListType;
  isNumberedList = value: lib.isAttrs value && (value._type or null) == numberedListType;
  isMarkedList = value: isNamedList value || isNumberedList value;
  # Remove the marker. This gives the bare attribute set of entries.
  stripListMarker = value: lib.removeAttrs value [ "_type" ];

  # Search a value for a named-list or numbered-list marker.
  # `ekn.lib.kubeValueType` resolves a marker when it merges an option. Thus a
  # marker can only stay in a value that goes around the type, such as an entry
  # of `kubernetes.crds`. Such a marker would reach the cluster as a literal
  # `_type` field, which is not valid Kubernetes.
  # The search stops at the first marker, because `lib.any` is lazy.
  hasListMarker =
    value:
    if isMarkedList value then
      true
    else if lib.isAttrs value then
      lib.any hasListMarker (lib.attrValues value)
    else if lib.isList value then
      lib.any hasListMarker value
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
  kubeAttrsToLists =
    path: value:
    if isNamedList value then
      let
        entries = stripListMarker value;
      in
      fromNamedAttrs (lib.attrNames entries) entries
    else if isNumberedList value then
      fromNumberedAttrs (stripListMarker value)
    else
      value;

  # md5 hash an attrset, useful to trigger rollouts by hashing ConfigMaps.
  hashAttrs = attrs: builtins.hashString "md5" (builtins.toJSON attrs);
}
