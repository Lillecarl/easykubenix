self: lib: {
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

  # Master transformer: Converts special `_namedlist` and `_numberedlist` attribute sets
  # back into standard JSON lists. This function is the inverse of `kubeListsToAttrs`.
  kubeAttrsToLists =
    path: value:
    if lib.isAttrs value then
      if value._namedlist or false == true then
        # `_namedlist`s are attribute sets where keys were derived from a `name` attribute.
        # Convert back to a list of attribute sets, injecting the key back as the `name` attribute.
        lib.mapAttrsToList (
          name: val:
          if !lib.isAttrs val then
            throw "namedListToList error: Value for key '${name}' is not an attribute set."
          else
            val // { inherit name; }
        ) (lib.removeAttrs value [ "_namedlist" ])
      else if value._numberedlist or false == true then
        # `_numberedlist`s are attribute sets where keys are numeric indices ("0", "1", ...).
        # Convert back to a simple list of values, ensuring order is preserved by sorting the keys.
        lib.pipe value [
          (x: lib.removeAttrs x [ "_numberedlist" ])
          lib.attrsToList
          (lib.sort (a: b: (lib.toInt a.name) < (lib.toInt b.name)))
          (map (x: x.value))
        ]
      else
        value
    else
      value;

  # Master transformer: Converts all standard JSON lists into special attribute sets
  # to make them easily overridable in Nix.
  kubeListsToAttrs =
    path: value:
    let
      currentKey = lib.last (path ++ [ null ]);
      # Heuristic to identify a list that should become a `_namedlist`.
      # A list is a candidate if all its elements are attribute sets that contain a `name` key.
      isNamedListCandidate =
        lib.isList value
        && (lib.all (v: lib.isAttrs v && lib.hasAttr "name" v) value)
        # Special exclusion for Kubernetes `initContainers`. The order of init containers is
        # significant and must be preserved. By failing this check, it will be converted
        # to a `_numberedlist` instead, which preserves order.
        && currentKey != "initContainers";
    in
    if isNamedListCandidate then
      # Convert the list to a `_namedlist` attribute set. The `name` attribute of each
      # element becomes the key in the resulting attribute set.
      lib.pipe value [
        (lib.map (x: {
          inherit (x) name;
          value = lib.removeAttrs x [ "name" ];
        }))
        lib.listToAttrs
        (x: x // { _namedlist = true; })
      ]
    # Any other list (e.g., simple string lists like container `args`, or `initContainers`)
    # is converted to a `_numberedlist`.
    else if lib.isList value then
      # The list index becomes the key (e.g., "0", "1", "2", ...), preserving order.
      lib.pipe value [
        (lib.imap0 (
          i: v: {
            name = toString i;
            value = v;
          }
        ))
        lib.listToAttrs
        (x: x // { _numberedlist = true; })
      ]
    else
      value;
}
